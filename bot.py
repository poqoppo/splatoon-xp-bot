
import discord
from discord import app_commands
import asyncio, io, json, os, random, re, time, urllib.request
from datetime import datetime, timedelta, timezone
from threading import Thread
from flask import Flask
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

app = Flask(__name__)

@app.route("/")
def home():
    return "I am alive!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

Thread(target=run_flask, daemon=True).start()


def setup_font():
    try:
        font_path = "NotoSansCJK.ttc"
        font_url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Regular.ttc"
        if not os.path.exists(font_path):
            urllib.request.urlretrieve(font_url, font_path)
        fm.fontManager.addfont(font_path)
        plt.rcParams["font.family"] = "Noto Sans CJK JP"
    except Exception as e:
        print(f"Font setup warning: {e}")
        plt.rcParams["font.family"] = "sans-serif"

setup_font()

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が設定されていません")

TARGET_CHANNEL_ID = 1474973509217423401
LOG_CHANNEL_ID = 1508739566138294522
ARCHIVE_CHANNEL_ID = 1510291838957912285
ADMIN_USERS = ["poqoppo","ricekei"]
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"drama_enabled": True, "area_notice_enabled": True}
ARCHIVE_THRESHOLD = 4500
ARCHIVE_HISTORY_LIMIT = 5000
ARCHIVE_READ_LIMIT = 1000
DELETE_SLEEP_SECONDS = 0.15
JST = timezone(timedelta(hours=9), "JST")

CACHED_AREA_SHIFTS = set()
CACHED_AREA_DETAILS = []
LAST_SCHEDULE_FETCH = None
SCHEDULE_CACHE_SECONDS = 600
archive_lock = asyncio.Lock()


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

BOT_SETTINGS = load_settings()


