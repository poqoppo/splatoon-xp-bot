import discord
from discord import app_commands
import re
import os
import time
import random
import urllib.request
import json
import asyncio
from datetime import datetime, timedelta, timezone
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
from flask import Flask
from threading import Thread

# Flaskサーバー用 (Renderスリープ防止)
app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

t = Thread(target=run_flask, daemon=True)
t.start()

# 日本語フォント設定
def setup_font():
    font_url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Regular.ttc"
    font_path = "NotoSansCJK.ttc"
    try:
        if not os.path.exists(font_path):
            urllib.request.urlretrieve(font_url, font_path)
        fm.fontManager.addfont(font_path)
        plt.rcParams['font.family'] = 'Noto Sans CJK JP'
    except Exception as e:
        print(f"Font setup warning: {e}")
        plt.rcParams['font.family'] = 'sans-serif'

setup_font()

# ==================== 【設定部分】 ====================
TOKEN = os.environ.get('DISCORD_TOKEN')

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が設定されていません")

TARGET_CHANNEL_ID = 1474973509217423401  # XP報告するチャンネルID
LOG_CHANNEL_ID = 1508739566138294522     # バックアップ用チャンネルID

# ★ここをDiscordユーザーIDに置き換えてください
# 例: ADMIN_USER_IDS = [123456789012345678, 987654321098765432]
ADMIN_USER_IDS = [
    123456789012345678,
    987654321098765432,
]

SETTINGS_FILE = "settings.json"

DEFAULT_SETTINGS = {
    "drama_enabled": True,
    "area_notice_enabled": True,
}
# ====================================================

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return DEFAULT_SETTINGS.copy()

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        settings = DEFAULT_SETTINGS.copy()
        settings.update(data)
        return settings
    except Exception as e:
        print(f"Settings load error: {e}")
        return DEFAULT_SETTINGS.copy()

