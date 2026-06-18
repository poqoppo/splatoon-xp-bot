SYNC_COMMANDS=1
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

# XP Bot Stage2 Full + cleanup command
# - 起動時の重い読み込みなし
# - SYNC_COMMANDS=1 / SYNC_COMMADS=1 の時だけスラッシュコマンド同期
# - 設定はログチャンネルに保存
# - シーズン別アーカイブ
# - アーカイブ済みログ掃除コマンド追加
# - 個人グラフは全点・X軸ラベル全部表示
# - 比較グラフは3人以下ならX軸ラベル全部表示、4人以上は間引き

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

TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "1474973509217423401"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "1508739566138294522"))
ARCHIVE_CHANNEL_ID = int(os.environ.get("ARCHIVE_CHANNEL_ID", "1510291838957912285"))
ADMIN_USERS = ["poqoppo", "ricekei"]
ADMIN_USER_IDS = [int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()]

DEFAULT_SETTINGS = {"drama_enabled": True, "area_notice_enabled": True}
BOT_SETTINGS = DEFAULT_SETTINGS.copy()

ARCHIVE_THRESHOLD = int(os.environ.get("ARCHIVE_THRESHOLD", "4500"))
ARCHIVE_HISTORY_LIMIT = int(os.environ.get("ARCHIVE_HISTORY_LIMIT", "5000"))
ARCHIVE_READ_LIMIT = int(os.environ.get("ARCHIVE_READ_LIMIT", "300"))
DELETE_SLEEP_SECONDS = float(os.environ.get("DELETE_SLEEP_SECONDS", "0.15"))
ARCHIVE_MAX_RECORDS_PER_FILE = int(os.environ.get("ARCHIVE_MAX_RECORDS_PER_FILE", "4500"))
ARCHIVE_MAX_PARTS_PER_SEASON = int(os.environ.get("ARCHIVE_MAX_PARTS_PER_SEASON", "2"))
ARCHIVE_SEASON_GRACE_DAYS = int(os.environ.get("ARCHIVE_SEASON_GRACE_DAYS", "1"))
ARCHIVE_CHECK_INTERVAL_SECONDS = int(os.environ.get("ARCHIVE_CHECK_INTERVAL_SECONDS", "1800"))
ARCHIVE_INDEX_CACHE_SECONDS = int(os.environ.get("ARCHIVE_INDEX_CACHE_SECONDS", "1800"))
ARCHIVE_SEASON_CACHE_SECONDS = int(os.environ.get("ARCHIVE_SEASON_CACHE_SECONDS", "600"))
MAX_POINTS_PER_USER = int(os.environ.get("MAX_POINTS_PER_USER", "200"))
MAX_XTICK_LABELS = int(os.environ.get("MAX_XTICK_LABELS", "30"))
COMPARE_ALL_MAX_USERS = int(os.environ.get("COMPARE_ALL_MAX_USERS", "30"))
JST = timezone(timedelta(hours=9), "JST")

CACHED_AREA_SHIFTS = set()
CACHED_AREA_DETAILS = []
LAST_SCHEDULE_FETCH = None
SCHEDULE_CACHE_SECONDS = 600
LAST_ARCHIVE_CHECK = 0.0

archive_lock = asyncio.Lock()
cache_lock = asyncio.Lock()
CACHE_READY = False
CACHE_BY_USER = {}
CACHE_BY_SOURCE = {}
CACHE_GOALS = {}
CACHE_GOAL_MSG = {}
CACHE_SETTINGS_MSG = None

ARCHIVE_INDEX_READY_AT = 0.0
ARCHIVE_INDEX = {}
ARCHIVE_UNKNOWN_ATTACHMENTS = []
ARCHIVE_SEASON_CACHE = {}
ARCHIVE_ALL_CACHE = {"time": 0.0, "records": None}


class XPClient(discord.Client):
    def __init__(self, *, intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if os.environ.get("SYNC_COMMANDS") == "1" or os.environ.get("SYNC_COMMADS") == "1":
            await self.tree.sync()
            print("スラッシュコマンドを同期しました。次回以降は SYNC_COMMANDS=0/未設定 に戻してください。")
        else:
            print("コマンド同期はスキップ。コマンド変更時だけ SYNC_COMMANDS=1 で1回起動してください。")

intents = discord.Intents.default()
intents.message_content = True
client = XPClient(intents=intents)


def is_admin(user):
    if ADMIN_USER_IDS and user.id in ADMIN_USER_IDS:
        return True
    return user.name in ADMIN_USERS


def season_full(year, season_type):
    return f"{int(year)}年 {season_type}"


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


def get_record_season_for_shift_end(end_time):
    return get_current_season(end_time - timedelta(seconds=1))


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


def get_graph_bounds(year_str, season_str=None, month_int=None):
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


def get_season_end(year, season_type):
    _, end = get_graph_bounds(str(year), season_type, None)
    return end


def is_record_in_period(record_time, year_str, season_str=None, month_int=None):
    if month_int:
        return record_time.year == int(year_str) and record_time.month == month_int
    if season_str:
        start, end = get_graph_bounds(year_str, season_str, None)
        return bool(start and end and start <= record_time < end)
    return True


def is_archive_eligible_season(season_name, now_dt=None):
    now_dt = now_dt or datetime.now(JST)
    m = re.match(r"(\d{4})年\s*(春シーズン|夏シーズン|秋シーズン|冬シーズン)", season_name)
    if not m:
        return False
    y, s = int(m.group(1)), m.group(2)
    end = get_season_end(y, s)
    return bool(end and now_dt >= end + timedelta(days=ARCHIVE_SEASON_GRACE_DAYS))


def get_candidate_seasons_for_month(year, month):
    y = int(year)
    if month == 1:
        return [season_full(y - 1, "冬シーズン")]
    if month == 2:
        return [season_full(y - 1, "冬シーズン")]
    if month == 3:
        return [season_full(y - 1, "冬シーズン"), season_full(y, "春シーズン")]
    if month in [4, 5]:
        return [season_full(y, "春シーズン")]
    if month == 6:
        return [season_full(y, "春シーズン"), season_full(y, "夏シーズン")]
    if month in [7, 8]:
        return [season_full(y, "夏シーズン")]
    if month == 9:
        return [season_full(y, "夏シーズン"), season_full(y, "秋シーズン")]
    if month in [10, 11]:
        return [season_full(y, "秋シーズン")]
    if month == 12:
        return [season_full(y, "秋シーズン"), season_full(y, "冬シーズン")]
    return []


def parse_args_from_str(text, current_year, current_season_type, default_to_current_season=False):
    if not text:
        if default_to_current_season:
            return str(current_year), current_season_type, None, False, f"{current_year}年 {current_season_type}", False
        text = ""
    text = text.strip()
    is_continuous = "通し" in text or "やった日から" in text
    if text in ["全期間"]:
        return str(current_year), current_season_type, None, False, f"{current_year}年 {current_season_type}", is_continuous
    if text in ["全記録", "全シーズン", "すべて", "全部", "all", "ALL"]:
        return str(current_year), None, None, True, "全記録", is_continuous
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
    return target_year, None, None, True, "全記録", is_continuous


def season_from_filename(filename):
    m = re.search(r"xp_archive_(\d{4})_(spring|summer|autumn|winter)", filename)
    if not m:
        return None
    mp = {"spring": "春シーズン", "summer": "夏シーズン", "autumn": "秋シーズン", "winter": "冬シーズン"}
    return season_full(int(m.group(1)), mp[m.group(2)])


def safe_archive_filename_part(season_name):
    m = re.match(r"(\d{4})年\s*(春シーズン|夏シーズン|秋シーズン|冬シーズン)", season_name)
    if not m:
        return re.sub(r"[^0-9A-Za-z_\-]", "_", season_name)
    mp = {"春シーズン": "spring", "夏シーズン": "summer", "秋シーズン": "autumn", "冬シーズン": "winter"}
    return f"{m.group(1)}_{mp[m.group(2)]}"


def chunk_records(records, size):
    for i in range(0, len(records), size):
        yield records[i:i + size]


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
    return json.dumps({"type": "xp_record", "user_id": int(user_id), "user_name": str(user_name), "xp": int(xp), "time": record_time.strftime("%Y/%m/%d %H:%M"), "season": str(season), "message_id": int(message_id)}, ensure_ascii=False)


def make_goal_json(user_id, user_name, target_xp, season):
    return json.dumps({"type": "xp_goal", "user_id": int(user_id), "user_name": str(user_name), "target_xp": int(target_xp), "season": str(season), "created_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M"), "active": True}, ensure_ascii=False)