def save_settings():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(BOT_SETTINGS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Settings save error: {e}")


def is_admin(user):
    return user.name in ADMIN_USERS


class XPClient(discord.Client):
    def __init__(self, *, intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

intents = discord.Intents.default()
intents.message_content = True
client = XPClient(intents=intents)


def get_current_season(dt):
    y = dt.year
    spring = datetime(y, 3, 1, 9, 0, tzinfo=JST)
    summer = datetime(y, 6, 1, 9, 0, tzinfo=JST)
    autumn = datetime(y, 9, 1, 9, 0, tzinfo=JST)
    winter = datetime(y, 12, 1, 9, 0, tzinfo=JST)
    if spring <= dt < summer:
        return y, "春シーズン"
    if summer <= dt < autumn:
        return y, "夏シーズン"
    if autumn <= dt < winter:
        return y, "秋シーズン"
    if dt >= winter:
        return y, "冬シーズン"
    return y - 1, "冬シーズン"


def get_previous_season_for_award(dt):
    y = dt.year
    if dt.month == 3:
        return y - 1, "冬シーズン"
    if dt.month == 6:
        return y, "春シーズン"
    if dt.month == 9:
        return y, "夏シーズン"
    if dt.month == 12:
        return y, "秋シーズン"
    return y, "不明シーズン"


def parse_api_datetime(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def parse_time_str(text, fallback_created_at=None):
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y/%m/%d").replace(tzinfo=JST)
        except ValueError:
            return fallback_created_at.astimezone(JST) if fallback_created_at else datetime.now(JST)


def make_record_json(user_id, user_name, xp, record_time, season, message_id):
    return json.dumps({
        "type": "xp_record",
        "user_id": int(user_id),
        "user_name": str(user_name),
        "xp": int(xp),
        "time": record_time.strftime("%Y/%m/%d %H:%M"),
        "season": str(season),
        "message_id": int(message_id),
    }, ensure_ascii=False)


def make_goal_json(user_id, user_name, target_xp, season):
    return json.dumps({
        "type": "xp_goal",
        "user_id": int(user_id),
        "user_name": str(user_name),
        "target_xp": int(target_xp),
        "season": str(season),
        "created_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
        "active": True,
    }, ensure_ascii=False)


def parse_log_record(content, fallback_created_at=None):
    try:
        obj = json.loads(content)
        if obj.get("type") != "xp_record":
            return None
        uid = int(obj["user_id"])
        return {
            "user_id": uid,
            "user_name": str(obj.get("user_name", f"ID:{uid}")),
            "xp": int(obj["xp"]),
            "time": parse_time_str(str(obj["time"]), fallback_created_at),
            "season": str(obj["season"]),
            "message_id": int(obj.get("message_id", 0)),
            "raw_type": "json",
        }
    except Exception:
        pass
    try:
        p = content.split("|")
        if len(p) >= 4:
            uid = int(p[0])
            if len(p) >= 6:
                uname, xp, t_str, season, msg_id = p[1], int(p[2]), p[3], p[4], int(p[5])
            else:
                uname, xp, t_str, season, msg_id = f"ID:{uid}", int(p[1]), p[2], p[3], 0
            return {
                "user_id": uid,
                "user_name": uname,
                "xp": xp,
                "time": parse_time_str(t_str, fallback_created_at),
                "season": season,
                "message_id": msg_id,
                "raw_type": "pipe",
            }
    except Exception:
        return None
    return None


def parse_goal_record(content):
    try:
        obj = json.loads(content)
        if obj.get("type") != "xp_goal":
            return None
        uid = int(obj["user_id"])
        return {
            "user_id": uid,
            "user_name": str(obj.get("user_name", f"ID:{uid}")),
            "target_xp": int(obj["target_xp"]),
            "season": str(obj["season"]),
            "created_at": str(obj.get("created_at", "")),
            "active": bool(obj.get("active", True)),
        }
    except Exception:
        return None


def record_to_log_content(record):
    return json.dumps({
        "type": "xp_record",
        "user_id": int(record["user_id"]),
        "user_name": str(record["user_name"]),
        "xp": int(record["xp"]),
        "time": record["time"].strftime("%Y/%m/%d %H:%M"),
        "season": str(record["season"]),
        "message_id": int(record.get("message_id", 0)),
    }, ensure_ascii=False)


def goal_to_log_content(goal):
    return json.dumps({
        "type": "xp_goal",
        "user_id": int(goal["user_id"]),
        "user_name": str(goal["user_name"]),
        "target_xp": int(goal["target_xp"]),
        "season": str(goal["season"]),
        "created_at": str(goal.get("created_at", "")),
        "active": bool(goal.get("active", True)),
    }, ensure_ascii=False)


def record_to_archive_obj(record):
    return {
        "type": "xp_record",
        "user_id": int(record["user_id"]),
        "user_name": str(record["user_name"]),
        "xp": int(record["xp"]),
        "time": record["time"].strftime("%Y/%m/%d %H:%M"),
        "season": str(record["season"]),
        "message_id": int(record.get("message_id", 0)),
    }


def parse_archive_record_obj(obj):
    try:
        if obj.get("type") != "xp_record":
            return None
        uid = int(obj["user_id"])
        return {
            "user_id": uid,
            "user_name": str(obj.get("user_name", f"ID:{uid}")),
            "xp": int(obj["xp"]),
            "time": parse_time_str(str(obj["time"])),
            "season": str(obj["season"]),
            "message_id": int(obj.get("message_id", 0)),
            "raw_type": "archive_json",
        }
    except Exception:
        return None


def make_record_unique_key(record):
    msg_id = int(record.get("message_id", 0))
    if msg_id != 0:
        return f"msg:{msg_id}"
    return f"fallback:{record['user_id']}:{record['time'].strftime('%Y/%m/%d %H:%M')}:{record['xp']}:{record['season']}"


def build_power_change_message(old_xp, new_xp):
    if old_xp is None:
        return random.choice([
            "\n🆕 **初記録！** ここから伝説、始めちゃいますか！",
            "\n🦑 **初陣記録！** まずはここがスタートライン。次は上を見よう！",
            "\n📌 **初登録完了！** さあ、ここから沼……いや成長の始まりです。",
            "\n🎮 **初参戦！** 数字が残った瞬間から、言い訳できない戦いが始まります。",
        ])
    diff = new_xp - old_xp
    if diff > 0:
        if diff >= 100:
            return random.choice([
                f"\n🚀 **爆伸び！** 前回から **+{diff} XP**！今日のあなた、ちょっと人間やめてます。",
                f"\n🔥 **大暴れ成功！** **+{diff} XP** はさすがに強すぎ。敵、泣いてたかも。",
                f"\n🧨 **XP爆破上昇！** **+{diff} XP**！これは完全に勝ち筋を握ってます。",
                f"\n👑 **上振れじゃなく実力説！** **+{diff} XP**、その調子で上位勢を荒らしましょう。",
                f"\n🦈 **捕食モード突入！** **+{diff} XP**。エリアの海で完全に食物連鎖の上側です。",
            ])
        if diff >= 50:
            return random.choice([
                f"\n📈 **かなり良い伸び！** 前回から **+{diff} XP**！その調子、普通にえらい。",
                f"\n⚡ **ナイス上昇！** **+{diff} XP**！今日はエリアの神が味方してる。",
                f"\n🦾 **堅実に強い！** **+{diff} XP**、これはちゃんと積み上げてる人の動き。",
                f"\n🧠 **勝ち方わかってる動き！** **+{diff} XP**。これは偶然じゃなくて理解度です。",
            ])
        return random.choice([
            f"\n✅ **微増ナイス！** **+{diff} XP**！小さくても増えたら勝ちです。",
            f"\n📊 **じわ伸び！** **+{diff} XP**。こういう積み重ねが最後に効くんよ。",
            f"\n🪜 **一歩前進！** **+{diff} XP**。地味だけどちゃんと前に進んでます。",
            f"\n🌱 **ちょい伸び！** **+{diff} XP**。育ってます。水やり継続。",
        ])
    if diff == 0:
        return random.choice([
            "\n🟰 **現状維持！** 減ってないので実質勝ち。たぶん。",
            "\n😐 **変動なし！** 今日はパワーが様子見してます。",
            "\n🧘 **無風！** XP、微動だにせず。メンタル修行回です。",
        ])
    drop = abs(diff)
    if drop >= 150:
        return random.choice([
            f"\n💥 **大事故発生！** 前回から **-{drop} XP**……これはエリアじゃなくて心が割れた。",
            f"\n🫠 **溶けすぎ注意！** **-{drop} XP**。今日はもう水分補給して寝た方がいいかも。",
            f"\n🚑 **緊急搬送レベル！** **-{drop} XP**。XPが敵インクに全身浸かってます。",
            f"\n📉 **急降下！** **-{drop} XP**。でも底を見た者だけが、高く飛べる。たぶん。",
        ])
    if drop >= 80:
        return random.choice([
            f"\n😱 **けっこう痛い！** **-{drop} XP**。でもまだ取り返せる範囲、次で回収しよう。",
            f"\n🧯 **消火活動開始！** **-{drop} XP**。燃えてるけど、まだ鎮火できます。",
            f"\n🪦 **本日の供養対象：XP {drop}**。次のエリアで成仏させましょう。",
            f"\n🥶 **冷えました。** **-{drop} XP**。でも次勝てば手のひら返します。",
        ])
    if drop >= 30:
        return random.choice([
            f"\n😬 **ちょい痛い減少！** **-{drop} XP**。まあ誤差……と言い張りたい。",
            f"\n📉 **少し後退！** **-{drop} XP**。次の勝ちで取り戻せるやつです。",
            f"\n🫡 **XPが少し出張しました。** **-{drop} XP**。すぐ連れ戻しましょう。",
            f"\n🦑 **イカした反省会案件！** **-{drop} XP**。次は塗りで黙らせよう。",
        ])
    return random.choice([
        f"\n🤏 **微減！** **-{drop} XP**。これはノーカンにしたいレベル。",
        f"\n🍃 **ちょい減り！** **-{drop} XP**。風で飛んだだけです。たぶん。",
        f"\n😌 **軽傷！** **-{drop} XP**。まだ全然戦える。",
        f"\n🔁 **小さな揺れ！** **-{drop} XP**。次でプラスに戻しましょう。",
    ])


async def fetch_area_schedule(force=False):
    global CACHED_AREA_SHIFTS, CACHED_AREA_DETAILS, LAST_SCHEDULE_FETCH
    now = datetime.now(JST)
    if not force and LAST_SCHEDULE_FETCH and (now - LAST_SCHEDULE_FETCH).total_seconds() < SCHEDULE_CACHE_SECONDS and CACHED_AREA_DETAILS:
        return
    try:
        req = urllib.request.Request("https://spla3.yuu26.com/api/x/schedule", headers={"User-Agent": "XP-Bot/3.4"})
        res = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
        data = json.loads(res.read().decode())
        for node in data.get("results", []):
            if node.get("rule", {}).get("key") != "AREA":
                continue
            st = parse_api_datetime(node["start_time"])
            et = parse_api_datetime(node["end_time"])
            stages = [s.get("name", "不明ステージ") for s in node.get("stages", [])]
            CACHED_AREA_SHIFTS.add((st, et))
            detail = {"start": st, "end": et, "stages": stages}
            if detail not in CACHED_AREA_DETAILS:
                CACHED_AREA_DETAILS.append(detail)
        CACHED_AREA_DETAILS.sort(key=lambda x: x["start"])
        LAST_SCHEDULE_FETCH = now
    except Exception as e:
        print(f"API Fetch Error: {e}")


async def update_and_get_last_area_time(now_dt):
    await fetch_area_schedule()
    best_et = None
    for st, et in CACHED_AREA_SHIFTS:
        if st <= now_dt and (best_et is None or et > best_et):
            best_et = et
    return best_et


async def get_next_area_shift(now_dt):
    await fetch_area_schedule()
    candidates = [d for d in CACHED_AREA_DETAILS if d["start"] > now_dt]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x["start"])
    return candidates[0]


def get_last_splat_end_time(dt):
    h = dt.hour
    if h % 2 == 0:
        h -= 1
    if h < 0:
        h = 23
        dt = dt - timedelta(days=1)
    return dt.replace(hour=h, minute=0, second=0, microsecond=0)


def parse_specified_time(content, now_dt):
    m = re.search(r"([0-9]{1,2}):([0-9]{2})", content)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2))
        if 0 <= h < 24 and 0 <= minute < 60:
            return now_dt.replace(hour=h, minute=minute, second=0, microsecond=0)
    m = re.search(r"([0-9]{1,2})時", content)
    if m:
        h = int(m.group(1))
        if 0 <= h < 24:
            return now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
    return None