def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(BOT_SETTINGS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Settings save error: {e}")

BOT_SETTINGS = load_settings()

def is_admin(user: discord.User):
    return user.id in ADMIN_USER_IDS

class XPClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

intents = discord.Intents.default()
intents.message_content = True
client = XPClient(intents=intents)

# 日本時間
JST = timezone(timedelta(hours=+9), 'JST')

# APIキャッシュ
CACHED_AREA_SHIFTS = set()
CACHED_AREA_DETAILS = []
LAST_SCHEDULE_FETCH = None
SCHEDULE_CACHE_SECONDS = 600

def get_current_season(now_dt):
    """
    冬シーズンの年ズレ対策済み。
    例:
    2026/12 -> 2026年 冬シーズン
    2027/01 -> 2026年 冬シーズン
    2027/02 -> 2026年 冬シーズン
    """
    if now_dt.month in [3, 4, 5]:
        return now_dt.year, "春シーズン"
    elif now_dt.month in [6, 7, 8]:
        return now_dt.year, "夏シーズン"
    elif now_dt.month in [9, 10, 11]:
        return now_dt.year, "秋シーズン"
    else:
        season_year = now_dt.year if now_dt.month == 12 else now_dt.year - 1
        return season_year, "冬シーズン"

def get_previous_season_for_award(now_dt):
    """
    表彰式用。
    3月 -> 前年冬
    6月 -> 当年春
    9月 -> 当年夏
    12月 -> 当年秋
    """
    if now_dt.month == 3:
        return now_dt.year - 1, "冬シーズン"
    elif now_dt.month == 6:
        return now_dt.year, "春シーズン"
    elif now_dt.month == 9:
        return now_dt.year, "夏シーズン"
    elif now_dt.month == 12:
        return now_dt.year, "秋シーズン"
    return now_dt.year, "不明シーズン"

def parse_api_datetime(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)

async def fetch_area_schedule(force=False):
    """
    Xマッチのガチエリアスケジュールを取得してキャッシュする。
    10分以内ならAPIを叩かずキャッシュを使う。
    """
    global CACHED_AREA_SHIFTS, CACHED_AREA_DETAILS, LAST_SCHEDULE_FETCH

    now = datetime.now(JST)

    if (
        not force
        and LAST_SCHEDULE_FETCH is not None
        and (now - LAST_SCHEDULE_FETCH).total_seconds() < SCHEDULE_CACHE_SECONDS
        and CACHED_AREA_DETAILS
    ):
        return

    try:
        req = urllib.request.Request(
            "https://spla3.yuu26.com/api/x/schedule",
            headers={'User-Agent': 'XP-Bot/3.0'}
        )
        res = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        data = json.loads(res.read().decode())

        for node in data.get("results", []):
            if node.get("rule", {}).get("key") != "AREA":
                continue

            st = parse_api_datetime(node["start_time"])
            et = parse_api_datetime(node["end_time"])
            stages = [s.get("name", "不明ステージ") for s in node.get("stages", [])]

            CACHED_AREA_SHIFTS.add((st, et))

            detail = {
                "start": st,
                "end": et,
                "stages": stages,
            }

            if detail not in CACHED_AREA_DETAILS:
                CACHED_AREA_DETAILS.append(detail)

        CACHED_AREA_DETAILS.sort(key=lambda x: x["start"])
        LAST_SCHEDULE_FETCH = now

    except Exception as e:
        print(f"API Fetch Error: {e}")

async def update_and_get_last_area_time(now_dt):
    await fetch_area_schedule()

    best_et = None

    if CACHED_AREA_SHIFTS:
        for st, et in CACHED_AREA_SHIFTS:
            if st <= now_dt:
                if best_et is None or et > best_et:
                    best_et = et

    return best_et

async def get_next_area_shift(now_dt):
    """
    現在時刻より後に始まる次のガチエリアを返す。
    戻り値: {"start": datetime, "end": datetime, "stages": list[str]} or None
    """
    await fetch_area_schedule()

    candidates = [
        d for d in CACHED_AREA_DETAILS
        if d["start"] > now_dt
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["start"])
    return candidates[0]

# APIが死んでいる場合の最終保険
def get_last_splat_end_time(dt):
    h = dt.hour
    if h % 2 == 0:
        h -= 1

    if h < 0:
        h = 23
        dt = dt - timedelta(days=1)

    return dt.replace(hour=h, minute=0, second=0, microsecond=0)

def parse_specified_time(content, now_dt):
    m1 = re.search(r'([0-9]{1,2}):([0-9]{2})', content)
    if m1:
        h, m = int(m1.group(1)), int(m1.group(2))
        if 0 <= h < 24 and 0 <= m < 60:
            return now_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    m2 = re.search(r'([0-9]{1,2})時', content)
    if m2:
        h = int(m2.group(1))
        if 0 <= h < 24:
            return now_dt.replace(hour=h, minute=0, second=0, microsecond=0)

    return None

def parse_args_from_str(text, current_year, current_season_type):
    if not text:
        text = ""

    is_continuous = "通し" in text or "やった日から" in text

    target_year = str(current_year)
    year_match = re.search(r'([0-9]{4})年', text)
    if year_match:
        target_year = year_match.group(1)

    month_match = re.search(r'([0-9]{1,2})月', text)

    season_match = None
    for s in ["春シーズン", "夏シーズン", "秋シーズン", "冬シーズン", "春", "夏", "秋", "冬"]:
        if s in text:
            season_match = s if "シーズン" in s else f"{s}シーズン"
            break

    if month_match:
        target_month = int(month_match.group(1))
        return target_year, None, target_month, False, f"{target_year}年 {target_month}月", is_continuous
    elif season_match:
        return target_year, season_match, None, False, f"{target_year}年 {season_match}", is_continuous
    else:
        return target_year, None, None, True, "全期間", is_continuous

def get_graph_bounds(year_str, season_str, month_int):
    y = int(year_str)

    if month_int:
        start = datetime(y, month_int, 1, 0, 0, tzinfo=JST)
        if month_int == 12:
            end = datetime(y + 1, 1, 1, 0, 0, tzinfo=JST)
        else:
            end = datetime(y, month_int + 1, 1, 0, 0, tzinfo=JST)
        return start, end

    if season_str:
        if "春" in season_str:
            start = datetime(y, 3, 1, 0, 0, tzinfo=JST)
            end = datetime(y, 6, 1, 0, 0, tzinfo=JST)
        elif "夏" in season_str:
            start = datetime(y, 6, 1, 0, 0, tzinfo=JST)
            end = datetime(y, 9, 1, 0, 0, tzinfo=JST)
        elif "秋" in season_str:
            start = datetime(y, 9, 1, 0, 0, tzinfo=JST)
            end = datetime(y, 12, 1, 0, 0, tzinfo=JST)
        elif "冬" in season_str:
            start = datetime(y, 12, 1, 0, 0, tzinfo=JST)
            end = datetime(y + 1, 3, 1, 0, 0, tzinfo=JST)
        else:
            return None, None

        return start, end

    return None, None

async def get_all_records():
    channel = client.get_channel(LOG_CHANNEL_ID)
    data = {}

    if not channel:
        return data

    async for message in channel.history(limit=5000, oldest_first=True):
        if message.author == client.user:
            p = message.content.split('|')

            if len(p) >= 4:
                uid = int(p[0])

                if len(p) >= 6:
                    uname = p[1]
                    xp = int(p[2])
                    t_str = p[3]
                    season = p[4]
                    msg_id = int(p[5])
                else:
                    uname = f"ID:{uid}"
                    xp = int(p[1])
                    t_str = p[2]
                    season = p[3]
                    msg_id = 0

                try:
                    dt = datetime.strptime(t_str, '%Y/%m/%d %H:%M').replace(tzinfo=JST)
                except ValueError:
                    try:
                        dt = datetime.strptime(t_str, '%Y/%m/%d').replace(tzinfo=JST)
                    except ValueError:
                        dt = message.created_at.astimezone(JST)

                if uid not in data:
                    data[uid] = {'name': uname, 'records': []}

                if uname != f"ID:{uid}":
                    data[uid]['name'] = uname

                data[uid]['records'].append({
                    'xp': xp,
                    'time': dt,
                    'season': season,
                    'msg_id': msg_id
                })

    for uid in data:
        data[uid]['records'].sort(key=lambda x: x['time'])

    return data

@client.event
async def on_ready():
    print(f'{client.user} が起動しました！')

# ==================== スラッシュコマンド群 ====================

@client.tree.command(name="通知設定", description="煽り文章・次のガチエリア表示をON/OFFします")
@app_commands.describe(
    煽り文章="XP保存後の煽り文章を表示するか",
    エリア通知="XP保存後に次のガチエリア時間とステージを表示するか"
)
async def notification_settings(
    interaction: discord.Interaction,
    煽り文章: bool = None,
    エリア通知: bool = None
):
    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return

    changed = False

    if 煽り文章 is not None:
        BOT_SETTINGS["drama_enabled"] = 煽り文章
        changed = True

    if エリア通知 is not None:
        BOT_SETTINGS["area_notice_enabled"] = エリア通知
        changed = True

    if changed:
        save_settings()

    await interaction.followup.send(
        "⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**"
    )

@client.tree.command(name="設定確認", description="現在のBot設定を確認します")
async def show_settings(interaction: discord.Interaction):
    await interaction.response.send_message(
        "⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**",
        ephemeral=True
    )

@client.tree.command(name="グラフ", description="自分の成長グラフを生成します")
@app_commands.describe(期間="例：「5月」「春シーズン」など（空欄で全期間表示）")
async def graph(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()

    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(期間, season_year, current_season_type)

    all_d = await get_all_records()

    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return

    recs = all_d[interaction.user.id]['records']

    if not ia:
        if tm:
            recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
        else:
            recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]

    if not recs:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    indices = list(range(len(recs)))
    xps = [r['xp'] for r in recs]

    ax.plot(indices, xps, marker='o', color='#1f77b4', linewidth=1.5, markersize=5)

    labels = [r['time'].strftime('%m/%d %H:%M') for r in recs]
    plt.xticks(indices, labels, rotation=90, fontsize=9)

    ax.set_title(f"{interaction.user.display_name} さんの成長記録 ({title})", fontsize=15)
    ax.grid(True, linestyle='--', alpha=0.6)

    plt.tight_layout()

    fname = f'g_{interaction.user.id}_{int(time.time())}.png'

    try:
        plt.savefig(fname)
        plt.close()
        await interaction.followup.send(file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)

@client.tree.command(name="比較グラフ", description="メンバー全員、または指定した人を重ねて比較します")
@app_commands.describe(
    相手1="比較したい相手1",
    相手2="比較したい相手2",
    相手3="比較したい相手3",
    期間="「5月」など（空欄で全員表示）"
)
async def comp_graph(
    interaction: discord.Interaction,
    相手1: discord.Member = None,
    相手2: discord.Member = None,
    相手3: discord.Member = None,
    期間: str = None
):
    await interaction.response.defer()

    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(期間, season_year, current_season_type)

    all_d = await get_all_records()

    targets = [interaction.user.id]

    if 相手1:
        targets.append(相手1.id)
    if 相手2:
        targets.append(相手2.id)
    if 相手3:
        targets.append(相手3.id)

    targets = list(set(targets))

    is_all = (len(targets) == 1)

    if is_all:
        target_ids = list(all_d.keys())
    else:
        target_ids = targets

    fig, ax = plt.subplots(figsize=(12, 6))

    plot_data = []
    max_time = None

    for uid in target_ids:
        if uid not in all_d:
            continue

        info = all_d[uid]
        recs = info['records']

        if not ia and not is_continuous:
            if tm:
                recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
            else:
                recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]

        if recs:
            plot_data.append((info['name'], recs))

            if max_time is None or recs[-1]['time'] > max_time:
                max_time = recs[-1]['time']

    if not plot_data:
        plt.close()
        await interaction.followup.send("⚠️ 比較するデータがありません。")
        return

    for name, recs in plot_data:
        times = [r['time'] for r in recs]
        xps = [r['xp'] for r in recs]

        line, = ax.plot(times, xps, marker='o', linewidth=1.5, markersize=4, label=name)

        if max_time and times[-1] < max_time:
            ax.plot(
                [times[-1], max_time],
                [xps[-1], xps[-1]],
                color=line.get_color(),
                linewidth=1.5,
                marker=''
            )

    if not ia and not is_continuous:
        start_bounds, end_bounds = get_graph_bounds(ty, ts, tm)
        if start_bounds and end_bounds:
            ax.set_xlim(start_bounds, end_bounds)

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))

    plt.xticks(rotation=90, fontsize=9)

    graph_title = "みんなのXP比較グラフ" if is_all else "指定メンバーのXP比較グラフ"
    ax.set_title(f"{graph_title} ({title})", fontsize=15)

    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='upper left', bbox_to_anchor=(1, 1))

    plt.tight_layout()

    fname = f'comp_{interaction.user.id}_{int(time.time())}.png'

    try:
        plt.savefig(fname)
        plt.close()
        await interaction.followup.send(file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)