def make_settings_json(settings):
    return json.dumps({"type": "bot_settings", "drama_enabled": bool(settings.get("drama_enabled", True)), "area_notice_enabled": bool(settings.get("area_notice_enabled", True)), "updated_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M")}, ensure_ascii=False)


def parse_log_record(content, fallback_created_at=None):
    try:
        obj = json.loads(content)
        if obj.get("type") != "xp_record":
            return None
        uid = int(obj["user_id"])
        return {"user_id": uid, "user_name": str(obj.get("user_name", f"ID:{uid}")), "xp": int(obj["xp"]), "time": parse_time_str(str(obj["time"]), fallback_created_at), "season": str(obj["season"]), "message_id": int(obj.get("message_id", 0)), "raw_type": "json"}
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
            return {"user_id": uid, "user_name": uname, "xp": xp, "time": parse_time_str(t_str, fallback_created_at), "season": season, "message_id": msg_id, "raw_type": "pipe"}
    except Exception:
        return None
    return None


def parse_goal_record(content):
    try:
        obj = json.loads(content)
        if obj.get("type") != "xp_goal":
            return None
        uid = int(obj["user_id"])
        return {"user_id": uid, "user_name": str(obj.get("user_name", f"ID:{uid}")), "target_xp": int(obj["target_xp"]), "season": str(obj["season"]), "created_at": str(obj.get("created_at", "")), "active": bool(obj.get("active", True))}
    except Exception:
        return None


def parse_settings_record(content):
    try:
        obj = json.loads(content)
        if obj.get("type") != "bot_settings":
            return None
        return {"drama_enabled": bool(obj.get("drama_enabled", True)), "area_notice_enabled": bool(obj.get("area_notice_enabled", True))}
    except Exception:
        return None


def record_to_log_content(record):
    return make_record_json(record["user_id"], record["user_name"], record["xp"], record["time"], record["season"], record.get("message_id", 0))


def goal_to_log_content(goal):
    return json.dumps({"type": "xp_goal", "user_id": int(goal["user_id"]), "user_name": str(goal["user_name"]), "target_xp": int(goal["target_xp"]), "season": str(goal["season"]), "created_at": str(goal.get("created_at", "")), "active": bool(goal.get("active", True))}, ensure_ascii=False)


def record_to_archive_obj(record):
    return {"type": "xp_record", "user_id": int(record["user_id"]), "user_name": str(record["user_name"]), "xp": int(record["xp"]), "time": record["time"].strftime("%Y/%m/%d %H:%M"), "season": str(record["season"]), "message_id": int(record.get("message_id", 0))}


def parse_archive_record_obj(obj):
    try:
        if obj.get("type") != "xp_record":
            return None
        uid = int(obj["user_id"])
        return {"user_id": uid, "user_name": str(obj.get("user_name", f"ID:{uid}")), "xp": int(obj["xp"]), "time": parse_time_str(str(obj["time"])), "season": str(obj["season"]), "message_id": int(obj.get("message_id", 0)), "raw_type": "archive_json"}
    except Exception:
        return None


def make_record_unique_key(record):
    msg_id = int(record.get("message_id", 0))
    if msg_id:
        return f"msg:{msg_id}"
    return f"fallback:{record['user_id']}:{record['time'].strftime('%Y/%m/%d %H:%M')}:{record['xp']}:{record['season']}"


async def fetch_area_schedule(force=False):
    global CACHED_AREA_SHIFTS, CACHED_AREA_DETAILS, LAST_SCHEDULE_FETCH
    now = datetime.now(JST)
    if not force and LAST_SCHEDULE_FETCH and (now - LAST_SCHEDULE_FETCH).total_seconds() < SCHEDULE_CACHE_SECONDS and CACHED_AREA_DETAILS:
        return
    try:
        req = urllib.request.Request("https://spla3.yuu26.com/api/x/schedule", headers={"User-Agent": "XP-Bot/3.4"})
        res = await asyncio.to_thread(urllib.request.urlopen, req, timeout=5)
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
    return sorted(candidates, key=lambda x: x["start"])[0]


def get_last_splat_end_time(dt):
    h = dt.hour
    if h % 2 == 0:
        h -= 1
    if h < 0:
        h = 23
        dt = dt - timedelta(days=1)
    return dt.replace(hour=h, minute=0, second=0, microsecond=0)


def normalize_specified_time(candidate, now_dt):
    return candidate - timedelta(days=1) if candidate > now_dt else candidate


def parse_specified_time(content, now_dt):
    m = re.search(r"([0-9]{1,2}):([0-9]{2})", content)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= minute < 60:
            return normalize_specified_time(now_dt.replace(hour=h, minute=minute, second=0, microsecond=0), now_dt)
    m = re.search(r"([0-9]{1,2})時", content)
    if m:
        h = int(m.group(1))
        if 0 <= h < 24:
            return normalize_specified_time(now_dt.replace(hour=h, minute=0, second=0, microsecond=0), now_dt)
    return None


def _new_cache_rec(parsed, source, log_msg_id):
    src_id = int(parsed.get("message_id", 0))
    return {"user_id": int(parsed["user_id"]), "user_name": str(parsed["user_name"]), "xp": int(parsed["xp"]), "time": parsed["time"], "season": str(parsed["season"]), "message_id": src_id, "msg_id": src_id, "_source": source, "_log_msg_id": log_msg_id}


