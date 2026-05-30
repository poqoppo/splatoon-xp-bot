import discord
from discord import app_commands
import re
import os
import time
import math
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
def home(): return "I am alive!"
t = Thread(target=lambda: app.run(host='0.0.0.0', port=10000)); t.start()

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
TARGET_CHANNEL_ID = 1474973509217423401 # ★XP報告するチャンネルID
LOG_CHANNEL_ID = 1508739566138294522    # ★バックアップ用チャンネルID
ADMIN_USERS = ["poqoppo", "ricekei"]    # ★全削除・特定削除ができる管理者リスト
# ====================================================

class XPClient(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

intents = discord.Intents.default()
intents.message_content = True
client = XPClient(intents=intents)

# 日本時間 (JST) の設定
JST = timezone(timedelta(hours=+9), 'JST')

# ---------------------------------------------------------
# ★新機能: 実際のXマッチ（ガチエリア）のスケジュールを取得する機能
# ---------------------------------------------------------
def fetch_splatoon_schedule():
    url = "https://splatoon3.ink/data/schedules.json"
    req = urllib.request.Request(url, headers={'User-Agent': 'XP-Bot/1.0'})
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read().decode())

async def get_real_area_time(now_dt):
    try:
        data = await asyncio.to_thread(fetch_splatoon_schedule)
        x_schedules = data.get("data", {}).get("xSchedules", {}).get("nodes", [])
        area_shifts = []
        for node in x_schedules:
            if not node or not node.get("startTime") or not node.get("rule"): continue
            if node["rule"]["rule"] == "AREA": # ガチエリアを抽出
                st = datetime.strptime(node["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                et = datetime.strptime(node["endTime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                area_shifts.append((st, et))
        
        area_shifts.sort(key=lambda x: x[0])
        best_et = None
        # 「現在時刻」以前に始まった一番新しいエリア枠を探す
        for st, et in area_shifts:
            if st <= now_dt:
                best_et = et
        
        if best_et:
            return best_et.astimezone(JST) # その枠の「終了時刻」を返す
    except Exception as e:
        print(f"API Fetch Error: {e}")
    return None

# 万が一APIが落ちていた場合の予備ロジック（偶数時間に丸める）
def fallback_splat_time(dt):
    base_midnight = datetime(dt.year, dt.month, dt.day, 0, 0, tzinfo=JST)
    seconds_from_midnight = (dt - base_midnight).total_seconds()
    blocks = round(seconds_from_midnight / 7200)
    return base_midnight + timedelta(seconds=blocks * 7200)
# ---------------------------------------------------------

def parse_args_from_str(text, current_year, current_season_type):
    if not text: text = ""
    is_continuous = "通し" in text or "やった日から" in text
    
    target_year = str(current_year)
    year_match = re.search(r'([0-9]{4})年', text)
    if year_match: target_year = year_match.group(1)
        
    month_match = re.search(r'([0-9]{1,2})月', text)
    season_match = None
    for s in ["春シーズン", "夏シーズン", "秋シーズン", "冬シーズン", "春", "夏", "秋", "冬"]:
        if s in text:
            season_match = s if "シーズン" in s else f"{s}シーズン"
            break
            
    if month_match:
        return target_year, None, int(month_match.group(1)), False, f"{target_year}年 {int(month_match.group(1))}月", is_continuous
    elif season_match:
        return target_year, season_match, None, False, f"{target_year}年 {season_match}", is_continuous
    else:
        return target_year, None, None, True, "全期間", is_continuous

def get_graph_bounds(year_str, season_str, month_int):
    y = int(year_str)
    if month_int:
        start = datetime(y, month_int, 1, 0, 0, tzinfo=JST)
        if month_int == 12: end = datetime(y + 1, 1, 1, 0, 0, tzinfo=JST)
        else: end = datetime(y, month_int + 1, 1, 0, 0, tzinfo=JST)
        return start, end
    if season_str:
        if "春" in season_str: start, end = datetime(y, 3, 1, 0, 0, tzinfo=JST), datetime(y, 6, 1, 0, 0, tzinfo=JST)
        elif "夏" in season_str: start, end = datetime(y, 6, 1, 0, 0, tzinfo=JST), datetime(y, 9, 1, 0, 0, tzinfo=JST)
        elif "秋" in season_str: start, end = datetime(y, 9, 1, 0, 0, tzinfo=JST), datetime(y, 12, 1, 0, 0, tzinfo=JST)
        elif "冬" in season_str: start, end = datetime(y, 12, 1, 0, 0, tzinfo=JST), datetime(y + 1, 3, 1, 0, 0, tzinfo=JST)
        return start, end
    return None, None

async def get_all_records():
    channel = client.get_channel(LOG_CHANNEL_ID)
    data = {} 
    if not channel: return data
    async for message in channel.history(limit=5000, oldest_first=True):
        if message.author == client.user:
            p = message.content.split('|')
            if len(p) >= 4:
                uid = int(p[0])
                if len(p) >= 6:
                    uname, xp, t_str, season, msg_id = p[1], int(p[2]), p[3], p[4], int(p[5])
                else:
                    uname, xp, t_str, season, msg_id = f"ID:{uid}", int(p[1]), p[2], p[3], 0
                
                try: dt = datetime.strptime(t_str, '%Y/%m/%d %H:%M').replace(tzinfo=JST)
                except ValueError:
                    try: dt = datetime.strptime(t_str, '%Y/%m/%d').replace(tzinfo=JST)
                    except ValueError: dt = message.created_at.astimezone(JST)

                if uid not in data: data[uid] = {'name': uname, 'records': []}
                if uname != f"ID:{uid}": data[uid]['name'] = uname
                data[uid]['records'].append({'xp': xp, 'time': dt, 'season': season, 'msg_id': msg_id})
    for uid in data: data[uid]['records'].sort(key=lambda x: x['time'])
    return data

@client.event
async def on_ready():
    print(f'{client.user} がAPIスケジュール同期モードで起動しました！')

# ==================== スラッシュコマンド群 ====================

@client.tree.command(name="グラフ", description="自分の成長グラフを生成します")
@app_commands.describe(期間="例：「5月」「春シーズン」「通し」など（空欄で全期間）")
async def graph(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    current_season_type = "春シーズン" if now.month in [3,4,5] else "夏シーズン" if now.month in [6,7,8] else "秋シーズン" if now.month in [9,10,11] else "冬シーズン"
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(期間, now.year, current_season_type)
    
    all_d = await get_all_records()
    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return
    
    recs = all_d[interaction.user.id]['records']
    if not ia:
        if tm: recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
        else: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
    
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
    plt.savefig(fname); plt.close()
    await interaction.followup.send(file=discord.File(fname))


@client.tree.command(name="比較グラフ", description="メンバー全員、または指定した人を重ねて比較します")
@app_commands.describe(相手1="比較したい相手1", 相手2="比較したい相手2", 相手3="比較したい相手3", 期間="「5月」「通し」など（空欄で全員の全期間）")
async def comp_graph(interaction: discord.Interaction, 相手1: discord.Member = None, 相手2: discord.Member = None, 相手3: discord.Member = None, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    current_season_type = "春シーズン" if now.month in [3,4,5] else "夏シーズン" if now.month in [6,7,8] else "秋シーズン" if now.month in [9,10,11] else "冬シーズン"
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(期間, now.year, current_season_type)
    all_d = await get_all_records()
    
    targets = [interaction.user.id]
    if 相手1: targets.append(相手1.id)
    if 相手2: targets.append(相手2.id)
    if 相手3: targets.append(相手3.id)
    targets = list(set(targets))
    
    is_all = (len(targets) == 1)
    if is_all: target_ids = list(all_d.keys())
    else: target_ids = targets
        
    fig, ax = plt.subplots(figsize=(12, 6))
    plot_data, max_time = [], None
    
    for uid in target_ids:
        if uid not in all_d: continue
        info = all_d[uid]; recs = info['records']
        if not ia and not is_continuous:
            if tm: recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
            else: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
        if recs:
            plot_data.append((info['name'], recs))
            if max_time is None or recs[-1]['time'] > max_time: max_time = recs[-1]['time']
    
    if not plot_data:
        await interaction.followup.send(f"⚠️ 比較するデータがありません。")
        return
    
    for name, recs in plot_data:
        times = [r['time'] for r in recs]
        xps = [r['xp'] for r in recs]
        line, = ax.plot(times, xps, marker='o', linewidth=1.5, markersize=4, label=name)
        if max_time and times[-1] < max_time:
            ax.plot([times[-1], max_time], [xps[-1], xps[-1]], color=line.get_color(), linewidth=1.5, marker='')

    if not ia and not is_continuous:
        start_bounds, end_bounds = get_graph_bounds(ty, ts, tm)
        if start_bounds and end_bounds: ax.set_xlim(start_bounds, end_bounds)

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    plt.xticks(rotation=90, fontsize=9) 
    graph_title = "みんなのXP比較グラフ" if is_all else "指定メンバーのXP比較グラフ"
    ax.set_title(f"{graph_title} ({title})", fontsize=15)
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='upper left', bbox_to_anchor=(1, 1))
    plt.tight_layout()
    
    fname = f'comp_{interaction.user.id}_{int(time.time())}.png'
    plt.savefig(fname); plt.close()
    await interaction.followup.send(file=discord.File(fname))


@client.tree.command(name="ランキング", description="XPランキングを表示します")
@app_commands.describe(期間="「5月」「春」など（空欄で全期間）")
async def ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    current_season_type = "春シーズン" if now.month in [3,4,5] else "夏シーズン" if now.month in [6,7,8] else "秋シーズン" if now.month in [9,10,11] else "冬シーズン"
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, now.year, current_season_type)
    all_d = await get_all_records()
    ranking_list = []
    
    for uid, info in all_d.items():
        recs = info['records']
        if not ia:
            if tm: recs = [r for r in recs if r['time'].year == int(ty) and r['time'].month == tm]
            else: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
        if recs: ranking_list.append((info['name'], recs[-1]['xp']))
        
    if not ranking_list:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
        
    ranking_list.sort(key=lambda x: x[1], reverse=True)
    res = f"🏆 **{title} ランキング** 🏆\n\n"
    for i, (name, xp) in enumerate(ranking_list[:10]): 
        medal = ['🥇','🥈','🥉'][i] if i<3 else f"**{i+1}位**"
        res += f"{medal}：{name} ({xp} XP)\n"
    await interaction.followup.send(res)


@client.tree.command(name="表彰式", description="シーズン終了直後の表彰式を行います")
async def award(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.now(JST)
    if not ((now.month in [3, 6, 9, 12]) and (1 <= now.day <= 7)):
        await interaction.followup.send('⚠️ 表彰式はシーズン終了直後の1週間限定です！')
        return
    target_s_type = {"3": "冬シーズン", "6": "春シーズン", "9": "夏シーズン", "12": "秋シーズン"}.get(str(now.month))
    target_season_str = f"{now.year}年 {target_s_type}"
    all_d = await get_all_records(); most_played, last_spurt = [], []
    for uid, info in all_d.items():
        s_recs = [r for r in info['records'] if r['season'] == target_season_str]
        if not s_recs: continue
        most_played.append((info['name'], len(s_recs)))
        if len(s_recs) >= 2:
            base = s_recs[0]
            for r in reversed(s_recs):
                if r['time'] <= s_recs[-1]['time'] - timedelta(days=7): base = r; break
            last_spurt.append((info['name'], s_recs[-1]['xp'] - base['xp']))
        else: last_spurt.append((info['name'], 0))
        
    if not most_played:
        await interaction.followup.send("⚠️ 表彰データがありません。")
        return
    most_played.sort(key=lambda x: x[1], reverse=True); last_spurt.sort(key=lambda x: x[1], reverse=True)
    await interaction.followup.send(f"🎉 **{target_season_str} 表彰式** 🎉\n\n🦑 **一番潜ったで賞**: {most_played[0][0]}さん ({most_played[0][1]}回)\n🔥 **ラストスパート賞**: {last_spurt[0][0]}さん (+{last_spurt[0][1]} XP)")


@client.tree.command(name="リセット", description="自分の直近1件のXP記録を取り消します")
async def reset_last(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    deleted = False
    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user and m_log.content.startswith(f"{interaction.user.id}|"):
            await m_log.delete(); deleted = True; break
    await interaction.followup.send("🗑️ 直近の記録を1件リセットしました！" if deleted else "⚠️ 削除対象がありません。")


@client.tree.command(name="マイデータ全削除", description="自分のこれまでの全データを消去します")
async def delete_my_data(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    deleted_count = 0
    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user and m_log.content.startswith(f"{interaction.user.id}|"):
            await m_log.delete()
            deleted_count += 1
    await interaction.followup.send(f"✅ あなたの過去データ {deleted_count} 件をすべて完全に消去しました！")


@client.tree.command(name="メンバーデータ削除", description="【管理者専用】指定したメンバーの全データを削除します")
@app_commands.describe(対象="データを削除するメンバー")
async def delete_member_data(interaction: discord.Interaction, 対象: discord.Member):
    await interaction.response.defer()
    if interaction.user.name not in ADMIN_USERS:
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
        
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    deleted_count = 0
    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user and m_log.content.startswith(f"{対象.id}|"):
            await m_log.delete()
            deleted_count += 1
    await interaction.followup.send(f"🚨 管理者権限：{対象.display_name}さんのデータ {deleted_count} 件を完全に消去しました！")


@client.tree.command(name="全員のデータ強制リセット", description="【管理者専用】サーバー内の全データを初期化します")
async def reset_all_data(interaction: discord.Interaction):
    await interaction.response.defer()
    if interaction.user.name not in ADMIN_USERS:
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
        
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    deleted_count = 0
    async for m_log in log_channel.history(limit=5000):
        if m_log.author == client.user:
            await m_log.delete()
            deleted_count += 1
    await interaction.followup.send(f"🚨 管理者権限：システムの全XPデータ（計 {deleted_count} 件）を完全に初期化しました！")


# ==================== メッセージイベント（XP登録・自動連動） ====================

@client.event
async def on_message(message):
    if message.author == client.user: return
    now = datetime.now(JST)
    current_season_type = "春シーズン" if now.month in [3,4,5] else "夏シーズン" if now.month in [6,7,8] else "秋シーズン" if now.month in [9,10,11] else "冬シーズン"
    curr_season_full_str = f"{now.year}年 {current_season_type}"

    if message.channel.id == TARGET_CHANNEL_ID:
        match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000):
                await message.channel.send("⚠️ パワーは500〜5000で入力してください！"); return
            
            all_d = await get_all_records()
            current_season_xps = {}
            for uid, info in all_d.items():
                s_recs = [r for r in info['records'] if r['season'] == curr_season_full_str]
                if s_recs: current_season_xps[uid] = (info['name'], s_recs[-1]['xp'])
            
            old_xp = current_season_xps.get(message.author.id, (message.author.display_name, None))[1]
            
            if message.author.id in all_d and all_d[message.author.id]['records']:
                last_xp = all_d[message.author.id]['records'][-1]['xp']
                if abs(new_xp - last_xp) > 500:
                    await message.channel.send(f"⚠️ 前回記録({last_xp} XP)から±500以上の急激な増減があるため保存できません！"); return
            
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                # ★APIから時間を取得して同期
                splat_time = await get_real_area_time(now)
                if not splat_time:
                    splat_time = fallback_splat_time(now) # API取得失敗時の保険
                
                await log_channel.send(f"{message.author.id}|{message.author.display_name}|{new_xp}|{splat_time.strftime('%Y/%m/%d %H:%M')}|{curr_season_full_str}|{message.id}")
                
                updated_xps = current_season_xps.copy()
                updated_xps[message.author.id] = (message.author.display_name, new_xp)
                passed_users, overtaken_users = [], []
                
                for uid, (name, xp) in current_season_xps.items():
                    if uid == message.author.id: continue
                    if (old_xp is not None and xp >= old_xp and new_xp > xp) or (old_xp is None and new_xp > xp):
                        passed_users.append(name)
                    if old_xp is not None and xp < old_xp and new_xp < xp:
                        overtaken_users.append(name)
                
                sorted_ranking = sorted(updated_xps.items(), key=lambda x: x[1][1], reverse=True)
                my_index = -1
                for idx, (uid, _) in enumerate(sorted_ranking):
                    if uid == message.author.id:
                        my_index = idx; break
                
                drama_msg = ""
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
                    drama_msg += f"\n👑 **現在トップ独走中！** このまま連勝して逃げ切りましょう！"
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
                
                await message.channel.send(f"✅ {new_xp} XP を保存しました！（記録枠：{splat_time.strftime('%m/%d %H:%M')}）{drama_msg}")

@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID: return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user:
            p = m_log.content.split('|')
            if len(p) >= 6 and str(payload.message_id) == p[5]:
                await m_log.delete(); break

@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id != TARGET_CHANNEL_ID: return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    content = payload.data.get('content')
    if not log_channel or not content: return
    match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', content, re.IGNORECASE)
    
    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user:
            p = m_log.content.split('|')
            if len(p) >= 6 and str(payload.message_id) == p[5]:
                if match:
                    new_xp = int(match.group(1) or match.group(2))
                    if not (500 <= new_xp < 5000): return
                    p[2] = str(new_xp)
                    await m_log.edit(content="|".join(p))
                else:
                    await m_log.delete()
                break

client.run(TOKEN)