def parse_args_from_str(text, current_year, current_season_type, default_to_current_season=False):
    if not text:
        if default_to_current_season:
            return str(current_year), current_season_type, None, False, f"{current_year}年 {current_season_type}", False
        text = ""
    text = text.strip()
    is_continuous = "通し" in text or "やった日から" in text
    if text in ["全期間", "全部", "all", "ALL"]:
        return str(current_year), None, None, True, "全期間", is_continuous
    target_year = str(current_year)
    year_match = re.search(r"([0-9]{4})年", text)
    if year_match:
        target_year = year_match.group(1)
    month_match = re.search(r"([0-9]{1,2})月", text)
    season_match = None
    for s in ["春シーズン", "夏シーズン", "秋シーズン", "冬シーズン", "春", "夏", "秋", "冬"]:
        if s in text:
            season_match = s if "シーズン" in s else f"{s}シーズン"
            break
    if month_match:
        month = int(month_match.group(1))
        return target_year, None, month, False, f"{target_year}年 {month}月", is_continuous
    if season_match:
        return target_year, season_match, None, False, f"{target_year}年 {season_match}", is_continuous
    if default_to_current_season:
        return str(current_year), current_season_type, None, False, f"{current_year}年 {current_season_type}", is_continuous
    return target_year, None, None, True, "全期間", is_continuous


def get_graph_bounds(year_str, season_str, month_int):
    y = int(year_str)
    if month_int:
        start = datetime(y, month_int, 1, 0, 0, tzinfo=JST)
        end = datetime(y + 1, 1, 1, 0, 0, tzinfo=JST) if month_int == 12 else datetime(y, month_int + 1, 1, 0, 0, tzinfo=JST)
        return start, end
    if season_str:
        if "春" in season_str:
            return datetime(y, 3, 1, 9, 0, tzinfo=JST), datetime(y, 6, 1, 9, 0, tzinfo=JST)
        if "夏" in season_str:
            return datetime(y, 6, 1, 9, 0, tzinfo=JST), datetime(y, 9, 1, 9, 0, tzinfo=JST)
        if "秋" in season_str:
            return datetime(y, 9, 1, 9, 0, tzinfo=JST), datetime(y, 12, 1, 9, 0, tzinfo=JST)
        if "冬" in season_str:
            return datetime(y, 12, 1, 9, 0, tzinfo=JST), datetime(y + 1, 3, 1, 9, 0, tzinfo=JST)
    return None, None


def is_record_in_period(record_time, year_str, season_str=None, month_int=None):
    if month_int:
        return record_time.year == int(year_str) and record_time.month == month_int
    if season_str:
        start, end = get_graph_bounds(year_str, season_str, None)
        return bool(start and end and start <= record_time < end)
    return True


async def get_current_log_records_with_messages():
    channel = client.get_channel(LOG_CHANNEL_ID)
    items = []
    if not channel:
        return items
    async for message in channel.history(limit=ARCHIVE_HISTORY_LIMIT, oldest_first=True):
        if message.author != client.user:
            continue
        record = parse_log_record(message.content, message.created_at)
        if record:
            items.append((message, record))
    return items