@client.tree.command(name="ランキング", description="XPランキングを表示します")
@app_commands.describe(期間="例：「5月」「春シーズン」など（空欄で全期間表示）")
async def ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()

    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, season_year, current_season_type)

    all_d = await get_all_records()
    ranking_list = []

    for uid, info in all_d.items():
        recs = info['records']

        if not ia:
            if tm:
                recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
            else:
                recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]

        if recs:
            ranking_list.append((info['name'], recs[-1]['xp']))

    if not ranking_list:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return

    ranking_list.sort(key=lambda x: x[1], reverse=True)

    res = f"🏆 **{title} ランキング** 🏆\n\n"

    for i, (name, xp) in enumerate(ranking_list[:10]):
        medal = ['🥇', '🥈', '🥉'][i] if i < 3 else f"**{i + 1}位**"
        res += f"{medal}：{name} ({xp} XP)\n"

    await interaction.followup.send(res)

@client.tree.command(name="表彰式", description="シーズン終了直後の表彰式を行います")
async def award(interaction: discord.Interaction):
    await interaction.response.defer()

    now = datetime.now(JST)

    if not ((now.month in [3, 6, 9, 12]) and (1 <= now.day <= 7)):
        await interaction.followup.send('⚠️ 表彰式はシーズン終了直後の1週間限定です！')
        return

    target_year, target_s_type = get_previous_season_for_award(now)
    target_season_str = f"{target_year}年 {target_s_type}"

    all_d = await get_all_records()

    most_played = []
    last_spurt = []

    for uid, info in all_d.items():
        s_recs = [r for r in info['records'] if r['season'] == target_season_str]

        if not s_recs:
            continue

        most_played.append((info['name'], len(s_recs)))

        if len(s_recs) >= 2:
            base = s_recs[0]

            for r in reversed(s_recs):
                if r['time'] <= s_recs[-1]['time'] - timedelta(days=7):
                    base = r
                    break

            last_spurt.append((info['name'], s_recs[-1]['xp'] - base['xp']))
        else:
            last_spurt.append((info['name'], 0))

    if not most_played:
        await interaction.followup.send("⚠️ 表彰データがありません。")
        return

    most_played.sort(key=lambda x: x[1], reverse=True)
    last_spurt.sort(key=lambda x: x[1], reverse=True)

    await interaction.followup.send(
        f"🎉 **{target_season_str} 表彰式** 🎉\n\n"
        f"🦑 **一番潜ったで賞**: {most_played[0][0]}さん ({most_played[0][1]}回)\n"
        f"🔥 **ラストスパート賞**: {last_spurt[0][0]}さん (+{last_spurt[0][1]} XP)"
    )