def _cache_insert(rec):
    uid = rec["user_id"]
    uname = rec["user_name"]
    if uid not in CACHE_BY_USER:
        CACHE_BY_USER[uid] = {"name": uname, "records": []}
    if uname != f"ID:{uid}":
        CACHE_BY_USER[uid]["name"] = uname
    CACHE_BY_USER[uid]["records"].append(rec)
    if rec["_source"] == "log" and rec["message_id"]:
        CACHE_BY_SOURCE[rec["message_id"]] = rec


def _cache_add_single(rec):
    _cache_insert(rec)
    CACHE_BY_USER[rec["user_id"]]["records"].sort(key=lambda x: x["time"])


def _cache_remove_record(rec):
    uid = rec["user_id"]
    if uid in CACHE_BY_USER:
        try:
            CACHE_BY_USER[uid]["records"].remove(rec)
        except ValueError:
            pass
    mid = rec.get("message_id")
    if mid and CACHE_BY_SOURCE.get(mid) is rec:
        del CACHE_BY_SOURCE[mid]


def _cache_mark_archived(rec):
    mid = rec.get("message_id")
    if mid and CACHE_BY_SOURCE.get(mid) is rec:
        del CACHE_BY_SOURCE[mid]
    rec["_source"] = "archive"
    rec["_log_msg_id"] = None


def _cache_find_latest_log_record(uid):
    cands = [r for r in CACHE_BY_USER.get(uid, {}).get("records", []) if r["_source"] == "log" and r["_log_msg_id"]]
    return max(cands, key=lambda r: r["_log_msg_id"]) if cands else None


def _cache_user_log_records(uid):
    return [r for r in CACHE_BY_USER.get(uid, {}).get("records", []) if r["_source"] == "log" and r["_log_msg_id"]]


def _cache_all_log_records():
    out = []
    for info in CACHE_BY_USER.values():
        out.extend(r for r in info["records"] if r["_source"] == "log" and r["_log_msg_id"])
    return out


def _cache_set_goal(goal, log_msg_id):
    CACHE_GOALS.setdefault(goal["user_id"], {})[goal["season"]] = goal
    CACHE_GOAL_MSG[(goal["user_id"], goal["season"])] = log_msg_id


def _cache_remove_goal(uid, season):
    if uid in CACHE_GOALS and season in CACHE_GOALS[uid]:
        del CACHE_GOALS[uid][season]
    CACHE_GOAL_MSG.pop((uid, season), None)


def _count_cache_records():
    current = sum(1 for u in CACHE_BY_USER.values() for r in u["records"] if r["_source"] == "log")
    archived_loaded = sum(1 for u in CACHE_BY_USER.values() for r in u["records"] if r["_source"] == "archive")
    return current, archived_loaded


async def _persist_settings():
    global CACHE_SETTINGS_MSG
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    content = make_settings_json(BOT_SETTINGS)
    if CACHE_SETTINGS_MSG:
        try:
            await log_channel.get_partial_message(CACHE_SETTINGS_MSG).edit(content=content)
            return
        except Exception:
            CACHE_SETTINGS_MSG = None
    try:
        sent = await log_channel.send(content)
        CACHE_SETTINGS_MSG = sent.id
    except Exception as e:
        print(f"Settings persist error: {e}")


async def _do_rebuild():
    global CACHE_SETTINGS_MSG
    CACHE_BY_USER.clear()
    CACHE_BY_SOURCE.clear()
    CACHE_GOALS.clear()
    CACHE_GOAL_MSG.clear()
    BOT_SETTINGS.clear()
    BOT_SETTINGS.update(DEFAULT_SETTINGS)
    CACHE_SETTINGS_MSG = None
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        async for message in log_channel.history(limit=ARCHIVE_HISTORY_LIMIT, oldest_first=True):
            if message.author != client.user:
                continue
            parsed = parse_log_record(message.content, message.created_at)
            if parsed:
                _cache_insert(_new_cache_rec(parsed, "log", message.id))
                continue
            goal = parse_goal_record(message.content)
            if goal:
                _cache_set_goal(goal, message.id)
                continue
            settings = parse_settings_record(message.content)
            if settings is not None:
                BOT_SETTINGS.update(settings)
                CACHE_SETTINGS_MSG = message.id
    for uid in CACHE_BY_USER:
        CACHE_BY_USER[uid]["records"].sort(key=lambda x: x["time"])


async def ensure_cache():
    global CACHE_READY
    if CACHE_READY:
        return
    async with cache_lock:
        if CACHE_READY:
            return
        await _do_rebuild()
        CACHE_READY = True


async def rebuild_cache():
    global CACHE_READY
    async with cache_lock:
        await _do_rebuild()
        CACHE_READY = True


async def get_active_goal(user_id, season):
    await ensure_cache()
    goal = CACHE_GOALS.get(user_id, {}).get(season)
    if not goal or not goal.get("active", True):
        return None
    return goal


async def get_archive_index(force=False):
    global ARCHIVE_INDEX_READY_AT, ARCHIVE_INDEX, ARCHIVE_UNKNOWN_ATTACHMENTS
    now_mono = time.monotonic()
    if not force and ARCHIVE_INDEX_READY_AT and now_mono - ARCHIVE_INDEX_READY_AT < ARCHIVE_INDEX_CACHE_SECONDS:
        return ARCHIVE_INDEX, ARCHIVE_UNKNOWN_ATTACHMENTS
    channel = client.get_channel(ARCHIVE_CHANNEL_ID)
    index = {}
    unknown = []
    if channel:
        async for message in channel.history(limit=ARCHIVE_READ_LIMIT, oldest_first=True):
            if message.author != client.user:
                continue
            for attachment in message.attachments:
                if not (attachment.filename.startswith("xp_archive_") and attachment.filename.endswith(".json")):
                    continue
                s = season_from_filename(attachment.filename)
                if s:
                    index.setdefault(s, []).append(attachment)
                else:
                    unknown.append(attachment)
    ARCHIVE_INDEX = index
    ARCHIVE_UNKNOWN_ATTACHMENTS = unknown
    ARCHIVE_INDEX_READY_AT = time.monotonic()
    return ARCHIVE_INDEX, ARCHIVE_UNKNOWN_ATTACHMENTS


async def _read_archive_attachment(attachment, expected_seasons=None):
    records = []
    try:
        raw = await attachment.read()
        obj = json.loads(raw.decode("utf-8"))
        if obj.get("type") != "xp_archive":
            return records
        archive_season = obj.get("season")
        if expected_seasons and archive_season and archive_season not in expected_seasons:
            return records
        for rec_obj in obj.get("records", []):
            record = parse_archive_record_obj(rec_obj)
            if record:
                if expected_seasons and record.get("season") not in expected_seasons:
                    continue
                records.append(record)
    except Exception as e:
        print(f"Archive read error: {getattr(attachment, 'filename', 'unknown')}: {e}")
    return records