async def get_archive_records():
    channel = client.get_channel(ARCHIVE_CHANNEL_ID)
    records = []
    if not channel:
        return records
    async for message in channel.history(limit=ARCHIVE_READ_LIMIT, oldest_first=True):
        if message.author != client.user:
            continue
        for attachment in message.attachments:
            if not (attachment.filename.startswith("xp_archive_") and attachment.filename.endswith(".json")):
                continue
            try:
                raw = await attachment.read()
                obj = json.loads(raw.decode("utf-8"))
                if obj.get("type") != "xp_archive":
                    continue
                for rec_obj in obj.get("records", []):
                    record = parse_archive_record_obj(rec_obj)
                    if record:
                        records.append(record)
            except Exception as e:
                print(f"Archive read error: {attachment.filename}: {e}")
    return records


async def auto_archive_if_needed(force=False):
    async with archive_lock:
        archive_channel = client.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel or not client.get_channel(LOG_CHANNEL_ID):
            return False, 0, "チャンネルが見つかりません"
        items = await get_current_log_records_with_messages()
        count = len(items)
        if count == 0:
            return False, 0, "アーカイブ対象がありません"
        if not force and count < ARCHIVE_THRESHOLD:
            return False, count, "しきい値未満です"
        now = datetime.now(JST)
        records = [record_to_archive_obj(record) for _, record in items]
        archive_obj = {
            "type": "xp_archive",
            "created_at": now.strftime("%Y/%m/%d %H:%M:%S"),
            "record_count": len(records),
            "records": records,
        }
        file_obj = io.BytesIO(json.dumps(archive_obj, ensure_ascii=False, indent=2).encode("utf-8"))
        fname = f"xp_archive_{now.strftime('%Y%m%d_%H%M%S')}_{len(records)}records.json"
        try:
            await archive_channel.send(
                content=f"📦 **XPログ自動アーカイブ**\n件数：**{len(records)}件**\n作成日時：**{now.strftime('%Y/%m/%d %H:%M:%S')}**",
                file=discord.File(file_obj, filename=fname),
            )
        except Exception as e:
            return False, count, f"アーカイブ送信失敗: {e}"
        deleted_count = 0
        for msg, _ in items:
            try:
                await msg.delete()
                deleted_count += 1
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception as e:
                print(f"Archive delete error: {e}")
        return True, deleted_count, "アーカイブ完了"


async def get_all_records():
    data = {}
    seen = set()
    all_records = []
    all_records.extend(await get_archive_records())
    current_items = await get_current_log_records_with_messages()
    all_records.extend([record for _, record in current_items])
    for record in all_records:
        key = make_record_unique_key(record)
        if key in seen:
            continue
        seen.add(key)
        uid = record["user_id"]
        uname = record["user_name"]
        if uid not in data:
            data[uid] = {"name": uname, "records": []}
        if uname != f"ID:{uid}":
            data[uid]["name"] = uname
        data[uid]["records"].append({
            "xp": record["xp"],
            "time": record["time"],
            "season": record["season"],
            "msg_id": record["message_id"],
        })
    for uid in data:
        data[uid]["records"].sort(key=lambda x: x["time"])
    return data


async def get_all_goals():
    channel = client.get_channel(LOG_CHANNEL_ID)
    goals = {}
    if not channel:
        return goals
    async for message in channel.history(limit=ARCHIVE_HISTORY_LIMIT, oldest_first=True):
        if message.author != client.user:
            continue
        goal = parse_goal_record(message.content)
        if goal:
            goals.setdefault(goal["user_id"], {})[goal["season"]] = goal
    return goals


async def get_active_goal(user_id, season):
    goal = (await get_all_goals()).get(user_id, {}).get(season)
    if not goal or not goal.get("active", True):
        return None
    return goal


@client.event
async def on_ready():
    print(f"{client.user} が起動しました！")


@client.tree.command(name="通知設定", description="煽り文章・次のガチエリア表示をON/OFFします")
@app_commands.describe(煽り文章="XP保存後の煽り文章を表示するか", エリア通知="XP保存後に次のガチエリア時間とステージを表示するか")
async def notification_settings(interaction: discord.Interaction, 煽り文章: bool = None, エリア通知: bool = None):
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
        f"⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**"
    )


@client.tree.command(name="設定確認", description="現在のBot設定を確認します")
async def show_settings(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**",
        ephemeral=True,
    )