@client.tree.command(name="リセット", description="自分の直近1件のXP記録を取り消します")
async def reset_last(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    log_channel = client.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return

    deleted = False

    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user and m_log.content.startswith(f"{interaction.user.id}|"):
            await m_log.delete()
            deleted = True
            break

    await interaction.followup.send(
        "🗑️ 直近の記録を1件リセットしました！"
        if deleted
        else "⚠️ 削除対象がありません。"
    )

@client.tree.command(name="マイデータ全削除", description="自分のこれまでの全データを消去します")
@app_commands.describe(確認="本当に削除する場合は DELETE と入力")
async def delete_my_data(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)

    if 確認 != "DELETE":
        await interaction.followup.send("⚠️ 確認文字列が違います。自分の全データを削除するには `DELETE` と入力してください。")
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return

    deleted_count = 0

    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user and m_log.content.startswith(f"{interaction.user.id}|"):
            await m_log.delete()
            deleted_count += 1

    await interaction.followup.send(f"✅ あなたの過去データ {deleted_count} 件をすべて完全に消去しました！")

@client.tree.command(name="メンバーデータ削除", description="【管理者専用】指定したメンバーの全データを削除します")
@app_commands.describe(
    対象="データを削除するメンバー",
    確認="本当に削除する場合は RESET と入力"
)
async def delete_member_data(interaction: discord.Interaction, 対象: discord.Member, 確認: str):
    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return

    if 確認 != "RESET":
        await interaction.followup.send("⚠️ 確認文字列が違います。削除するには `RESET` と入力してください。")
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return

    deleted_count = 0

    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user and m_log.content.startswith(f"{対象.id}|"):
            await m_log.delete()
            deleted_count += 1

    await interaction.followup.send(
        f"🚨 管理者権限：{対象.display_name}さんのデータ {deleted_count} 件を完全に消去しました！"
    )

@client.tree.command(name="全員のデータ強制リセット", description="【管理者専用】サーバー内の全データを初期化します")
@app_commands.describe(確認="本当に全削除する場合は RESET と入力")
async def reset_all_data(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return

    if 確認 != "RESET":
        await interaction.followup.send("⚠️ 確認文字列が違います。全削除するには `RESET` と入力してください。")
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return

    deleted_count = 0

    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user:
            await m_log.delete()
            deleted_count += 1

    await interaction.followup.send(
        f"🚨 管理者権限：システムの全XPデータ（計 {deleted_count} 件）を完全に初期化しました！"
    )

# ==================== メッセージイベント ====================

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    curr_season_full_str = f"{season_year}年 {current_season_type}"

    if message.channel.id == TARGET_CHANNEL_ID:
        match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)

        if match:
            new_xp = int(match.group(1) or match.group(2))

            if not (500 <= new_xp < 5000):
                await message.channel.send("⚠️ パワーは500〜5000で入力してください！")
                return

            all_d = await get_all_records()

            current_season_xps = {}

            for uid, info in all_d.items():
                s_recs = [r for r in info['records'] if r['season'] == curr_season_full_str]

                if s_recs:
                    current_season_xps[uid] = (info['name'], s_recs[-1]['xp'])

            old_xp = current_season_xps.get(message.author.id, (message.author.display_name, None))[1]

            if message.author.id in all_d and all_d[message.author.id]['records']:
                last_xp = all_d[message.author.id]['records'][-1]['xp']

                if abs(new_xp - last_xp) > 500:
                    await message.channel.send(
                        f"⚠️ 前回記録({last_xp} XP)から±500以上の急激な増減があるため保存できません！"
                    )
                    return

            log_channel = client.get_channel(LOG_CHANNEL_ID)

            if log_channel:
                splat_time = parse_specified_time(message.content, now)
                is_confident = True

                if not splat_time:
                    splat_time = await update_and_get_last_area_time(now)

                if not splat_time:
                    splat_time = get_last_splat_end_time(now)
                    is_confident = False

                await log_channel.send(
                    f"{message.author.id}|{message.author.display_name}|{new_xp}|"
                    f"{splat_time.strftime('%Y/%m/%d %H:%M')}|{curr_season_full_str}|{message.id}"
                )

                updated_xps = current_season_xps.copy()
                updated_xps[message.author.id] = (message.author.display_name, new_xp)

                passed_users = []
                overtaken_users = []

                for uid, (name, xp) in current_season_xps.items():
                    if uid == message.author.id:
                        continue

                    if (old_xp is not None and xp >= old_xp and new_xp > xp) or (old_xp is None and new_xp > xp):
                        passed_users.append(name)

                    if old_xp is not None and xp < old_xp and new_xp < xp:
                        overtaken_users.append(name)

                sorted_ranking = sorted(updated_xps.items(), key=lambda x: x[1][1], reverse=True)

                my_index = -1

                for idx, (uid, _) in enumerate(sorted_ranking):
                    if uid == message.author.id:
                        my_index = idx
                        break

                drama_msg = ""

                if BOT_SETTINGS.get("drama_enabled", True):
                    if passed_users:
                        names_str = "、".join(passed_users)
                        drama_msg += random.choice([
                            f"\n⚔️ **【下剋上】** {names_str}さんをブチ抜きました！後ろに気をつけてくださいね〜？😜",
                            f"\n🔥 **【ジャイアントキリング】** {names_str}さんを抜き去りました！ナイス精神攻撃！"
                        ])
                    elif overtaken_users:
                        names_str = "、".join(overtaken_users)
                        drama_msg += random.choice([
                            f"\n😱 **【悲報】** {names_str}さんに抜かされてしまいました…悔しくないんか！？さっさと取り返しましょう！💥",
                            f"\n📉 **【煽り運転感知】** {names_str}さんにスマートにパスされました。悔しさをバネに次、潜りましょう！"
                        ])

                    if my_index == 0:
                        drama_msg += "\n👑 **現在トップ独走中！** このまま連勝して逃げ切りましょう！"

                        if len(sorted_ranking) > 1:
                            _, (next_name, next_xp) = sorted_ranking[1]
                            drama_msg += f"（2位の{next_name}さんとは **XP {new_xp - next_xp}** 差）"
                    else:
                        _, (above_name, above_xp) = sorted_ranking[my_index - 1]
                        diff_above = above_xp - new_xp

                        if diff_above == 0:
                            drama_msg += f"\n🔥 1つ上の{above_name}さんと **完全にXPが並びました！** 次の1勝で一気に引き離そう！"
                        elif diff_above <= 30:
                            drama_msg += random.choice([
                                f"\n🎯 {above_name}さんまであと **XP {diff_above}**！背中が見えたぞ、突撃ーー！🚀",
                                f"\n✨ {above_name}さんまであと **XP {diff_above}**！もう完全に射程圏内です！"
                            ])
                        else:
                            drama_msg += f"\n🎯 1つ上の{above_name}さんまであと **XP {diff_above}**！一歩ずつ距離を詰めよう！"

                start_time = splat_time - timedelta(hours=2)
                notice = f"（記録枠：{start_time.strftime('%m/%d %H:%M')}-{splat_time.strftime('%H:%M')}）"

                if not is_confident:
                    notice += "\n💡 ※時間が違った場合は、チャットを編集して『17:00』のように終了時間を書き足してください！"

                area_msg = ""

                if BOT_SETTINGS.get("area_notice_enabled", True):
                    next_area = await get_next_area_shift(now)

                    if next_area:
                        ns = next_area["start"]
                        ne = next_area["end"]
                        stages = next_area["stages"]

                        stage_text = " / ".join(stages) if stages else "ステージ情報なし"

                        area_msg = (
                            f"\n\n🗓️ **次のガチエリア**\n"
                            f"**{ns.strftime('%m/%d %H:%M')} - {ne.strftime('%H:%M')}**\n"
                            f"🗺️ ステージ：**{stage_text}**"
                        )
                    else:
                        area_msg = "\n\n🗓️ **次のガチエリア**\n現在、次回エリア情報を取得できませんでした。"

                await message.channel.send(
                    f"✅ {new_xp} XP を保存しました！{notice}{drama_msg}{area_msg}"
                )

@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)

    if not log_channel:
        return

    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user:
            p = m_log.content.split('|')

            if len(p) >= 6 and str(payload.message_id) == p[5]:
                await m_log.delete()
                break

@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)
    target_channel = client.get_channel(TARGET_CHANNEL_ID)

    content = payload.data.get('content')

    if not log_channel or not content:
        return

    match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', content, re.IGNORECASE)

    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user:
            p = m_log.content.split('|')

            if len(p) >= 6 and str(payload.message_id) == p[5]:
                if match:
                    new_xp = int(match.group(1) or match.group(2))

                    # 編集時もXPチェック
                    if not (500 <= new_xp < 5000):
                        if target_channel:
                            await target_channel.send("⚠️ 編集後のパワーも500〜5000で入力してください！")
                        return

                    p[2] = str(new_xp)

                    spec_time = parse_specified_time(content, datetime.now(JST))

                    if spec_time:
                        p[3] = spec_time.strftime('%Y/%m/%d %H:%M')

                        if target_channel:
                            start_time = spec_time - timedelta(hours=2)
                            time_range_str = f"{start_time.strftime('%H:%M')}ー{spec_time.strftime('%H:%M')}"
                            await target_channel.send(f"🔄 記録枠を **{time_range_str}** に変更しました！")

                    await m_log.edit(content="|".join(p))
                else:
                    await m_log.delete()

                break

client.run(TOKEN)