async def get_archive_records_for_seasons(seasons, include_unknown=True):
    seasons = list(dict.fromkeys(seasons or []))
    if not seasons:
        return []
    now_mono = time.monotonic()
    out = []
    missing = []
    for s in seasons:
        cached = ARCHIVE_SEASON_CACHE.get(s)
        if cached and now_mono - cached["time"] < ARCHIVE_SEASON_CACHE_SECONDS:
            out.extend(cached["records"])
        else:
            missing.append(s)
    if not missing:
        return out
    index, unknown = await get_archive_index()
    for s in missing:
        recs = []
        for attachment in index.get(s, []):
            recs.extend(await _read_archive_attachment(attachment, expected_seasons=[s]))
        if include_unknown:
            for attachment in unknown:
                recs.extend(await _read_archive_attachment(attachment, expected_seasons=[s]))
        ARCHIVE_SEASON_CACHE[s] = {"time": time.monotonic(), "records": recs}
        out.extend(recs)
    return out


async def get_all_archive_records():
    now_mono = time.monotonic()
    if ARCHIVE_ALL_CACHE["records"] is not None and now_mono - ARCHIVE_ALL_CACHE["time"] < ARCHIVE_SEASON_CACHE_SECONDS:
        return ARCHIVE_ALL_CACHE["records"]
    index, unknown = await get_archive_index()
    records = []
    for attachments in index.values():
        for attachment in attachments:
            records.extend(await _read_archive_attachment(attachment))
    for attachment in unknown:
        records.extend(await _read_archive_attachment(attachment))
    ARCHIVE_ALL_CACHE["records"] = records
    ARCHIVE_ALL_CACHE["time"] = time.monotonic()
    return records


def current_cache_records_as_parsed():
    records = []
    for info in CACHE_BY_USER.values():
        records.extend(info["records"])
    return records


def build_data_from_records(records):
    data = {}
    seen = set()
    for record in records:
        key = make_record_unique_key(record)
        if key in seen:
            continue
        seen.add(key)
        uid = int(record["user_id"])
        uname = str(record["user_name"])
        if uid not in data:
            data[uid] = {"name": uname, "records": []}
        if uname != f"ID:{uid}":
            data[uid]["name"] = uname
        data[uid]["records"].append({"user_id": uid, "user_name": uname, "xp": int(record["xp"]), "time": record["time"], "season": str(record["season"]), "message_id": int(record.get("message_id", 0)), "msg_id": int(record.get("message_id", 0)), "_source": record.get("_source", record.get("raw_type", "merged"))})
    for uid in data:
        data[uid]["records"].sort(key=lambda x: x["time"])
    return data


async def get_records_for_period(year_str, season_str=None, month_int=None, include_all=False, is_continuous=False):
    await ensure_cache()
    current_records = current_cache_records_as_parsed()
    if include_all or is_continuous:
        archive_records = await get_all_archive_records()
    else:
        if month_int:
            seasons_to_load = get_candidate_seasons_for_month(year_str, month_int)
        elif season_str:
            seasons_to_load = [season_full(year_str, season_str)]
        else:
            cy, cs = get_current_season(datetime.now(JST))
            seasons_to_load = [season_full(cy, cs)]
        archive_records = await get_archive_records_for_seasons(seasons_to_load)
    return build_data_from_records(current_records + archive_records)


async def get_all_records():
    await ensure_cache()
    return build_data_from_records(current_cache_records_as_parsed() + await get_all_archive_records())


async def get_archive_records_for_cleanup(period_text=None):
    now = datetime.now(JST)
    cy, cs = get_current_season(now)
    if not period_text:
        records = await get_all_archive_records()
        return records, "全アーカイブ"
    ty, ts, tm, ia, title, is_continuous = parse_args_from_str(period_text, cy, cs, False)
    if ia or is_continuous:
        records = await get_all_archive_records()
    elif tm:
        records = await get_archive_records_for_seasons(get_candidate_seasons_for_month(ty, tm))
        records = [r for r in records if is_record_in_period(r["time"], ty, None, tm)]
    elif ts:
        records = await get_archive_records_for_seasons([season_full(ty, ts)])
        records = [r for r in records if is_record_in_period(r["time"], ty, ts, None)]
    else:
        records = await get_all_archive_records()
    return records, title


async def auto_archive_if_needed(force=False):
    global LAST_ARCHIVE_CHECK, ARCHIVE_INDEX_READY_AT
    async with archive_lock:
        await ensure_cache()
        now = datetime.now(JST)
        now_mono = time.monotonic()
        if not force and now_mono - LAST_ARCHIVE_CHECK < ARCHIVE_CHECK_INTERVAL_SECONDS:
            return False, 0, "次回チェック待ちです"
        LAST_ARCHIVE_CHECK = now_mono
        archive_channel = client.get_channel(ARCHIVE_CHANNEL_ID)
        log_channel = client.get_channel(LOG_CHANNEL_ID)
        if not archive_channel or not log_channel:
            return False, 0, "チャンネルが見つかりません"
        log_records = list(_cache_all_log_records())
        if not log_records:
            return False, 0, "アーカイブ対象がありません"
        current_count, _ = _count_cache_records()
        if force:
            candidates = log_records
        else:
            candidates = [r for r in log_records if is_archive_eligible_season(r.get("season", ""), now)]
            if current_count < ARCHIVE_THRESHOLD and not candidates:
                return False, current_count, "アーカイブ条件未達です"
        if not candidates:
            return False, current_count, "アーカイブ可能な過去シーズンがありません"
        grouped = {}
        for rec in candidates:
            grouped.setdefault(rec.get("season", "不明シーズン"), []).append(rec)
        archived_records = []
        warnings = []
        for season_name, recs in grouped.items():
            recs.sort(key=lambda x: x["time"])
            chunks = list(chunk_records([record_to_archive_obj(r) for r in recs], ARCHIVE_MAX_RECORDS_PER_FILE))
            if len(chunks) > ARCHIVE_MAX_PARTS_PER_SEASON:
                warnings.append(f"{season_name} が {len(recs)}件あり、最大{ARCHIVE_MAX_RECORDS_PER_FILE * ARCHIVE_MAX_PARTS_PER_SEASON}件を超えました。先頭分のみアーカイブします。")
                chunks = chunks[:ARCHIVE_MAX_PARTS_PER_SEASON]
            safe_season = safe_archive_filename_part(season_name)
            max_parts = min(len(chunks), ARCHIVE_MAX_PARTS_PER_SEASON)
            archived_count_for_season = 0
            for part_index, chunk in enumerate(chunks, start=1):
                archive_obj = {"type": "xp_archive", "archive_unit": "season", "season": season_name, "part": part_index, "max_parts": ARCHIVE_MAX_PARTS_PER_SEASON, "created_at": now.strftime("%Y/%m/%d %H:%M:%S"), "record_count": len(chunk), "records": chunk}
                file_obj = io.BytesIO(json.dumps(archive_obj, ensure_ascii=False, indent=2).encode("utf-8"))
                fname = f"xp_archive_{safe_season}_part{part_index}_{now.strftime('%Y%m%d_%H%M%S')}_{len(chunk)}records.json"
                try:
                    await archive_channel.send(content=f"📦 **XPログ シーズン別アーカイブ**\nシーズン：**{season_name}**\nPart：**{part_index}/{max_parts}**\n件数：**{len(chunk)}件**\n作成日時：**{now.strftime('%Y/%m/%d %H:%M:%S')}**", file=discord.File(file_obj, filename=fname))
                    archived_count_for_season += len(chunk)
                except Exception as e:
                    return False, len(archived_records), f"アーカイブ送信失敗: {e}"
            archived_records.extend(recs[:archived_count_for_season])
        deleted_count = 0
        failed_count = 0
        for rec in archived_records:
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
                deleted_count += 1
                _cache_mark_archived(rec)
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception as e:
                failed_count += 1
                print(f"Archive delete error: {e}")
        ARCHIVE_INDEX_READY_AT = 0.0
        ARCHIVE_ALL_CACHE["time"] = 0.0
        ARCHIVE_ALL_CACHE["records"] = None
        for season_name in grouped:
            ARCHIVE_SEASON_CACHE.pop(season_name, None)
        msg = "アーカイブ完了"
        if failed_count:
            msg += f"。ただし {failed_count}件のログ削除に失敗しました。/アーカイブ済みログ掃除 を実行してください。"
        if warnings:
            msg += "（警告あり）: " + " / ".join(warnings)
        return True, deleted_count, msg