@client.tree.command(name="ログ件数", description="現在ログとアーカイブ済みログの件数を確認します")
async def log_count(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    current_items = await get_current_log_records_with_messages()
    archive_records = await get_archive_records()
    await interaction.followup.send(
        f"📊 **XPログ件数**\n"
        f"現在ログチャンネル：**{len(current_items)}件**\n"
        f"アーカイブ済み：**{len(archive_records)}件**\n"
        f"合計：**{len(current_items) + len(archive_records)}件**\n"
        f"自動アーカイブしきい値：**{ARCHIVE_THRESHOLD}件**"
    )


@client.tree.command(name="手動アーカイブ", description="【管理者専用】現在ログを手動でアーカイブします")
@app_commands.describe(確認="実行する場合は ARCHIVE と入力")
async def manual_archive(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    if 確認 != "ARCHIVE":
        await interaction.followup.send("⚠️ 確認文字列が違います。実行するには `ARCHIVE` と入力してください。")
        return
    success, count, msg = await auto_archive_if_needed(force=True)
    if success:
        await interaction.followup.send(f"📦 手動アーカイブ完了：**{count}件** を保存して現在ログから削除しました。")
    else:
        await interaction.followup.send(f"⚠️ 手動アーカイブ失敗/未実行：{msg}（対象 {count}件）")


@client.tree.command(name="目標設定", description="現シーズンの目標XPを設定します")
@app_commands.describe(目標xp="目標にするXP。例: 2800")
async def set_goal(interaction: discord.Interaction, 目標xp: int):
    await interaction.response.defer(ephemeral=True)
    if not (500 <= 目標xp < 5000):
        await interaction.followup.send("⚠️ 目標XPは500〜5000で入力してください！")
        return
    now = datetime.now(JST)
    season_year, season_type = get_current_season(now)
    season_full = f"{season_year}年 {season_type}"
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    async for m_log in log_channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        goal = parse_goal_record(m_log.content)
        if goal and goal["user_id"] == interaction.user.id and goal["season"] == season_full and goal.get("active", True):
            goal["active"] = False
            await m_log.edit(content=goal_to_log_content(goal))
    await log_channel.send(make_goal_json(interaction.user.id, interaction.user.display_name, 目標xp, season_full))
    all_d = await get_all_records()
    current_xp = None
    if interaction.user.id in all_d:
        recs = [r for r in all_d[interaction.user.id]["records"] if is_record_in_period(r["time"], str(season_year), season_type, None)]
        if recs:
            current_xp = recs[-1]["xp"]
    if current_xp is None:
        await interaction.followup.send(f"🎯 **{season_full} の目標を設定しました！**\n目標：**{目標xp} XP**\nまだ今シーズンの記録がないので、まずは1回記録しましょう！")
        return
    diff = 目標xp - current_xp
    await interaction.followup.send(
        f"🎯 **{season_full} の目標を設定しました！**\n"
        f"目標：**{目標xp} XP**\n"
        f"現在：**{current_xp} XP**\n"
        + ("✅ もう目標達成済みです。目標、低すぎません？😎" if diff <= 0 else f"あと **{diff} XP**！")
    )


@client.tree.command(name="目標確認", description="現シーズンの目標XPと達成状況を確認します")
async def check_goal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(JST)
    season_year, season_type = get_current_season(now)
    season_full = f"{season_year}年 {season_type}"
    goal = await get_active_goal(interaction.user.id, season_full)
    if not goal:
        await interaction.followup.send(f"⚠️ **{season_full}** の目標がまだ設定されていません。\n`/目標設定` で目標XPを設定できます。")
        return
    all_d = await get_all_records()
    current_xp = None
    best_xp = None
    if interaction.user.id in all_d:
        recs = [r for r in all_d[interaction.user.id]["records"] if is_record_in_period(r["time"], str(season_year), season_type, None)]
        if recs:
            current_xp = recs[-1]["xp"]
            best_xp = max(r["xp"] for r in recs)
    target_xp = goal["target_xp"]
    if current_xp is None:
        await interaction.followup.send(f"🎯 **{season_full} の目標**\n目標：**{target_xp} XP**\n現在：記録なし\nまずはXPを記録しましょう！")
        return
    diff = target_xp - current_xp
    best_text = f"{best_xp} XP" if best_xp is not None else "なし"
    await interaction.followup.send(
        f"🎯 **{season_full} の目標**\n"
        f"目標：**{target_xp} XP**\n"
        f"現在：**{current_xp} XP**\n"
        f"今シーズン最高：**{best_text}**\n"
        + ("✅ **目標達成済み！** 次はもっと高く設定して自分を追い込みましょう😎" if diff <= 0 else f"あと **{diff} XP**！")
    )


@client.tree.command(name="目標削除", description="現シーズンの目標XPを削除します")
@app_commands.describe(確認="削除する場合は DELETE と入力")
async def delete_goal(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if 確認 != "DELETE":
        await interaction.followup.send("⚠️ 目標を削除するには `DELETE` と入力してください。")
        return
    now = datetime.now(JST)
    season_year, season_type = get_current_season(now)
    season_full = f"{season_year}年 {season_type}"
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    deleted = False
    async for m_log in log_channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        goal = parse_goal_record(m_log.content)
        if goal and goal["user_id"] == interaction.user.id and goal["season"] == season_full and goal.get("active", True):
            await m_log.delete()
            deleted = True
            break
    if deleted:
        await interaction.followup.send(f"🗑️ **{season_full}** の目標を削除しました。")
    else:
        await interaction.followup.send(f"⚠️ **{season_full}** の有効な目標が見つかりませんでした。")


@client.tree.command(name="自己ベスト", description="自分の最高XPを表示します")
@app_commands.describe(期間="例：「夏シーズン」「5月」「全期間」など（空欄で現シーズン表示）")
async def personal_best(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, season_year, current_season_type, True)
    all_d = await get_all_records()
    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return
    recs = all_d[interaction.user.id]["records"]
    if not ia:
        recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
    if not recs:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    best = max(recs, key=lambda r: r["xp"])
    await interaction.followup.send(
        f"🏅 **{interaction.user.display_name} さんの自己ベスト**\n"
        f"期間：**{title}**\n"
        f"最高XP：**{best['xp']} XP**\n"
        f"記録枠：**{best['time'].strftime('%Y/%m/%d %H:%M')}**\n"
        f"シーズン：**{best['season']}**"
    )


@client.tree.command(name="伸びランキング", description="指定期間でXPが伸びた人ランキングを表示します")
@app_commands.describe(期間="例：「夏シーズン」「5月」「全期間」など（空欄で現シーズン表示）")
async def growth_ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, season_year, current_season_type, True)
    all_d = await get_all_records()
    growth_list = []
    for uid, info in all_d.items():
        recs = info["records"]
        if not ia:
            recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
        if len(recs) >= 2:
            growth_list.append((info["name"], recs[-1]["xp"] - recs[0]["xp"], recs[0]["xp"], recs[-1]["xp"], len(recs)))
        elif len(recs) == 1:
            growth_list.append((info["name"], 0, recs[0]["xp"], recs[0]["xp"], 1))
    if not growth_list:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    growth_list.sort(key=lambda x: x[1], reverse=True)
    res = f"🔥 **{title} 伸びランキング** 🔥\n\n"
    for i, (name, growth, start_xp, last_xp, count) in enumerate(growth_list[:10]):
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"**{i + 1}位**"
        sign = "+" if growth > 0 else ""
        res += f"{medal}：{name} ({sign}{growth} XP / {start_xp}→{last_xp} / {count}件)\n"
    await interaction.followup.send(res)


@client.tree.command(name="グラフ", description="自分の成長グラフを生成します")
@app_commands.describe(期間="例：「5月」「夏シーズン」「全期間」など（空欄で現シーズン表示）")
async def graph(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, season_year, current_season_type, True)
    all_d = await get_all_records()
    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return
    recs = all_d[interaction.user.id]["records"]
    if not ia:
        recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
    if not recs:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    indices = list(range(len(recs)))
    xps = [r["xp"] for r in recs]
    ax.plot(indices, xps, marker="o", color="#1f77b4", linewidth=1.5, markersize=5)
    plt.xticks(indices, [r["time"].strftime("%m/%d %H:%M") for r in recs], rotation=90, fontsize=9)
    ax.axhline(max(xps), linestyle="--", alpha=0.4)
    ax.set_title(f"{interaction.user.display_name} さんの成長記録 ({title})", fontsize=15)
    ax.grid(True, linestyle="--", alpha=0.6)
    plt.tight_layout()
    fname = f"g_{interaction.user.id}_{int(time.time())}.png"
    try:
        plt.savefig(fname)
        plt.close()
        await interaction.followup.send(file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)


@client.tree.command(name="比較グラフ", description="メンバー全員、または指定した人を重ねて比較します")
@app_commands.describe(相手1="比較したい相手1", 相手2="比較したい相手2", 相手3="比較したい相手3", 期間="例：「5月」「夏シーズン」「全期間」など（空欄で現シーズン表示）")
async def comp_graph(interaction: discord.Interaction, 相手1: discord.Member = None, 相手2: discord.Member = None, 相手3: discord.Member = None, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(期間, season_year, current_season_type, True)
    all_d = await get_all_records()
    targets = [interaction.user.id]
    if 相手1:
        targets.append(相手1.id)
    if 相手2:
        targets.append(相手2.id)
    if 相手3:
        targets.append(相手3.id)
    targets = list(set(targets))
    target_ids = list(all_d.keys()) if len(targets) == 1 else targets
    plot_data = []
    for uid in target_ids:
        if uid not in all_d:
            continue
        recs = all_d[uid]["records"]
        if not ia and not is_continuous:
            recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
        if recs:
            plot_data.append((all_d[uid]["name"], recs))
    if not plot_data:
        await interaction.followup.send("⚠️ 比較するデータがありません。")
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    max_len = max(len(recs) for _, recs in plot_data)
    label_recs = max(plot_data, key=lambda x: len(x[1]))[1]
    for name, recs in plot_data:
        ax.plot(list(range(len(recs))), [r["xp"] for r in recs], marker="o", linewidth=1.5, markersize=4, label=name)
    ax.set_xticks(range(max_len))
    ax.set_xticklabels([r["time"].strftime("%m/%d %H:%M") for r in label_recs], rotation=90, fontsize=9)
    graph_title = "みんなのXP比較グラフ" if len(targets) == 1 else "指定メンバーのXP比較グラフ"
    ax.set_title(f"{graph_title} ({title})", fontsize=15)
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(loc="upper left", bbox_to_anchor=(1, 1))
    plt.tight_layout()
    fname = f"comp_{interaction.user.id}_{int(time.time())}.png"
    try:
        plt.savefig(fname)
        plt.close()
        await interaction.followup.send(file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)


@client.tree.command(name="ランキング", description="XPランキングを表示します")
@app_commands.describe(期間="例：「5月」「夏シーズン」「全期間」など（空欄で現シーズン表示）")
async def ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    ty, ts, tm, ia, title, _ = parse_args_from_str(期間, season_year, current_season_type, True)
    all_d = await get_all_records()
    ranking_list = []
    for uid, info in all_d.items():
        recs = info["records"]
        if not ia:
            recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
        if recs:
            ranking_list.append((info["name"], recs[-1]["xp"]))
    if not ranking_list:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    ranking_list.sort(key=lambda x: x[1], reverse=True)
    res = f"🏆 **{title} ランキング** 🏆\n\n"
    for i, (name, xp) in enumerate(ranking_list[:10]):
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"**{i + 1}位**"
        res += f"{medal}：{name} ({xp} XP)\n"
    await interaction.followup.send(res)


@client.tree.command(name="表彰式", description="シーズン終了直後の表彰式を行います")
async def award(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.now(JST)
    if now.month not in [3, 6, 9, 12]:
        await interaction.followup.send("⚠️ 表彰式はシーズン終了直後の1週間限定です！")
        return
    change_time = datetime(now.year, now.month, 1, 9, 0, tzinfo=JST)
    if not (change_time <= now < change_time + timedelta(days=7)):
        await interaction.followup.send("⚠️ 表彰式はシーズン終了直後の1週間限定です！")
        return
    target_year, target_season = get_previous_season_for_award(now)
    all_d = await get_all_records()
    most_played = []
    last_spurt = []
    for uid, info in all_d.items():
        recs = [r for r in info["records"] if is_record_in_period(r["time"], str(target_year), target_season, None)]
        if not recs:
            continue
        most_played.append((info["name"], len(recs)))
        if len(recs) >= 2:
            base = recs[0]
            for r in reversed(recs):
                if r["time"] <= recs[-1]["time"] - timedelta(days=7):
                    base = r
                    break
            last_spurt.append((info["name"], recs[-1]["xp"] - base["xp"]))
        else:
            last_spurt.append((info["name"], 0))
    if not most_played:
        await interaction.followup.send("⚠️ 表彰データがありません。")
        return
    most_played.sort(key=lambda x: x[1], reverse=True)
    last_spurt.sort(key=lambda x: x[1], reverse=True)
    await interaction.followup.send(
        f"🎉 **{target_year}年 {target_season} 表彰式** 🎉\n\n"
        f"🦑 **一番潜ったで賞**: {most_played[0][0]}さん ({most_played[0][1]}回)\n"
        f"🔥 **ラストスパート賞**: {last_spurt[0][0]}さん (+{last_spurt[0][1]} XP)"
    )


@client.tree.command(name="リセット", description="自分の直近1件のXP記録を取り消します")
async def reset_last(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = client.get_channel(LOG_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    deleted = False
    async for m_log in channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        record = parse_log_record(m_log.content, m_log.created_at)
        if record and record["user_id"] == interaction.user.id:
            await m_log.delete()
            deleted = True
            break
    await interaction.followup.send("🗑️ 直近の未アーカイブ記録を1件リセットしました！" if deleted else "⚠️ 削除対象がありません。すでにアーカイブ済みの記録はこのコマンドでは消せません。")


@client.tree.command(name="マイデータ全削除", description="自分の未アーカイブデータを消去します")
@app_commands.describe(確認="本当に削除する場合は DELETE と入力")
async def delete_my_data(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if 確認 != "DELETE":
        await interaction.followup.send("⚠️ 確認文字列が違います。削除するには `DELETE` と入力してください。")
        return
    channel = client.get_channel(LOG_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    deleted_count = 0
    async for m_log in channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        record = parse_log_record(m_log.content, m_log.created_at)
        if record and record["user_id"] == interaction.user.id:
            await m_log.delete()
            deleted_count += 1
            await asyncio.sleep(DELETE_SLEEP_SECONDS)
    await interaction.followup.send(f"✅ あなたの未アーカイブデータ {deleted_count} 件を消去しました！\n※アーカイブ済みファイル内の過去データは削除されません。")


@client.tree.command(name="メンバーデータ削除", description="【管理者専用】指定メンバーの未アーカイブデータを削除します")
@app_commands.describe(対象="データを削除するメンバー", 確認="本当に削除する場合は RESET と入力")
async def delete_member_data(interaction: discord.Interaction, 対象: discord.Member, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    if 確認 != "RESET":
        await interaction.followup.send("⚠️ 確認文字列が違います。削除するには `RESET` と入力してください。")
        return
    channel = client.get_channel(LOG_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    deleted_count = 0
    async for m_log in channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        record = parse_log_record(m_log.content, m_log.created_at)
        if record and record["user_id"] == 対象.id:
            await m_log.delete()
            deleted_count += 1
            await asyncio.sleep(DELETE_SLEEP_SECONDS)
    await interaction.followup.send(f"🚨 管理者権限：{対象.display_name}さんの未アーカイブデータ {deleted_count} 件を削除しました！\n※アーカイブ済みファイル内の過去データは削除されません。")


@client.tree.command(name="全員のデータ強制リセット", description="【管理者専用】未アーカイブの全データを初期化します")
@app_commands.describe(確認="本当に全削除する場合は RESET と入力")
async def reset_all_data(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    if 確認 != "RESET":
        await interaction.followup.send("⚠️ 確認文字列が違います。全削除するには `RESET` と入力してください。")
        return
    channel = client.get_channel(LOG_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    deleted_count = 0
    async for m_log in channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author == client.user:
            record = parse_log_record(m_log.content, m_log.created_at)
            if record:
                await m_log.delete()
                deleted_count += 1
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
    await interaction.followup.send(f"🚨 管理者権限：未アーカイブXPデータ（計 {deleted_count} 件）を初期化しました！\n※アーカイブ済みファイル内の過去データは削除されません。")


@client.event
async def on_message(message):
    if message.author == client.user or message.channel.id != TARGET_CHANNEL_ID:
        return
    now = datetime.now(JST)
    season_year, current_season_type = get_current_season(now)
    match = re.search(r"xp\s*([0-9]+)|([0-9]+)\s*xp", message.content, re.IGNORECASE)
    if not match:
        return
    new_xp = int(match.group(1) or match.group(2))
    if not (500 <= new_xp < 5000):
        await message.channel.send("⚠️ パワーは500〜5000で入力してください！")
        return
    all_d = await get_all_records()
    personal_best_xp = max([r["xp"] for r in all_d.get(message.author.id, {}).get("records", [])], default=None)
    current_season_xps = {}
    for uid, info in all_d.items():
        recs = [r for r in info["records"] if is_record_in_period(r["time"], str(season_year), current_season_type, None)]
        if recs:
            current_season_xps[uid] = (info["name"], recs[-1]["xp"])
    old_xp = current_season_xps.get(message.author.id, (message.author.display_name, None))[1]

    # 重要：シーズン最初の入力は、前シーズンのXPと比較しません。
    # 同じシーズン内に既に記録がある場合だけ、±500チェックを行います。
    if old_xp is not None:
        if abs(new_xp - old_xp) > 500:
            await message.channel.send(f"⚠️ 今シーズン前回記録({old_xp} XP)から±500以上の急激な増減があるため保存できません！")
            return

    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await message.channel.send("⚠️ ログチャンネルが見つかりません。")
        return
    splat_time = parse_specified_time(message.content, now)
    is_confident = True
    if not splat_time:
        splat_time = await update_and_get_last_area_time(now)
    if not splat_time:
        splat_time = get_last_splat_end_time(now)
        is_confident = False
    record_season_year, record_season_type = get_current_season(splat_time)
    record_season_full_str = f"{record_season_year}年 {record_season_type}"
    await log_channel.send(make_record_json(message.author.id, message.author.display_name, new_xp, splat_time, record_season_full_str, message.id))
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
    my_index = next((i for i, (uid, _) in enumerate(sorted_ranking) if uid == message.author.id), 0)
    goal_msg = ""
    active_goal = await get_active_goal(message.author.id, record_season_full_str)
    if active_goal:
        target_xp = active_goal["target_xp"]
        if new_xp >= target_xp and (old_xp is None or old_xp < target_xp):
            goal_msg += random.choice([
                f"\n🎯 **目標達成！** 目標 **{target_xp} XP** を突破！言い訳なしで強いです。",
                f"\n🏁 **ゴール到達！** **{target_xp} XP** 達成！次はもっと高い壁、置きましょう。",
                f"\n🔥 **目標粉砕！** **{target_xp} XP** を超えました。目標、もう過去のものです。",
                f"\n👑 **目標クリア！** **{target_xp} XP** 到達！これはちゃんと祝っていいやつ。",
            ])
        elif new_xp < target_xp:
            remain = target_xp - new_xp
            if remain <= 30:
                goal_msg += random.choice([
                    f"\n🎯 目標 **{target_xp} XP** まであと **{remain} XP**！もう目の前、逃げるな。",
                    f"\n👀 目標まであと **{remain} XP**。ここまで来たら達成するしかないです。",
                    f"\n🔥 あと **{remain} XP** で目標到達！次の1勝で決めに行きましょう。",
                ])
            elif remain <= 100:
                goal_msg += random.choice([
                    f"\n📍 目標 **{target_xp} XP** まであと **{remain} XP**。射程圏内です。",
                    f"\n🧗 目標まであと **{remain} XP**。登れる壁です、サボらなければ。",
                    f"\n🚀 あと **{remain} XP**。そろそろ本気出す時間です。",
                ])
    drama_msg = ""
    if BOT_SETTINGS.get("drama_enabled", True):
        drama_msg += build_power_change_message(old_xp, new_xp)
        if personal_best_xp is None or new_xp > personal_best_xp:
            drama_msg += random.choice([
                f"\n🏅 **自己ベスト更新！** {new_xp} XP！これは胸張っていいやつ！",
                f"\n🌟 **最高到達点更新！** {new_xp} XP！今のあなたが過去最強です。",
                f"\n👑 **PB更新！** {new_xp} XP！過去の自分、置いてきました。",
                f"\n🚀 **自己最高パワー！** {new_xp} XP！今日は祝っていい日です。",
            ])
        if passed_users:
            names_str = "、".join(passed_users)
            drama_msg += random.choice([
                f"\n⚔️ **【下剋上】** {names_str}さんをブチ抜きました！後ろに気をつけてくださいね〜？😜",
                f"\n🔥 **【ジャイアントキリング】** {names_str}さんを抜き去りました！ナイス精神攻撃！",
                f"\n🦈 **【捕食完了】** {names_str}さんを飲み込みました。ランキングの海は弱肉強食です。",
                f"\n🚗 **【追い越し成功】** {names_str}さんを華麗にパス！ウインカー出す暇もなかった。",
            ])
        elif overtaken_users:
            names_str = "、".join(overtaken_users)
            drama_msg += random.choice([
                f"\n😱 **【悲報】** {names_str}さんに抜かされてしまいました…悔しくないんか！？さっさと取り返しましょう！💥",
                f"\n📉 **【煽り運転感知】** {names_str}さんにスマートにパスされました。悔しさをバネに次、潜りましょう！",
                f"\n🫥 **【順位、消失】** {names_str}さんに前へ行かれました。置いてかれてます、走ってください。",
            ])
        if my_index == 0:
            drama_msg += random.choice([
                "\n👑 **現在トップ独走中！** このまま連勝して逃げ切りましょう！",
                "\n🦑 **王座防衛中！** 今のところ追う側じゃなく追われる側です。",
                "\n🏰 **首位キープ！** 城、建ってます。あとは防衛するだけ。",
            ])
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
                    f"\n✨ {above_name}さんまであと **XP {diff_above}**！もう完全に射程圏内です！",
                    f"\n🐺 {above_name}さんまであと **XP {diff_above}**！獲物、見えてます。",
                ])
            else:
                drama_msg += random.choice([
                    f"\n🎯 1つ上の{above_name}さんまであと **XP {diff_above}**！一歩ずつ距離を詰めよう！",
                    f"\n🧗 {above_name}さんまで **XP {diff_above}**。壁はあるけど、登れない高さじゃない。",
                    f"\n📡 {above_name}さんまで **XP {diff_above}**。まだ遠いけど、レーダーには映ってます。",
                ])
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
            stage_text = " / ".join(next_area["stages"]) if next_area["stages"] else "ステージ情報なし"
            area_msg = f"\n\n🗓️ **次のガチエリア**\n**{ns.strftime('%m/%d %H:%M')} - {ne.strftime('%H:%M')}**\n🗺️ ステージ：**{stage_text}**"
        else:
            area_msg = "\n\n🗓️ **次のガチエリア**\n現在、次回エリア情報を取得できませんでした。"
    await message.channel.send(f"✅ {new_xp} XP を保存しました！{notice}{drama_msg}{goal_msg}{area_msg}")
    success, archived_count, archive_msg = await auto_archive_if_needed(force=False)
    if success:
        await message.channel.send(f"📦 XPログが多くなったため、**{archived_count}件** を自動アーカイブしました！")


@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    async for m_log in log_channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        record = parse_log_record(m_log.content, m_log.created_at)
        if record and record["message_id"] == payload.message_id:
            await m_log.delete()
            break


@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    target_channel = client.get_channel(TARGET_CHANNEL_ID)
    content = payload.data.get("content")
    if not log_channel or not content:
        return
    match = re.search(r"xp\s*([0-9]+)|([0-9]+)\s*xp", content, re.IGNORECASE)
    async for m_log in log_channel.history(limit=ARCHIVE_HISTORY_LIMIT):
        if m_log.author != client.user:
            continue
        record = parse_log_record(m_log.content, m_log.created_at)
        if not record or record["message_id"] != payload.message_id:
            continue
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000):
                if target_channel:
                    await target_channel.send("⚠️ 編集後のパワーも500〜5000で入力してください！")
                return
            record["xp"] = new_xp
            spec_time = parse_specified_time(content, record["time"])
            if spec_time:
                record["time"] = spec_time
                season_year, season_type = get_current_season(spec_time)
                record["season"] = f"{season_year}年 {season_type}"
                if target_channel:
                    start_time = spec_time - timedelta(hours=2)
                    await target_channel.send(f"🔄 記録枠を **{start_time.strftime('%H:%M')}ー{spec_time.strftime('%H:%M')}** に変更しました！")
            await m_log.edit(content=record_to_log_content(record))
        else:
            await m_log.delete()
        break


client.run(TOKEN)