def thin_records_for_plot(recs, max_points=MAX_POINTS_PER_USER):
    if len(recs) <= max_points:
        return recs
    if max_points <= 2:
        return [recs[0], recs[-1]]
    step = (len(recs) - 1) / (max_points - 1)
    idxs = sorted({round(i * step) for i in range(max_points)})
    return [recs[i] for i in idxs]


def set_limited_xticks(ax, indices, labels, force_all=False):
    if not indices:
        return
    if force_all or len(indices) <= MAX_XTICK_LABELS:
        show = list(range(len(indices)))
    else:
        step = (len(indices) - 1) / (MAX_XTICK_LABELS - 1)
        show = sorted({round(i * step) for i in range(MAX_XTICK_LABELS)})
    ax.set_xticks([indices[i] for i in show])
    ax.set_xticklabels([labels[i] for i in show], rotation=90, fontsize=9)


def build_power_change_message(old_xp, new_xp):
    if old_xp is None:
        return random.choice(["\n🆕 **初記録！** ここから伝説、始めちゃいますか！", "\n🦑 **初陣記録！** まずはここがスタートライン。", "\n📌 **初登録完了！** 成長の始まりです。"]) 
    diff = new_xp - old_xp
    if diff > 0:
        if diff >= 100:
            return random.choice([f"\n🚀 **爆伸び！** 前回から **+{diff} XP**！", f"\n🔥 **大暴れ成功！** **+{diff} XP**！"])
        if diff >= 50:
            return random.choice([f"\n📈 **かなり良い伸び！** **+{diff} XP**！", f"\n⚡ **ナイス上昇！** **+{diff} XP**！"])
        return random.choice([f"\n✅ **微増ナイス！** **+{diff} XP**！", f"\n📊 **じわ伸び！** **+{diff} XP**。"])
    if diff == 0:
        return random.choice(["\n🟰 **現状維持！**", "\n😐 **変動なし！**"])
    drop = abs(diff)
    if drop >= 150:
        return random.choice([f"\n💥 **大事故発生！** **-{drop} XP**……", f"\n🫠 **溶けすぎ注意！** **-{drop} XP**。"])
    if drop >= 80:
        return random.choice([f"\n😱 **けっこう痛い！** **-{drop} XP**。", f"\n🧯 **消火活動開始！** **-{drop} XP**。"])
    if drop >= 30:
        return random.choice([f"\n😬 **ちょい痛い減少！** **-{drop} XP**。", f"\n📉 **少し後退！** **-{drop} XP**。"])
    return random.choice([f"\n🤏 **微減！** **-{drop} XP**。", f"\n😌 **軽傷！** **-{drop} XP**。"])


@client.event
async def on_ready():
    print(f"{client.user} が起動しました！（キャッシュは初回アクセス時に遅延ロード）")


@client.tree.command(name="通知設定", description="煽り文章・次のガチエリア表示をON/OFFします")
@app_commands.describe(煽り文章="XP保存後の煽り文章を表示するか", エリア通知="XP保存後に次のガチエリア時間とステージを表示するか")
async def notification_settings(interaction: discord.Interaction, 煽り文章: bool = None, エリア通知: bool = None):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    await ensure_cache()
    async with cache_lock:
        changed = False
        if 煽り文章 is not None:
            BOT_SETTINGS["drama_enabled"] = 煽り文章; changed = True
        if エリア通知 is not None:
            BOT_SETTINGS["area_notice_enabled"] = エリア通知; changed = True
        if changed:
            await _persist_settings()
    await interaction.followup.send(f"⚙️ **現在の通知設定**\n煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**")


@client.tree.command(name="設定確認", description="現在のBot設定を確認します")
async def show_settings(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await ensure_cache()
    await interaction.followup.send(f"⚙️ **現在の通知設定**\n煽り文章：**{'ON' if BOT_SETTINGS.get('drama_enabled', True) else 'OFF'}**\n次のガチエリア表示：**{'ON' if BOT_SETTINGS.get('area_notice_enabled', True) else 'OFF'}**")


@client.tree.command(name="ログ件数", description="現在ログと読み込み済みアーカイブの件数を確認します")
async def log_count(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await ensure_cache()
    current, archived_loaded = _count_cache_records()
    await interaction.followup.send(f"📊 **XPログ件数**\n現在ログチャンネル：**{current}件**\nメモリ上のアーカイブ化済み記録：**{archived_loaded}件**\n自動アーカイブしきい値：**{ARCHIVE_THRESHOLD}件**\n※過去アーカイブJSONは必要時だけ読み込みます。")


@client.tree.command(name="再読み込み", description="【管理者専用】現在ログ・目標・設定のキャッシュを作り直します")
async def reload_cache(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    await rebuild_cache()
    current, archived_loaded = _count_cache_records()
    await interaction.followup.send(f"🔄 キャッシュを再読み込みしました。\nログ **{current}件** ・メモリ内archive **{archived_loaded}件**")


@client.tree.command(name="全再読み込み", description="【管理者専用】アーカイブ一覧・過去シーズンキャッシュもクリアします")
async def reload_all_cache(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    global ARCHIVE_INDEX_READY_AT
    ARCHIVE_INDEX_READY_AT = 0.0
    ARCHIVE_INDEX.clear(); ARCHIVE_UNKNOWN_ATTACHMENTS.clear(); ARCHIVE_SEASON_CACHE.clear()
    ARCHIVE_ALL_CACHE["time"] = 0.0; ARCHIVE_ALL_CACHE["records"] = None
    await rebuild_cache()
    await interaction.followup.send("🔄 全キャッシュをクリアして、現在ログを再読み込みしました。")


@client.tree.command(name="手動アーカイブ", description="【管理者専用】現在ログをシーズン別にアーカイブします")
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
    await interaction.followup.send(f"📦 手動アーカイブ完了：**{count}件** をシーズン別に保存して現在ログから削除しました。\n{msg}" if success else f"⚠️ 手動アーカイブ失敗/未実行：{msg}（対象 {count}件）")


@client.tree.command(name="アーカイブ済みログ掃除", description="【管理者専用】アーカイブ済みなのに現在ログに残った記録だけ削除します")
@app_commands.describe(確認="実行する場合は CLEAN と入力", 期間="任意。例：春シーズン、2026年 春シーズン、5月。空欄なら全アーカイブ照合")
async def cleanup_archived_logs(interaction: discord.Interaction, 確認: str, 期間: str = None):
    await interaction.response.defer(ephemeral=True)
    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return
    if 確認 != "CLEAN":
        await interaction.followup.send("⚠️ 実行するには `CLEAN` と入力してください。")
        return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    archive_records, title = await get_archive_records_for_cleanup(期間)
    archived_keys = set(make_record_unique_key(r) for r in archive_records)
    if not archived_keys:
        await interaction.followup.send(f"⚠️ {title} のアーカイブ記録が見つかりませんでした。")
        return
    deleted_count = 0
    failed_count = 0
    checked_count = 0
    async with cache_lock:
        for rec in list(_cache_all_log_records()):
            checked_count += 1
            if make_record_unique_key(rec) not in archived_keys:
                continue
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
                deleted_count += 1
                _cache_mark_archived(rec)
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception as e:
                failed_count += 1
                print(f"Archived log cleanup delete error: {e}")
    await interaction.followup.send(f"🧹 **アーカイブ済みログ掃除完了**\n照合範囲：**{title}**\n現在ログ確認：**{checked_count}件**\n削除：**{deleted_count}件**\n失敗：**{failed_count}件**")


@client.tree.command(name="目標設定", description="現シーズンの目標XPを設定します")
@app_commands.describe(目標xp="目標にするXP。例: 2800")
async def set_goal(interaction: discord.Interaction, 目標xp: int):
    await interaction.response.defer(ephemeral=True)
    if not (500 <= 目標xp < 5000):
        await interaction.followup.send("⚠️ 目標XPは500〜5000で入力してください！")
        return
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    current_xp = None
    async with cache_lock:
        old_goal = CACHE_GOALS.get(interaction.user.id, {}).get(sname)
        old_msg_id = CACHE_GOAL_MSG.get((interaction.user.id, sname))
        if old_goal and old_goal.get("active", True) and old_msg_id:
            old_goal["active"] = False
            try:
                await log_channel.get_partial_message(old_msg_id).edit(content=goal_to_log_content(old_goal))
            except Exception:
                pass
        sent = await log_channel.send(make_goal_json(interaction.user.id, interaction.user.display_name, 目標xp, sname))
        _cache_set_goal({"user_id": interaction.user.id, "user_name": interaction.user.display_name, "target_xp": 目標xp, "season": sname, "created_at": now.strftime("%Y/%m/%d %H:%M"), "active": True}, sent.id)
        info = CACHE_BY_USER.get(interaction.user.id)
        if info:
            recs = [r for r in info["records"] if is_record_in_period(r["time"], str(sy), st, None)]
            if recs:
                current_xp = recs[-1]["xp"]
    if current_xp is None:
        await interaction.followup.send(f"🎯 **{sname} の目標を設定しました！**\n目標：**{目標xp} XP**\nまだ今シーズンの記録がないので、まずは1回記録しましょう！")
    else:
        diff = 目標xp - current_xp
        await interaction.followup.send(f"🎯 **{sname} の目標を設定しました！**\n目標：**{目標xp} XP**\n現在：**{current_xp} XP**\n" + ("✅ もう目標達成済みです。" if diff <= 0 else f"あと **{diff} XP**！"))


@client.tree.command(name="目標確認", description="現シーズンの目標XPと達成状況を確認します")
async def check_goal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)
    goal = await get_active_goal(interaction.user.id, sname)
    if not goal:
        await interaction.followup.send(f"⚠️ **{sname}** の目標がまだ設定されていません。\n`/目標設定` で目標XPを設定できます。")
        return
    all_d = await get_records_for_period(str(sy), st, None, False)
    current_xp = best_xp = None
    if interaction.user.id in all_d:
        recs = [r for r in all_d[interaction.user.id]["records"] if is_record_in_period(r["time"], str(sy), st, None)]
        if recs:
            current_xp = recs[-1]["xp"]
            best_xp = max(r["xp"] for r in recs)
    target_xp = goal["target_xp"]
    if current_xp is None:
        await interaction.followup.send(f"🎯 **{sname} の目標**\n目標：**{target_xp} XP**\n現在：記録なし")
    else:
        diff = target_xp - current_xp
        await interaction.followup.send(f"🎯 **{sname} の目標**\n目標：**{target_xp} XP**\n現在：**{current_xp} XP**\n今シーズン最高：**{best_xp} XP**\n" + ("✅ **目標達成済み！**" if diff <= 0 else f"あと **{diff} XP**！"))


@client.tree.command(name="目標削除", description="現シーズンの目標XPを削除します")
@app_commands.describe(確認="削除する場合は DELETE と入力")
async def delete_goal(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if 確認 != "DELETE":
        await interaction.followup.send("⚠️ 目標を削除するには `DELETE` と入力してください。")
        return
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    deleted = False
    async with cache_lock:
        msg_id = CACHE_GOAL_MSG.get((interaction.user.id, sname))
        goal = CACHE_GOALS.get(interaction.user.id, {}).get(sname)
        if msg_id and goal and goal.get("active", True):
            try:
                await log_channel.get_partial_message(msg_id).delete()
            except Exception:
                pass
            _cache_remove_goal(interaction.user.id, sname)
            deleted = True
    await interaction.followup.send(f"🗑️ **{sname}** の目標を削除しました。" if deleted else f"⚠️ **{sname}** の有効な目標が見つかりませんでした。")


@client.tree.command(name="自己ベスト", description="自分の最高XPを表示します")
@app_commands.describe(期間="例：「夏シーズン」「5月」「全期間」「全記録」など")
async def personal_best(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    ty, ts, tm, ia, title, cont = parse_args_from_str(期間, sy, st, True)
    all_d = await get_records_for_period(ty, ts, tm, ia, cont)
    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return
    recs = all_d[interaction.user.id]["records"]
    if not ia and not cont:
        recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
    if not recs:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    best = max(recs, key=lambda r: r["xp"])
    await interaction.followup.send(f"🏅 **{interaction.user.display_name} さんの自己ベスト**\n期間：**{title}**\n最高XP：**{best['xp']} XP**\n記録枠：**{best['time'].strftime('%Y/%m/%d %H:%M')}**\nシーズン：**{best['season']}**")


@client.tree.command(name="伸びランキング", description="指定期間でXPが伸びた人ランキングを表示します")
@app_commands.describe(期間="例：「夏シーズン」「5月」「全期間」「全記録」など")
async def growth_ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    ty, ts, tm, ia, title, cont = parse_args_from_str(期間, sy, st, True)
    all_d = await get_records_for_period(ty, ts, tm, ia, cont)
    growth_list = []
    for uid, info in all_d.items():
        recs = info["records"]
        if not ia and not cont:
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
@app_commands.describe(期間="例：「5月」「夏シーズン」「全期間」「全記録」など")
async def graph(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    ty, ts, tm, ia, title, cont = parse_args_from_str(期間, sy, st, True)
    all_d = await get_records_for_period(ty, ts, tm, ia, cont)
    if interaction.user.id not in all_d:
        await interaction.followup.send("⚠️ データがありません。")
        return
    recs = all_d[interaction.user.id]["records"]
    if not ia and not cont:
        recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
    if not recs:
        await interaction.followup.send(f"⚠️ {title} のデータがありません。")
        return
    plot_recs = recs
    fig, ax = plt.subplots(figsize=(12, 6))
    indices = list(range(len(plot_recs)))
    xps = [r["xp"] for r in plot_recs]
    ax.plot(indices, xps, marker="o", color="#1f77b4", linewidth=1.5, markersize=5)
    set_limited_xticks(ax, indices, [r["time"].strftime("%m/%d %H:%M") for r in plot_recs], force_all=True)
    ax.axhline(max(xps), linestyle="--", alpha=0.4)
    ax.set_title(f"{interaction.user.display_name} さんの成長記録 ({title})", fontsize=15)
    ax.grid(True, linestyle="--", alpha=0.6)
    plt.tight_layout()
    fname = f"g_{interaction.user.id}_{int(time.time())}.png"
    try:
        plt.savefig(fname, dpi=100)
        plt.close(fig)
        await interaction.followup.send(file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)


@client.tree.command(name="比較グラフ", description="メンバー全員、または指定した人を重ねて比較します")
@app_commands.describe(相手1="比較したい相手1", 相手2="比較したい相手2", 相手3="比較したい相手3", 期間="例：「5月」「夏シーズン」「全期間」「全記録」など")
async def comp_graph(interaction: discord.Interaction, 相手1: discord.Member = None, 相手2: discord.Member = None, 相手3: discord.Member = None, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    ty, ts, tm, ia, title, cont = parse_args_from_str(期間, sy, st, True)
    all_d = await get_records_for_period(ty, ts, tm, ia, cont)
    targets = [interaction.user.id]
    if 相手1:
        targets.append(相手1.id)
    if 相手2:
        targets.append(相手2.id)
    if 相手3:
        targets.append(相手3.id)
    targets = list(dict.fromkeys(targets))
    is_all_compare = len(targets) == 1
    target_ids = list(all_d.keys()) if is_all_compare else targets
    plot_data = []
    for uid in target_ids:
        if uid not in all_d:
            continue
        recs = all_d[uid]["records"]
        if not ia and not cont:
            recs = [r for r in recs if is_record_in_period(r["time"], ty, ts, tm)]
        if recs:
            plot_data.append((all_d[uid]["name"], recs))
    omitted_msg = ""
    if is_all_compare and len(plot_data) > COMPARE_ALL_MAX_USERS:
        plot_data.sort(key=lambda x: x[1][-1]["xp"], reverse=True)
        omitted = len(plot_data) - COMPARE_ALL_MAX_USERS
        plot_data = plot_data[:COMPARE_ALL_MAX_USERS]
        omitted_msg = f"\n⚠️ 全員比較対象が多いため、最新XP上位{COMPARE_ALL_MAX_USERS}人のみ表示しています（省略 {omitted}人）。"
    if not plot_data:
        await interaction.followup.send("⚠️ 比較するデータがありません。")
        return
    plot_data = [(name, thin_records_for_plot(recs)) for name, recs in plot_data]
    fig, ax = plt.subplots(figsize=(12, 6))
    max_len = max(len(recs) for _, recs in plot_data)
    label_recs = max(plot_data, key=lambda x: len(x[1]))[1]
    for name, recs in plot_data:
        ax.plot(list(range(len(recs))), [r["xp"] for r in recs], marker="o", linewidth=1.5, markersize=4, label=name)
    set_limited_xticks(ax, list(range(max_len)), [r["time"].strftime("%m/%d %H:%M") for r in label_recs], force_all=(len(plot_data) <= 3))
    graph_title = "みんなのXP比較グラフ" if is_all_compare else "指定メンバーのXP比較グラフ"
    ax.set_title(f"{graph_title} ({title})", fontsize=15)
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=8)
    plt.tight_layout()
    fname = f"comp_{interaction.user.id}_{int(time.time())}.png"
    try:
        plt.savefig(fname, dpi=100)
        plt.close(fig)
        await interaction.followup.send(content=omitted_msg if omitted_msg else None, file=discord.File(fname))
    finally:
        if os.path.exists(fname):
            os.remove(fname)


@client.tree.command(name="ランキング", description="XPランキングを表示します")
@app_commands.describe(期間="例：「5月」「夏シーズン」「全期間」「全記録」など")
async def ranking(interaction: discord.Interaction, 期間: str = None):
    await interaction.response.defer()
    now = datetime.now(JST)
    sy, st = get_current_season(now)
    ty, ts, tm, ia, title, cont = parse_args_from_str(期間, sy, st, True)
    all_d = await get_records_for_period(ty, ts, tm, ia, cont)
    ranking_list = []
    for uid, info in all_d.items():
        recs = info["records"]
        if not ia and not cont:
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
    all_d = await get_records_for_period(str(target_year), target_season, None, False)
    most_played, last_spurt = [], []
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
    await interaction.followup.send(f"🎉 **{target_year}年 {target_season} 表彰式** 🎉\n\n🦑 **一番潜ったで賞**: {most_played[0][0]}さん ({most_played[0][1]}回)\n🔥 **ラストスパート賞**: {last_spurt[0][0]}さん (+{last_spurt[0][1]} XP)")


@client.tree.command(name="リセット", description="自分の直近1件のXP記録を取り消します")
async def reset_last(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    deleted = False
    async with cache_lock:
        rec = _cache_find_latest_log_record(interaction.user.id)
        if rec:
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
            except Exception:
                pass
            _cache_remove_record(rec)
            deleted = True
    await interaction.followup.send("🗑️ 直近の未アーカイブ記録を1件リセットしました！" if deleted else "⚠️ 削除対象がありません。すでにアーカイブ済みの記録はこのコマンドでは消せません。")


@client.tree.command(name="マイデータ全削除", description="自分の未アーカイブデータを消去します")
@app_commands.describe(確認="本当に削除する場合は DELETE と入力")
async def delete_my_data(interaction: discord.Interaction, 確認: str):
    await interaction.response.defer(ephemeral=True)
    if 確認 != "DELETE":
        await interaction.followup.send("⚠️ 確認文字列が違います。削除するには `DELETE` と入力してください。")
        return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    deleted_count = 0
    async with cache_lock:
        for rec in list(_cache_user_log_records(interaction.user.id)):
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
                deleted_count += 1
                _cache_remove_record(rec)
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception:
                pass
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
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    deleted_count = 0
    async with cache_lock:
        for rec in list(_cache_user_log_records(対象.id)):
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
                deleted_count += 1
                _cache_remove_record(rec)
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception:
                pass
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
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    deleted_count = 0
    async with cache_lock:
        for rec in list(_cache_all_log_records()):
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
                deleted_count += 1
                _cache_remove_record(rec)
                await asyncio.sleep(DELETE_SLEEP_SECONDS)
            except Exception:
                pass
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
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await message.channel.send("⚠️ ログチャンネルが見つかりません。")
        return
    await ensure_cache()
    async with cache_lock:
        all_d = CACHE_BY_USER
        personal_best_xp = max([r["xp"] for r in all_d.get(message.author.id, {"records": []})["records"]], default=None)
        current_season_xps = {}
        for uid, info in all_d.items():
            recs = [r for r in info["records"] if is_record_in_period(r["time"], str(season_year), current_season_type, None)]
            if recs:
                current_season_xps[uid] = (info["name"], recs[-1]["xp"])
        old_xp = current_season_xps.get(message.author.id, (message.author.display_name, None))[1]
        if old_xp is not None and abs(new_xp - old_xp) > 500:
            await message.channel.send(f"⚠️ 今シーズン前回記録({old_xp} XP)から±500以上の急激な増減があるため保存できません！")
            return
    splat_time = parse_specified_time(message.content, now)
    is_confident = True
    if not splat_time:
        splat_time = await update_and_get_last_area_time(now)
    if not splat_time:
        splat_time = get_last_splat_end_time(now)
        is_confident = False
    record_season_year, record_season_type = get_record_season_for_shift_end(splat_time)
    record_season_name = season_full(record_season_year, record_season_type)
    sent = await log_channel.send(make_record_json(message.author.id, message.author.display_name, new_xp, splat_time, record_season_name, message.id))
    async with cache_lock:
        rec = _new_cache_rec({"user_id": message.author.id, "user_name": message.author.display_name, "xp": new_xp, "time": splat_time, "season": record_season_name, "message_id": message.id}, "log", sent.id)
        _cache_add_single(rec)
        updated_xps = current_season_xps.copy()
        updated_xps[message.author.id] = (message.author.display_name, new_xp)
        passed_users, overtaken_users = [], []
        for uid, (name, xp) in current_season_xps.items():
            if uid == message.author.id:
                continue
            if (old_xp is not None and xp >= old_xp and new_xp > xp) or (old_xp is None and new_xp > xp):
                passed_users.append(name)
            if old_xp is not None and xp < old_xp and new_xp < xp:
                overtaken_users.append(name)
        sorted_ranking = sorted(updated_xps.items(), key=lambda x: x[1][1], reverse=True)
        my_index = next((i for i, (uid, _) in enumerate(sorted_ranking) if uid == message.author.id), 0)
        active_goal = CACHE_GOALS.get(message.author.id, {}).get(record_season_name)
        if active_goal and not active_goal.get("active", True):
            active_goal = None
    goal_msg = ""
    if active_goal:
        target_xp = active_goal["target_xp"]
        if new_xp >= target_xp and (old_xp is None or old_xp < target_xp):
            goal_msg += f"\n🎯 **目標達成！** 目標 **{target_xp} XP** を突破！"
        elif new_xp < target_xp:
            remain = target_xp - new_xp
            if remain <= 100:
                goal_msg += f"\n🎯 目標 **{target_xp} XP** まであと **{remain} XP**！"
    drama_msg = ""
    if BOT_SETTINGS.get("drama_enabled", True):
        drama_msg += build_power_change_message(old_xp, new_xp)
        if personal_best_xp is None or new_xp > personal_best_xp:
            drama_msg += f"\n🏅 **自己ベスト更新！** {new_xp} XP！"
        if passed_users:
            drama_msg += f"\n⚔️ **【下剋上】** {'、'.join(passed_users)}さんをブチ抜きました！"
        elif overtaken_users:
            drama_msg += f"\n😱 **【悲報】** {'、'.join(overtaken_users)}さんに抜かされてしまいました…"
        if my_index == 0:
            drama_msg += "\n👑 **現在トップ独走中！**"
            if len(sorted_ranking) > 1:
                _, (next_name, next_xp) = sorted_ranking[1]
                drama_msg += f"（2位の{next_name}さんとは **XP {new_xp - next_xp}** 差）"
        else:
            _, (above_name, above_xp) = sorted_ranking[my_index - 1]
            drama_msg += f"\n🎯 1つ上の{above_name}さんまであと **XP {above_xp - new_xp}**！"
    start_time = splat_time - timedelta(hours=2)
    notice = f"（記録枠：{start_time.strftime('%m/%d %H:%M')}-{splat_time.strftime('%H:%M')}）"
    if not is_confident:
        notice += "\n💡 ※時間が違った場合は、チャットを編集して『17:00』のように終了時間を書き足してください！"
    area_msg = ""
    if BOT_SETTINGS.get("area_notice_enabled", True):
        next_area = await get_next_area_shift(now)
        if next_area:
            ns, ne = next_area["start"], next_area["end"]
            stage_text = " / ".join(next_area["stages"]) if next_area["stages"] else "ステージ情報なし"
            area_msg = f"\n\n🗓️ **次のガチエリア**\n**{ns.strftime('%m/%d %H:%M')} - {ne.strftime('%H:%M')}**\n🗺️ ステージ：**{stage_text}**"
        else:
            area_msg = "\n\n🗓️ **次のガチエリア**\n現在、次回エリア情報を取得できませんでした。"
    await message.channel.send(f"✅ {new_xp} XP を保存しました！{notice}{drama_msg}{goal_msg}{area_msg}")
    success, archived_count, _ = await auto_archive_if_needed(force=False)
    if success and archived_count:
        await message.channel.send(f"📦 条件を満たした過去シーズンログ **{archived_count}件** をシーズン別アーカイブしました！")


@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        return
    await ensure_cache()
    async with cache_lock:
        rec = CACHE_BY_SOURCE.get(payload.message_id)
        if not rec:
            return
        try:
            await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
        except Exception:
            pass
        _cache_remove_record(rec)


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
    await ensure_cache()
    async with cache_lock:
        rec = CACHE_BY_SOURCE.get(payload.message_id)
        if not rec:
            return
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000):
                if target_channel:
                    await target_channel.send("⚠️ 編集後のパワーも500〜5000で入力してください！")
                return
            rec["xp"] = new_xp
            spec_time = parse_specified_time(content, datetime.now(JST))
            if spec_time:
                rec["time"] = spec_time
                sy, st = get_record_season_for_shift_end(spec_time)
                rec["season"] = season_full(sy, st)
                CACHE_BY_USER[rec["user_id"]]["records"].sort(key=lambda x: x["time"])
                if target_channel:
                    start_time = spec_time - timedelta(hours=2)
                    await target_channel.send(f"🔄 記録枠を **{start_time.strftime('%H:%M')}ー{spec_time.strftime('%H:%M')}** に変更しました！")
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).edit(content=record_to_log_content(rec))
            except Exception:
                pass
        else:
            try:
                await log_channel.get_partial_message(rec["_log_msg_id"]).delete()
            except Exception:
                pass
            _cache_remove_record(rec)


client.run(TOKEN)
