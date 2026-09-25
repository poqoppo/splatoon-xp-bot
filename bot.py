import asyncio
import io
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from threading import Thread

import discord
from discord import app_commands
from flask import Flask

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# Splatoon 3 XP Management Discord Bot
# Rate-limit safe / local JSON + Discord backup + season archive version
#
# IMPORTANT:
# - XP/goal/settings data are stored locally in XP_DATA_FILE.
# - Startup and graph commands NEVER read channel.history().
# - Discord log messages are retained as a disaster-recovery backup; graph commands do not read them.
# - Area schedule is cached for 15 minutes.
# - Slash commands have cooldowns.
# - Existing Discord history is imported only by the explicit admin /データ復旧 command.
#   This prevents startup/API bursts and makes local storage the source of truth.
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "I am alive!"


def run_flask():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        debug=False,
        use_reloader=False,
    )


Thread(target=run_flask, daemon=True).start()


# -----------------------------
# Configuration
# -----------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が設定されていません")

TARGET_CHANNEL_ID = int(os.environ.get("TARGET_CHANNEL_ID", "1474973509217423401"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "1508739566138294522"))
ARCHIVE_CHANNEL_ID = int(os.environ.get("ARCHIVE_CHANNEL_ID", "1510291838957912285"))

ADMIN_USERS = ["poqoppo", "ricekei"]
ADMIN_USER_IDS = [
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
]

JST = timezone(timedelta(hours=9), "JST")

DATA_DIR = os.environ.get("XP_DATA_DIR", "xp_data")
XP_DATA_FILE = os.path.join(DATA_DIR, "xp_data.json")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archives")

ARCHIVE_THRESHOLD = int(os.environ.get("ARCHIVE_THRESHOLD", "4500"))
ARCHIVE_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("ARCHIVE_CHECK_INTERVAL_SECONDS", "1800")
)
ARCHIVE_SEASON_GRACE_DAYS = int(
    os.environ.get("ARCHIVE_SEASON_GRACE_DAYS", "1")
)
ARCHIVE_MAX_RECORDS_PER_FILE = int(
    os.environ.get("ARCHIVE_MAX_RECORDS_PER_FILE", "4500")
)
ARCHIVE_MAX_PARTS_PER_SEASON = int(
    os.environ.get("ARCHIVE_MAX_PARTS_PER_SEASON", "2")
)

DELETE_SLEEP_SECONDS = float(os.environ.get("DELETE_SLEEP_SECONDS", "0.35"))

MAX_POINTS_PER_USER = int(os.environ.get("MAX_POINTS_PER_USER", "200"))
MAX_XTICK_LABELS = int(os.environ.get("MAX_XTICK_LABELS", "30"))
COMPARE_ALL_MAX_USERS = int(os.environ.get("COMPARE_ALL_MAX_USERS", "30"))

# External API cache: 15 minutes.
SCHEDULE_CACHE_SECONDS = int(
    os.environ.get("SCHEDULE_CACHE_SECONDS", "900")
)

# A small guard for command use immediately after connection.
STARTUP_GUARD_SECONDS = float(
    os.environ.get("STARTUP_GUARD_SECONDS", "8")
)

DEFAULT_SETTINGS = {
    "drama_enabled": True,
    "area_notice_enabled": True,
}

BOT_SETTINGS = DEFAULT_SETTINGS.copy()

# -----------------------------
# In-memory state
# -----------------------------
CACHE_BY_USER = {}
CACHE_BY_SOURCE = {}
CACHE_GOALS = {}
CACHE_SETTINGS_MSG = None

DATA_LOCK = asyncio.Lock()
API_LOCK = asyncio.Lock()
ARCHIVE_LOCK = asyncio.Lock()

DATA_READY = False
BOT_READY = False
READY_AT_MONO = 0.0
LAST_ARCHIVE_CHECK = 0.0
RESTORE_MAX_MESSAGES = int(os.environ.get("RESTORE_MAX_MESSAGES", "20000"))
ARCHIVE_REQUIRE_DISCORD_UPLOAD = os.environ.get("ARCHIVE_REQUIRE_DISCORD_UPLOAD", "1") == "1"

CACHED_AREA_SHIFTS = set()
CACHED_AREA_DETAILS = []
LAST_SCHEDULE_FETCH = None

# Sending/deleting too many messages concurrently is another common source
# of rate-limit pressure. Keep these operations serialized.
DISCORD_WRITE_LOCK = asyncio.Lock()


# ============================================================
# Local JSON persistence
# ============================================================

def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)


def empty_database():
    return {
        "version": 3,
        "updated_at": datetime.now(JST).isoformat(),
        "records": [],
        "goals": [],
        "settings": DEFAULT_SETTINGS.copy(),
        "area_cache": {"fetched_at": None, "details": []},
    }


def load_database_sync():
    ensure_data_dirs()
    if not os.path.exists(XP_DATA_FILE):
        db = empty_database()
        save_database_sync(db)
        return db

    try:
        with open(XP_DATA_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception as e:
        print(f"Local DB read error: {e}")
        # Never overwrite a damaged database automatically.
        backup = XP_DATA_FILE + f".broken_{int(time.time())}"
        try:
            os.replace(XP_DATA_FILE, backup)
            print(f"壊れたDBを退避しました: {backup}")
        except Exception:
            pass
        db = empty_database()
        save_database_sync(db)

    if not isinstance(db, dict):
        db = empty_database()

    db.setdefault("version", 2)
    db.setdefault("records", [])
    db.setdefault("goals", [])
    db.setdefault("settings", DEFAULT_SETTINGS.copy())
    db["settings"] = {
        **DEFAULT_SETTINGS,
        **(db.get("settings") or {}),
    }
    return db


def save_database_sync(db):
    ensure_data_dirs()
    db = dict(db)
    db["updated_at"] = datetime.now(JST).isoformat()

    tmp = XP_DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, XP_DATA_FILE)


def load_archive_records_sync():
    """Read season archive JSON files from the local archive directory.

    This is local filesystem I/O only. It never calls Discord APIs.
    Archived records are loaded as a fallback when the main DB is missing
    or incomplete, which is useful when the app is deployed on a persistent disk.
    """
    ensure_data_dirs()
    results = []
    seen = set()

    try:
        names = sorted(os.listdir(ARCHIVE_DIR))
    except Exception as e:
        print(f"Archive directory read error: {e}")
        return results

    for name in names:
        if not name.endswith('.json') or not name.startswith('xp_archive_'):
            continue
        path = os.path.join(ARCHIVE_DIR, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                obj = json.load(f)
        except Exception as e:
            print(f"Archive file read error ({name}): {e}")
            continue

        for item in obj.get('records', []):
            rec = parse_db_record({
                **item,
                'type': 'xp_record',
                'archived': True,
            })
            if not rec:
                continue
            key = make_unique_key(rec)
            if key in seen:
                continue
            seen.add(key)
            rec['_source'] = 'archive_file'
            rec['_log_msg_id'] = None
            rec['archived'] = True
            results.append(rec)

    return results


async def load_local_database():
    global DATA_READY, BOT_SETTINGS
    global CACHED_AREA_DETAILS, CACHED_AREA_SHIFTS, LAST_SCHEDULE_FETCH

    async with DATA_LOCK:
        if DATA_READY:
            return

        db = await asyncio.to_thread(load_database_sync)
        BOT_SETTINGS.clear()
        BOT_SETTINGS.update({
            **DEFAULT_SETTINGS,
            **db.get("settings", {}),
        })

        rebuild_memory_from_db(db)

        # If the main DB was lost but archive JSON files still exist on a
        # persistent disk, merge them locally. No Discord history is read.
        local_keys = {
            make_unique_key(r)
            for r in current_cache_records_as_parsed()
        }
        archive_records = await asyncio.to_thread(load_archive_records_sync)
        for rec in archive_records:
            if make_unique_key(rec) not in local_keys:
                _cache_insert(rec)
                local_keys.add(make_unique_key(rec))

        # Restore the 15-minute schedule cache if it was persisted.
        area_cache = db.get("area_cache") or {}
        fetched_text = area_cache.get("fetched_at")
        details = []
        if fetched_text:
            try:
                fetched_at = datetime.fromisoformat(str(fetched_text))
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=JST)
                fetched_at = fetched_at.astimezone(JST)
                for item in area_cache.get("details", []):
                    st = parse_api_datetime(item["start"])
                    et = parse_api_datetime(item["end"])
                    details.append({
                        "start": st,
                        "end": et,
                        "stages": list(item.get("stages", [])),
                    })
                CACHED_AREA_DETAILS = sorted(details, key=lambda x: x["start"])
                CACHED_AREA_SHIFTS = {(d["start"], d["end"]) for d in CACHED_AREA_DETAILS}
                LAST_SCHEDULE_FETCH = fetched_at
            except Exception as e:
                print(f"Area cache restore error: {e}")

        DATA_READY = True


def database_snapshot_sync():
    db = empty_database()
    db["settings"] = BOT_SETTINGS.copy()

    records = []
    for info in CACHE_BY_USER.values():
        for rec in info.get("records", []):
            records.append(record_to_db_obj(rec))
    db["records"] = records

    goals = []
    for uid_goals in CACHE_GOALS.values():
        for goal in uid_goals.values():
            goals.append(dict(goal))
    db["goals"] = goals

    if LAST_SCHEDULE_FETCH and CACHED_AREA_DETAILS:
        db["area_cache"] = {
            "fetched_at": LAST_SCHEDULE_FETCH.astimezone(JST).isoformat(),
            "details": [
                {
                    "start": d["start"].astimezone(JST).isoformat(),
                    "end": d["end"].astimezone(JST).isoformat(),
                    "stages": list(d.get("stages", [])),
                }
                for d in CACHED_AREA_DETAILS
            ],
        }
    else:
        db["area_cache"] = {"fetched_at": None, "details": []}

    return db


async def save_local_database():
    # Called while DATA_LOCK is normally held by callers where necessary.
    db = await asyncio.to_thread(database_snapshot_sync)
    await asyncio.to_thread(save_database_sync, db)


def rebuild_memory_from_db(db):
    CACHE_BY_USER.clear()
    CACHE_BY_SOURCE.clear()
    CACHE_GOALS.clear()

    for obj in db.get("records", []):
        rec = parse_db_record(obj)
        if not rec:
            continue
        rec["_source"] = "local"
        rec["_log_msg_id"] = (
            int(obj.get("log_message_id", 0))
            if str(obj.get("log_message_id", "")).isdigit()
            else None
        )
        _cache_insert(rec)

    for goal in db.get("goals", []):
        try:
            uid = int(goal["user_id"])
            season = str(goal["season"])
            CACHE_GOALS.setdefault(uid, {})[season] = {
                "user_id": uid,
                "user_name": str(goal.get("user_name", f"ID:{uid}")),
                "target_xp": int(goal["target_xp"]),
                "season": season,
                "created_at": str(goal.get("created_at", "")),
                "active": bool(goal.get("active", True)),
            }
        except Exception:
            continue

    for uid in CACHE_BY_USER:
        CACHE_BY_USER[uid]["records"].sort(key=lambda x: x["time"])


def record_to_db_obj(record):
    return {
        "type": "xp_record",
        "user_id": int(record["user_id"]),
        "user_name": str(record["user_name"]),
        "xp": int(record["xp"]),
        "time": record["time"].astimezone(JST).isoformat(),
        "season": str(record["season"]),
        "message_id": int(record.get("message_id", 0)),
        "log_message_id": int(record.get("_log_msg_id", 0) or 0),
        "archived": bool(record.get("archived", False)),
    }


def parse_db_record(obj):
    try:
        if obj.get("type") != "xp_record":
            return None
        dt = datetime.fromisoformat(str(obj["time"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        dt = dt.astimezone(JST)

        return {
            "user_id": int(obj["user_id"]),
            "user_name": str(obj.get("user_name", f"ID:{obj['user_id']}")),
            "xp": int(obj["xp"]),
            "time": dt,
            "season": str(obj["season"]),
            "message_id": int(obj.get("message_id", 0)),
            "_log_msg_id": int(obj.get("log_message_id", 0) or 0) or None,
            "_source": "local",
            "archived": bool(obj.get("archived", False)),
        }
    except Exception:
        return None


# ============================================================
# Season / time helpers
# ============================================================

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
        end = (
            datetime(y + 1, 1, 1, tzinfo=JST)
            if month_int == 12
            else datetime(y, month_int + 1, 1, tzinfo=JST)
        )
        return start, end

    if season_str:
        if "春" in season_str:
            return (
                datetime(y, 3, 1, 9, 0, tzinfo=JST),
                datetime(y, 6, 1, 9, 0, tzinfo=JST),
            )
        if "夏" in season_str:
            return (
                datetime(y, 6, 1, 9, 0, tzinfo=JST),
                datetime(y, 9, 1, 9, 0, tzinfo=JST),
            )
        if "秋" in season_str:
            return (
                datetime(y, 9, 1, 9, 0, tzinfo=JST),
                datetime(y, 12, 1, 9, 0, tzinfo=JST),
            )
        if "冬" in season_str:
            return (
                datetime(y, 12, 1, 9, 0, tzinfo=JST),
                datetime(y + 1, 3, 1, 9, 0, tzinfo=JST),
            )
    return None, None


def get_season_end(year, season_type):
    _, end = get_graph_bounds(str(year), season_type)
    return end


def is_record_in_period(record_time, year_str, season_str=None, month_int=None):
    if month_int:
        return record_time.year == int(year_str) and record_time.month == month_int

    if season_str:
        start, end = get_graph_bounds(year_str, season_str)
        return bool(start and end and start <= record_time < end)

    return True


def is_archive_eligible_season(season_name, now_dt=None):
    now_dt = now_dt or datetime.now(JST)
    m = re.match(
        r"(\d{4})年\s*(春シーズン|夏シーズン|秋シーズン|冬シーズン)",
        season_name,
    )
    if not m:
        return False

    y, s = int(m.group(1)), m.group(2)
    end = get_season_end(y, s)
    return bool(
        end and now_dt >= end + timedelta(days=ARCHIVE_SEASON_GRACE_DAYS)
    )


def get_candidate_seasons_for_month(year, month):
    y = int(year)
    if month in (1, 2):
        return [season_full(y - 1, "冬シーズン")]
    if month == 3:
        return [
            season_full(y - 1, "冬シーズン"),
            season_full(y, "春シーズン"),
        ]
    if month in (4, 5):
        return [season_full(y, "春シーズン")]
    if month == 6:
        return [
            season_full(y, "春シーズン"),
            season_full(y, "夏シーズン"),
        ]
    if month in (7, 8):
        return [season_full(y, "夏シーズン")]
    if month == 9:
        return [
            season_full(y, "夏シーズン"),
            season_full(y, "秋シーズン"),
        ]
    if month in (10, 11):
        return [season_full(y, "秋シーズン")]
    if month == 12:
        return [
            season_full(y, "秋シーズン"),
            season_full(y, "冬シーズン"),
        ]
    return []


def parse_args_from_str(
    text,
    current_year,
    current_season_type,
    default_to_current_season=False,
):
    if not text:
        if default_to_current_season:
            return (
                str(current_year),
                current_season_type,
                None,
                False,
                f"{current_year}年 {current_season_type}",
                False,
            )
        text = ""

    text = text.strip()
    is_continuous = "通し" in text or "やった日から" in text

    if text == "全期間":
        return (
            str(current_year),
            current_season_type,
            None,
            False,
            f"{current_year}年 {current_season_type}",
            is_continuous,
        )

    if text in ["全記録", "全シーズン", "すべて", "全部", "all", "ALL"]:
        return (
            str(current_year),
            None,
            None,
            True,
            "全記録",
            is_continuous,
        )

    target_year = str(current_year)
    year_match = re.search(r"([0-9]{4})年", text)
    if year_match:
        target_year = year_match.group(1)

    month_match = re.search(r"([0-9]{1,2})月", text)
    season_match = None
    for s in [
        "春シーズン",
        "夏シーズン",
        "秋シーズン",
        "冬シーズン",
        "春",
        "夏",
        "秋",
        "冬",
    ]:
        if s in text:
            season_match = (
                s if "シーズン" in s else f"{s}シーズン"
            )
            break

    if month_match:
        month = int(month_match.group(1))
        if 1 <= month <= 12:
            return (
                target_year,
                None,
                month,
                False,
                f"{target_year}年 {month}月",
                is_continuous,
            )

    if season_match:
        return (
            target_year,
            season_match,
            None,
            False,
            f"{target_year}年 {season_match}",
            is_continuous,
        )

    if default_to_current_season:
        return (
            str(current_year),
            current_season_type,
            None,
            False,
            f"{current_year}年 {current_season_type}",
            is_continuous,
        )

    return target_year, None, None, True, "全記録", is_continuous


# ============================================================
# XP record helpers
# ============================================================

def make_record_json(
    user_id,
    user_name,
    xp,
    record_time,
    season,
    message_id,
):
    return json.dumps(
        {
            "type": "xp_record",
            "user_id": int(user_id),
            "user_name": str(user_name),
            "xp": int(xp),
            "time": record_time.strftime("%Y/%m/%d %H:%M"),
            "season": str(season),
            "message_id": int(message_id),
        },
        ensure_ascii=False,
    )


def make_goal_json(user_id, user_name, target_xp, season, active=True):
    return json.dumps(
        {
            "type": "xp_goal",
            "user_id": int(user_id),
            "user_name": str(user_name),
            "target_xp": int(target_xp),
            "season": str(season),
            "created_at": datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
            "active": bool(active),
        },
        ensure_ascii=False,
    )


def make_unique_key(record):
    message_id = int(record.get("message_id", 0))
    if message_id:
        return f"msg:{message_id}"
    return (
        f"fallback:{record['user_id']}:"
        f"{record['time'].strftime('%Y/%m/%d %H:%M')}:"
        f"{record['xp']}:{record['season']}"
    )


def _new_cache_rec(
    parsed,
    source="local",
    log_msg_id=None,
    archived=False,
):
    return {
        "user_id": int(parsed["user_id"]),
        "user_name": str(parsed["user_name"]),
        "xp": int(parsed["xp"]),
        "time": parsed["time"],
        "season": str(parsed["season"]),
        "message_id": int(parsed.get("message_id", 0)),
        "_source": source,
        "_log_msg_id": log_msg_id,
        "archived": bool(archived),
    }


def _cache_insert(rec):
    uid = rec["user_id"]
    if uid not in CACHE_BY_USER:
        CACHE_BY_USER[uid] = {
            "name": rec["user_name"],
            "records": [],
        }

    if rec["user_name"] != f"ID:{uid}":
        CACHE_BY_USER[uid]["name"] = rec["user_name"]

    # Avoid duplicate insertion.
    key = make_unique_key(rec)
    existing = None
    for old in CACHE_BY_USER[uid]["records"]:
        if make_unique_key(old) == key:
            existing = old
            break

    if existing is not None:
        existing.update(rec)
        if rec.get("message_id"):
            CACHE_BY_SOURCE[rec["message_id"]] = existing
        return existing

    CACHE_BY_USER[uid]["records"].append(rec)

    if rec.get("message_id"):
        CACHE_BY_SOURCE[rec["message_id"]] = rec

    return rec


def _cache_add_single(rec):
    result = _cache_insert(rec)
    CACHE_BY_USER[rec["user_id"]]["records"].sort(
        key=lambda x: x["time"]
    )
    return result


def _cache_remove_record(rec):
    uid = rec["user_id"]

    if uid in CACHE_BY_USER:
        try:
            CACHE_BY_USER[uid]["records"].remove(rec)
        except ValueError:
            pass

        if not CACHE_BY_USER[uid]["records"]:
            del CACHE_BY_USER[uid]

    mid = rec.get("message_id")
    if mid and CACHE_BY_SOURCE.get(mid) is rec:
        del CACHE_BY_SOURCE[mid]


def _cache_find_latest_log_record(uid):
    candidates = [
        r
        for r in CACHE_BY_USER.get(uid, {}).get("records", [])
        if r.get("_log_msg_id") and not r.get("archived", False)
    ]
    return max(
        candidates,
        key=lambda r: r["time"],
        default=None,
    )


def _cache_user_log_records(uid):
    return [
        r
        for r in CACHE_BY_USER.get(uid, {}).get("records", [])
        if r.get("_log_msg_id") and not r.get("archived", False)
    ]


def _cache_all_log_records():
    out = []
    for info in CACHE_BY_USER.values():
        out.extend(
            r
            for r in info["records"]
            if r.get("_log_msg_id") and not r.get("archived", False)
        )
    return out


def _cache_set_goal(goal):
    uid = int(goal["user_id"])
    season = str(goal["season"])
    CACHE_GOALS.setdefault(uid, {})[season] = goal


def _cache_remove_goal(uid, season):
    if uid in CACHE_GOALS:
        CACHE_GOALS[uid].pop(season, None)
        if not CACHE_GOALS[uid]:
            del CACHE_GOALS[uid]


def _count_cache_records():
    current = 0
    archived = 0
    for info in CACHE_BY_USER.values():
        for r in info["records"]:
            if r.get("archived"):
                archived += 1
            else:
                current += 1
    return current, archived


def current_cache_records_as_parsed():
    records = []
    for info in CACHE_BY_USER.values():
        records.extend(info["records"])
    return records


async def get_active_goal(user_id, season):
    await load_local_database()
    goal = CACHE_GOALS.get(user_id, {}).get(season)
    if not goal or not goal.get("active", True):
        return None
    return goal


# ============================================================
# External API: cached Splatoon 3 schedule
# ============================================================

def parse_api_datetime(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


async def fetch_area_schedule(force=False):
    global CACHED_AREA_SHIFTS
    global CACHED_AREA_DETAILS
    global LAST_SCHEDULE_FETCH

    now = datetime.now(JST)

    if (
        not force
        and LAST_SCHEDULE_FETCH is not None
        and (now - LAST_SCHEDULE_FETCH).total_seconds() < SCHEDULE_CACHE_SECONDS
        and CACHED_AREA_DETAILS
    ):
        return True

    async with API_LOCK:
        now = datetime.now(JST)
        if (
            not force
            and LAST_SCHEDULE_FETCH is not None
            and (now - LAST_SCHEDULE_FETCH).total_seconds() < SCHEDULE_CACHE_SECONDS
            and CACHED_AREA_DETAILS
        ):
            return True

        try:
            req = urllib.request.Request(
                "https://spla3.yuu26.com/api/x/schedule",
                headers={
                    "User-Agent": "Splatoon3-XP-Bot/5.0",
                    "Accept": "application/json",
                },
            )

            def fetch():
                with urllib.request.urlopen(req, timeout=10) as res:
                    return res.status, dict(res.headers), res.read()

            status, headers, raw = await asyncio.to_thread(fetch)
            content_type = str(headers.get("Content-Type", "")).lower()
            cf_mitigated = str(headers.get("cf-mitigated", "")).lower()
            text_body = raw[:500].decode("utf-8", errors="replace").lstrip().lower()

            if status != 200:
                raise RuntimeError(f"HTTP {status}")

            if (
                cf_mitigated == "challenge"
                or "text/html" in content_type
                or "/cdn-cgi/challenge-platform/" in text_body
                or "<html" in text_body
            ):
                raise RuntimeError(
                    "API returned a Cloudflare/HTML challenge instead of JSON"
                )

            if "json" not in content_type and not text_body.startswith(("{", "[")):
                raise RuntimeError(
                    f"API returned unexpected Content-Type: {content_type or 'unknown'}"
                )

            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("API response is not a JSON object")

            new_shifts = set()
            new_details = []

            for node in data.get("results", []):
                if node.get("rule", {}).get("key") != "AREA":
                    continue

                try:
                    st = parse_api_datetime(node["start_time"])
                    et = parse_api_datetime(node["end_time"])
                except Exception:
                    continue

                stages = [
                    str(s.get("name", "不明ステージ"))
                    for s in node.get("stages", [])
                ]

                new_shifts.add((st, et))
                new_details.append({
                    "start": st,
                    "end": et,
                    "stages": stages,
                })

            new_details.sort(key=lambda x: x["start"])
            if not new_details:
                raise RuntimeError("API JSON contained no AREA schedule")

            CACHED_AREA_SHIFTS = new_shifts
            CACHED_AREA_DETAILS = new_details
            LAST_SCHEDULE_FETCH = now

            print(
                f"ガチエリアスケジュール更新: {len(new_details)}件 / "
                f"キャッシュ{SCHEDULE_CACHE_SECONDS}秒"
            )
            return True

        except Exception as e:
            # Keep the last valid cache. An external API failure must never
            # terminate the Discord bot or prevent XP from being saved.
            print(f"[AREA API] 取得失敗（既存キャッシュを維持）: {e}")
            return bool(CACHED_AREA_DETAILS)


async def update_and_get_last_area_time(now_dt):
    await fetch_area_schedule()
    best_et = None

    for st, et in CACHED_AREA_SHIFTS:
        if st <= now_dt and (
            best_et is None or et > best_et
        ):
            best_et = et

    return best_et


async def get_next_area_shift(now_dt):
    await fetch_area_schedule()

    candidates = [
        d for d in CACHED_AREA_DETAILS
        if d["start"] > now_dt
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda x: x["start"])


def get_last_splat_end_time(dt):
    h = dt.hour

    if h % 2 == 0:
        h -= 1

    if h < 0:
        h = 23
        dt -= timedelta(days=1)

    return dt.replace(
        hour=h,
        minute=0,
        second=0,
        microsecond=0,
    )


def normalize_specified_time(candidate, now_dt):
    return candidate - timedelta(days=1) if candidate > now_dt else candidate


def parse_specified_time(content, now_dt):
    m = re.search(r"([0-9]{1,2}):([0-9]{2})", content)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= minute < 60:
            return normalize_specified_time(
                now_dt.replace(
                    hour=h,
                    minute=minute,
                    second=0,
                    microsecond=0,
                ),
                now_dt,
            )

    m = re.search(r"([0-9]{1,2})時", content)
    if m:
        h = int(m.group(1))
        if 0 <= h < 24:
            return normalize_specified_time(
                now_dt.replace(
                    hour=h,
                    minute=0,
                    second=0,
                    microsecond=0,
                ),
                now_dt,
            )

    return None


# ============================================================
# Data queries
# ============================================================

def build_data_from_records(records):
    data = {}
    seen = set()

    for record in records:
        key = make_unique_key(record)
        if key in seen:
            continue
        seen.add(key)

        uid = int(record["user_id"])
        uname = str(record["user_name"])

        if uid not in data:
            data[uid] = {
                "name": uname,
                "records": [],
            }

        if uname != f"ID:{uid}":
            data[uid]["name"] = uname

        data[uid]["records"].append(
            {
                "user_id": uid,
                "user_name": uname,
                "xp": int(record["xp"]),
                "time": record["time"],
                "season": str(record["season"]),
                "message_id": int(record.get("message_id", 0)),
                "msg_id": int(record.get("message_id", 0)),
                "_source": record.get("_source", "local"),
                "archived": bool(record.get("archived", False)),
            }
        )

    for uid in data:
        data[uid]["records"].sort(key=lambda x: x["time"])

    return data


async def get_records_for_period(
    year_str,
    season_str=None,
    month_int=None,
    include_all=False,
    is_continuous=False,
):
    await load_local_database()
    return build_data_from_records(
        current_cache_records_as_parsed()
    )


async def get_all_records():
    await load_local_database()
    return build_data_from_records(
        current_cache_records_as_parsed()
    )


def thin_records_for_plot(
    recs,
    max_points=MAX_POINTS_PER_USER,
):
    if len(recs) <= max_points:
        return recs

    if max_points <= 2:
        return [recs[0], recs[-1]]

    step = (len(recs) - 1) / (max_points - 1)
    idxs = sorted(
        {round(i * step) for i in range(max_points)}
    )
    return [recs[i] for i in idxs]


def set_limited_xticks(
    ax,
    indices,
    labels,
    force_all=False,
):
    if not indices:
        return

    if force_all or len(indices) <= MAX_XTICK_LABELS:
        show = list(range(len(indices)))
    else:
        step = (len(indices) - 1) / (
            MAX_XTICK_LABELS - 1
        )
        show = sorted(
            {
                round(i * step)
                for i in range(MAX_XTICK_LABELS)
            }
        )

    ax.set_xticks([indices[i] for i in show])
    ax.set_xticklabels(
        [labels[i] for i in show],
        rotation=90,
        fontsize=9,
    )


# ============================================================
# Discord API helpers
# ============================================================

async def safe_send(channel, *args, **kwargs):
    async with DISCORD_WRITE_LOCK:
        return await channel.send(*args, **kwargs)


async def safe_delete_message(channel, message_id):
    if not channel or not message_id:
        return False

    async with DISCORD_WRITE_LOCK:
        try:
            await channel.get_partial_message(
                int(message_id)
            ).delete()
            return True
        except discord.NotFound:
            return True
        except discord.HTTPException as e:
            print(f"Discord delete HTTP error: {e}")
            return False
        except Exception as e:
            print(f"Discord delete error: {e}")
            return False


async def safe_edit_message(channel, message_id, content):
    if not channel or not message_id:
        return False

    async with DISCORD_WRITE_LOCK:
        try:
            await channel.get_partial_message(
                int(message_id)
            ).edit(content=content)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException as e:
            print(f"Discord edit HTTP error: {e}")
            return False
        except Exception as e:
            print(f"Discord edit error: {e}")
            return False


def bot_is_ready_for_commands():
    if not BOT_READY:
        return False

    if time.monotonic() - READY_AT_MONO < STARTUP_GUARD_SECONDS:
        return False

    return True


async def require_ready(interaction):
    if bot_is_ready_for_commands():
        return True

    if interaction.response.is_done():
        await interaction.followup.send(
            "⏳ Bot起動直後のため、もう少し待ってから実行してください。",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "⏳ Bot起動直後のため、もう少し待ってから実行してください。",
            ephemeral=True,
        )

    return False


# ============================================================
# Archive
# ============================================================

def safe_archive_filename_part(season_name):
    m = re.match(
        r"(\d{4})年\s*(春シーズン|夏シーズン|秋シーズン|冬シーズン)",
        season_name,
    )
    if not m:
        return re.sub(
            r"[^0-9A-Za-z_\-]",
            "_",
            season_name,
        )

    mp = {
        "春シーズン": "spring",
        "夏シーズン": "summer",
        "秋シーズン": "autumn",
        "冬シーズン": "winter",
    }

    return f"{m.group(1)}_{mp[m.group(2)]}"


def chunk_records(records, size):
    for i in range(0, len(records), size):
        yield records[i:i + size]


def record_to_archive_obj(record):
    return {
        "type": "xp_record",
        "user_id": int(record["user_id"]),
        "user_name": str(record["user_name"]),
        "xp": int(record["xp"]),
        "time": record["time"].astimezone(JST).isoformat(),
        "season": str(record["season"]),
        "message_id": int(record.get("message_id", 0)),
    }


async def auto_archive_if_needed(force=False):
    global LAST_ARCHIVE_CHECK

    async with ARCHIVE_LOCK:
        await load_local_database()

        now = datetime.now(JST)
        now_mono = time.monotonic()

        if (
            not force
            and now_mono - LAST_ARCHIVE_CHECK
            < ARCHIVE_CHECK_INTERVAL_SECONDS
        ):
            return False, 0, "次回チェック待ちです"

        LAST_ARCHIVE_CHECK = now_mono

        records = [
            r for r in _cache_all_log_records()
            if not r.get("archived", False)
        ]

        if not records:
            return False, 0, "アーカイブ対象がありません"

        if not force and len(records) < ARCHIVE_THRESHOLD:
            return False, len(records), "アーカイブ条件未達です"

        candidates = records if force else [
            r for r in records
            if is_archive_eligible_season(
                r.get("season", ""),
                now,
            )
        ]

        if not candidates:
            return False, len(records), (
                "アーカイブ可能な過去シーズンがありません"
            )

        grouped = {}
        for rec in candidates:
            grouped.setdefault(
                rec.get("season", "不明シーズン"),
                [],
            ).append(rec)

        archive_channel = client.get_channel(
            ARCHIVE_CHANNEL_ID
        )
        log_channel = client.get_channel(LOG_CHANNEL_ID)

        archived_total = 0
        failed_delete = 0
        warnings = []

        for season_name, recs in grouped.items():
            recs.sort(key=lambda x: x["time"])

            chunks = list(
                chunk_records(
                    [
                        record_to_archive_obj(r)
                        for r in recs
                    ],
                    ARCHIVE_MAX_RECORDS_PER_FILE,
                )
            )

            if len(chunks) > ARCHIVE_MAX_PARTS_PER_SEASON:
                warnings.append(
                    f"{season_name}: "
                    f"{len(recs)}件のため最大"
                    f"{ARCHIVE_MAX_RECORDS_PER_FILE * ARCHIVE_MAX_PARTS_PER_SEASON}"
                    "件まで保存"
                )
                chunks = chunks[
                    :ARCHIVE_MAX_PARTS_PER_SEASON
                ]

            safe_name = safe_archive_filename_part(
                season_name
            )

            season_archived = []

            for part_index, chunk in enumerate(
                chunks,
                start=1,
            ):
                archive_obj = {
                    "type": "xp_archive",
                    "archive_unit": "season",
                    "season": season_name,
                    "part": part_index,
                    "created_at": now.isoformat(),
                    "record_count": len(chunk),
                    "records": chunk,
                }

                raw = json.dumps(
                    archive_obj,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")

                fname = (
                    f"xp_archive_{safe_name}_"
                    f"part{part_index}.json"
                )

                local_path = os.path.join(
                    ARCHIVE_DIR,
                    fname,
                )

                await asyncio.to_thread(
                    write_bytes_sync,
                    local_path,
                    raw,
                )

                uploaded = False
                if archive_channel:
                    try:
                        await safe_send(
                            archive_channel,
                            content=(
                                "📦 **XPログ シーズン別アーカイブ**\n"
                                f"シーズン：**{season_name}**\n"
                                f"Part：**{part_index}/{len(chunks)}**\n"
                                f"件数：**{len(chunk)}件**\n"
                                f"作成日時：**{now.strftime('%Y/%m/%d %H:%M:%S')}**"
                            ),
                            file=discord.File(
                                io.BytesIO(raw),
                                filename=fname,
                            ),
                        )
                        uploaded = True
                    except Exception as e:
                        print(f"Archive upload error: {e}")
                else:
                    print("Archive channel not found; Discord archive upload skipped")

                if ARCHIVE_REQUIRE_DISCORD_UPLOAD and not uploaded:
                    warnings.append(
                        f"{season_name} Part {part_index}: Discordアーカイブ送信失敗のため未確定"
                    )
                    continue

                # Only records actually represented in chunks
                # become archived.
                chunk_ids = {
                    int(obj.get("message_id", 0))
                    for obj in chunk
                }

                for rec in recs:
                    if (
                        int(rec.get("message_id", 0))
                        in chunk_ids
                    ):
                        season_archived.append(rec)

            for rec in season_archived:
                rec["archived"] = True

                # IMPORTANT: keep the original Discord XP log after archiving.
                # It is the disaster-recovery backup if Render's local files
                # are lost. The /アーカイブ済みログ掃除 command remains available
                # for an explicit administrator cleanup later.
                archived_total += 1

        await save_local_database()

        msg = "アーカイブ完了"
        if failed_delete:
            msg += (
                f"。Discordログの削除失敗："
                f"{failed_delete}件"
            )
        if warnings:
            msg += " / " + " / ".join(warnings)

        return True, archived_total, msg


def write_bytes_sync(path, data):
    with open(path, "wb") as f:
        f.write(data)


# ============================================================
# Bot
# ============================================================

class XPClient(discord.Client):
    def __init__(self, *, intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Synchronizing global commands every boot can create unnecessary
        # API traffic. Only sync when explicitly requested.
        if (
            os.environ.get("SYNC_COMMANDS") == "1"
            or os.environ.get("SYNC_COMMADS") == "1"
        ):
            await self.tree.sync()
            print(
                "スラッシュコマンドを同期しました。"
                "次回以降はSYNC_COMMANDS=0/未設定にしてください。"
            )
        else:
            print(
                "コマンド同期はスキップ。"
                "コマンド変更時だけSYNC_COMMANDS=1で1回起動してください。"
            )


intents = discord.Intents.default()
intents.message_content = True
client = XPClient(intents=intents)


# ============================================================
# Cooldown definitions
# ============================================================

def user_cooldown(rate, per):
    return app_commands.checks.cooldown(
        rate,
        per,
        key=lambda interaction: interaction.user.id,
    )


# ============================================================
# Utility / settings commands
# ============================================================

@client.event
async def on_ready():
    global BOT_READY, READY_AT_MONO

    print(f"{client.user} が起動しました。")
    print(
        "過去のDiscordログは読み込みません。"
        "ローカルJSONをデータソースとして使用します。"
    )

    # Do not perform history(), schedule fetches, graph generation,
    # or other heavy Discord API operations here.
    await load_local_database()

    READY_AT_MONO = time.monotonic()
    BOT_READY = True


@client.tree.command(
    name="通知設定",
    description="煽り文章・次のガチエリア表示をON/OFFします",
)
@app_commands.describe(
    煽り文章="XP保存後の煽り文章を表示するか",
    エリア通知="XP保存後に次のガチエリア時間とステージを表示するか",
)
@user_cooldown(1, 10)
async def notification_settings(
    interaction: discord.Interaction,
    煽り文章: bool = None,
    エリア通知: bool = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    await load_local_database()

    async with DATA_LOCK:
        if 煽り文章 is not None:
            BOT_SETTINGS["drama_enabled"] = bool(煽り文章)
        if エリア通知 is not None:
            BOT_SETTINGS["area_notice_enabled"] = bool(エリア通知)
        await save_local_database()

    await interaction.followup.send(
        "⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS['drama_enabled'] else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS['area_notice_enabled'] else 'OFF'}**"
    )


@client.tree.command(
    name="設定確認",
    description="現在のBot設定を確認します",
)
@user_cooldown(2, 10)
async def show_settings(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    await load_local_database()

    await interaction.followup.send(
        "⚙️ **現在の通知設定**\n"
        f"煽り文章：**{'ON' if BOT_SETTINGS['drama_enabled'] else 'OFF'}**\n"
        f"次のガチエリア表示：**{'ON' if BOT_SETTINGS['area_notice_enabled'] else 'OFF'}**"
    )


@client.tree.command(
    name="ログ件数",
    description="現在のローカルXPデータ件数を確認します",
)
@user_cooldown(2, 10)
async def log_count(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    await load_local_database()

    current, archived = _count_cache_records()

    await interaction.followup.send(
        "📊 **XPログ件数**\n"
        f"未アーカイブ：**{current}件**\n"
        f"アーカイブ済み：**{archived}件**\n"
        f"自動アーカイブしきい値：**{ARCHIVE_THRESHOLD}件**\n"
        f"保存先：`{XP_DATA_FILE}`"
    )


@client.tree.command(
    name="再読み込み",
    description="【管理者専用】ローカルJSONを再読み込みします",
)
@user_cooldown(1, 20)
async def reload_cache(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    global DATA_READY

    DATA_READY = False
    await load_local_database()

    current, archived = _count_cache_records()

    await interaction.followup.send(
        f"🔄 ローカルJSONを再読み込みしました。\n"
        f"未アーカイブ **{current}件** / "
        f"アーカイブ済み **{archived}件**"
    )


@client.tree.command(
    name="全再読み込み",
    description="【管理者専用】ローカルキャッシュを完全再構築します",
)
@user_cooldown(1, 30)
async def reload_all_cache(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    global DATA_READY

    DATA_READY = False
    CACHE_BY_USER.clear()
    CACHE_BY_SOURCE.clear()
    CACHE_GOALS.clear()
    await load_local_database()

    await interaction.followup.send(
        "🔄 ローカルJSONからキャッシュを完全再構築しました。"
    )


# ============================================================
# Goal commands
# ============================================================

@client.tree.command(
    name="目標設定",
    description="現シーズンの目標XPを設定します",
)
@app_commands.describe(目標xp="目標にするXP。例: 2800")
@user_cooldown(1, 10)
async def set_goal(
    interaction: discord.Interaction,
    目標xp: int,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if not (500 <= 目標xp < 5000):
        await interaction.followup.send(
            "⚠️ 目標XPは500〜5000で入力してください！"
        )
        return

    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)

    async with DATA_LOCK:
        old_goal = CACHE_GOALS.get(
            interaction.user.id,
            {},
        ).get(sname)

        if old_goal:
            old_goal["active"] = False

        goal = {
            "user_id": interaction.user.id,
            "user_name": interaction.user.display_name,
            "target_xp": int(目標xp),
            "season": sname,
            "created_at": now.strftime(
                "%Y/%m/%d %H:%M"
            ),
            "active": True,
        }

        _cache_set_goal(goal)
        await save_local_database()

    current_xp = None
    recs = CACHE_BY_USER.get(
        interaction.user.id,
        {},
    ).get("records", [])

    recs = [
        r for r in recs
        if is_record_in_period(
            r["time"],
            str(sy),
            st,
            None,
        )
    ]

    if recs:
        recs.sort(key=lambda x: x["time"])
        current_xp = recs[-1]["xp"]

    if current_xp is None:
        await interaction.followup.send(
            f"🎯 **{sname}の目標を設定しました！**\n"
            f"目標：**{目標xp} XP**\n"
            "まだ今シーズンの記録がありません。"
        )
    else:
        diff = 目標xp - current_xp
        await interaction.followup.send(
            f"🎯 **{sname}の目標を設定しました！**\n"
            f"目標：**{目標xp} XP**\n"
            f"現在：**{current_xp} XP**\n"
            + (
                "✅ もう目標達成済みです。"
                if diff <= 0
                else f"あと **{diff} XP**！"
            )
        )


@client.tree.command(
    name="目標確認",
    description="現シーズンの目標XPと達成状況を確認します",
)
@user_cooldown(2, 10)
async def check_goal(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)

    goal = await get_active_goal(
        interaction.user.id,
        sname,
    )

    if not goal:
        await interaction.followup.send(
            f"⚠️ **{sname}**の目標が設定されていません。"
        )
        return

    recs = [
        r for r in CACHE_BY_USER.get(
            interaction.user.id,
            {},
        ).get("records", [])
        if is_record_in_period(
            r["time"],
            str(sy),
            st,
            None,
        )
    ]

    if not recs:
        await interaction.followup.send(
            f"🎯 **{sname}の目標**\n"
            f"目標：**{goal['target_xp']} XP**\n"
            "現在：記録なし"
        )
        return

    recs.sort(key=lambda x: x["time"])
    current_xp = recs[-1]["xp"]
    best_xp = max(r["xp"] for r in recs)
    diff = goal["target_xp"] - current_xp

    await interaction.followup.send(
        f"🎯 **{sname}の目標**\n"
        f"目標：**{goal['target_xp']} XP**\n"
        f"現在：**{current_xp} XP**\n"
        f"今シーズン最高：**{best_xp} XP**\n"
        + (
            "✅ **目標達成済み！**"
            if diff <= 0
            else f"あと **{diff} XP**！"
        )
    )


@client.tree.command(
    name="目標削除",
    description="現シーズンの目標XPを削除します",
)
@app_commands.describe(確認="削除する場合は DELETE と入力")
@user_cooldown(1, 10)
async def delete_goal(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if 確認 != "DELETE":
        await interaction.followup.send(
            "⚠️ `DELETE` と入力してください。"
        )
        return

    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)
    sname = season_full(sy, st)

    async with DATA_LOCK:
        goal = CACHE_GOALS.get(
            interaction.user.id,
            {},
        ).get(sname)

        if not goal:
            await interaction.followup.send(
                f"⚠️ **{sname}**の有効な目標がありません。"
            )
            return

        _cache_remove_goal(
            interaction.user.id,
            sname,
        )
        await save_local_database()

    await interaction.followup.send(
        f"🗑️ **{sname}**の目標を削除しました。"
    )


# ============================================================
# Statistics commands
# ============================================================

@client.tree.command(
    name="自己ベスト",
    description="自分の最高XPを表示します",
)
@app_commands.describe(
    期間="例：「夏シーズン」「5月」「全期間」「全記録」など"
)
@user_cooldown(2, 10)
async def personal_best(
    interaction: discord.Interaction,
    期間: str = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)

    ty, ts, tm, ia, title, cont = parse_args_from_str(
        期間,
        sy,
        st,
        True,
    )

    data = build_data_from_records(
        current_cache_records_as_parsed()
    )

    if interaction.user.id not in data:
        await interaction.followup.send(
            "⚠️ データがありません。"
        )
        return

    recs = data[interaction.user.id]["records"]

    if not ia and not cont:
        recs = [
            r for r in recs
            if is_record_in_period(
                r["time"],
                ty,
                ts,
                tm,
            )
        ]

    if not recs:
        await interaction.followup.send(
            f"⚠️ {title}のデータがありません。"
        )
        return

    best = max(recs, key=lambda r: r["xp"])

    await interaction.followup.send(
        f"🏅 **{interaction.user.display_name}さんの自己ベスト**\n"
        f"期間：**{title}**\n"
        f"最高XP：**{best['xp']} XP**\n"
        f"記録枠：**{best['time'].strftime('%Y/%m/%d %H:%M')}**\n"
        f"シーズン：**{best['season']}**"
    )


@client.tree.command(
    name="伸びランキング",
    description="指定期間でXPが伸びた人ランキングを表示します",
)
@app_commands.describe(
    期間="例：「夏シーズン」「5月」「全期間」「全記録」など"
)
@user_cooldown(1, 15)
async def growth_ranking(
    interaction: discord.Interaction,
    期間: str = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()

    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)

    ty, ts, tm, ia, title, cont = parse_args_from_str(
        期間,
        sy,
        st,
        True,
    )

    data = build_data_from_records(
        current_cache_records_as_parsed()
    )

    growth_list = []

    for uid, info in data.items():
        recs = info["records"]

        if not ia and not cont:
            recs = [
                r for r in recs
                if is_record_in_period(
                    r["time"],
                    ty,
                    ts,
                    tm,
                )
            ]

        if len(recs) >= 2:
            growth_list.append(
                (
                    info["name"],
                    recs[-1]["xp"] - recs[0]["xp"],
                    recs[0]["xp"],
                    recs[-1]["xp"],
                    len(recs),
                )
            )
        elif len(recs) == 1:
            growth_list.append(
                (
                    info["name"],
                    0,
                    recs[0]["xp"],
                    recs[0]["xp"],
                    1,
                )
            )

    if not growth_list:
        await interaction.followup.send(
            f"⚠️ {title}のデータがありません。"
        )
        return

    growth_list.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    result = (
        f"🔥 **{title} 伸びランキング** 🔥\n\n"
    )

    for i, (
        name,
        growth,
        start_xp,
        last_xp,
        count,
    ) in enumerate(growth_list[:10]):
        medal = (
            ["🥇", "🥈", "🥉"][i]
            if i < 3
            else f"**{i + 1}位**"
        )
        sign = "+" if growth > 0 else ""
        result += (
            f"{medal}：{name} "
            f"({sign}{growth} XP / "
            f"{start_xp}→{last_xp} / "
            f"{count}件)\n"
        )

    await interaction.followup.send(result)


# ============================================================
# Area schedule command
# ============================================================

@client.tree.command(
    name="ガチエリア",
    description="次回のガチエリア時間とステージを表示します",
)
@user_cooldown(1, 30)
async def area_schedule_command(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()
    now = datetime.now(JST)

    # fetch_area_schedule() is cached for 15 minutes and never runs on startup.
    ok = await fetch_area_schedule()
    next_area = await get_next_area_shift(now) if ok else None

    if not next_area:
        if CACHED_AREA_DETAILS:
            await interaction.followup.send(
                "⚠️ スケジュールAPIを更新できなかったため、現在保持しているキャッシュにも次回エリアがありません。"
            )
        else:
            await interaction.followup.send(
                "⚠️ ガチエリアのスケジュールを取得できませんでした。API側の一時的な制限・障害の可能性があります。"
            )
        return

    ns = next_area["start"]
    ne = next_area["end"]
    stage_text = " / ".join(next_area.get("stages", [])) or "ステージ情報なし"

    await interaction.followup.send(
        "🗓️ **次のガチエリア**\n"
        f"**{ns.strftime('%Y/%m/%d %H:%M')} - {ne.strftime('%H:%M')}**\n"
        f"🗺️ ステージ：**{stage_text}**\n"
        f"💾 APIキャッシュ：**{SCHEDULE_CACHE_SECONDS // 60}分**"
    )


# ============================================================
# Graph commands
# ============================================================

@client.tree.command(
    name="グラフ",
    description="自分の成長グラフを生成します",
)
@app_commands.describe(
    期間="例：「5月」「夏シーズン」「全期間」「全記録」など"
)
@user_cooldown(1, 20)
async def graph(
    interaction: discord.Interaction,
    期間: str = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()

    # IMPORTANT: graph generation only reads local memory.
    # No channel.history(), fetch_message(), or Discord log reads.
    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)

    ty, ts, tm, ia, title, cont = parse_args_from_str(
        期間,
        sy,
        st,
        True,
    )

    data = build_data_from_records(
        current_cache_records_as_parsed()
    )

    if interaction.user.id not in data:
        await interaction.followup.send(
            "⚠️ データがありません。"
        )
        return

    recs = data[interaction.user.id]["records"]

    if not ia and not cont:
        recs = [
            r for r in recs
            if is_record_in_period(
                r["time"],
                ty,
                ts,
                tm,
            )
        ]

    if not recs:
        await interaction.followup.send(
            f"⚠️ {title}のデータがありません。"
        )
        return

    # Keep every point for personal graph, matching the old behavior.
    fig, ax = plt.subplots(figsize=(12, 6))

    indices = list(range(len(recs)))
    xps = [r["xp"] for r in recs]

    ax.plot(
        indices,
        xps,
        marker="o",
        linewidth=1.5,
        markersize=5,
    )

    set_limited_xticks(
        ax,
        indices,
        [
            r["time"].strftime("%m/%d %H:%M")
            for r in recs
        ],
        force_all=True,
    )

    ax.axhline(
        max(xps),
        linestyle="--",
        alpha=0.4,
    )

    ax.set_title(
        f"{interaction.user.display_name}さんの成長記録 ({title})",
        fontsize=15,
    )
    ax.set_ylabel("XP")
    ax.grid(
        True,
        linestyle="--",
        alpha=0.6,
    )
    plt.tight_layout()

    output = io.BytesIO()
    try:
        plt.savefig(
            output,
            format="png",
            dpi=100,
            bbox_inches="tight",
        )
        plt.close(fig)
        output.seek(0)

        await interaction.followup.send(
            file=discord.File(
                output,
                filename="xp_graph.png",
            )
        )
    except Exception:
        plt.close(fig)
        raise
    finally:
        output.close()


@client.tree.command(
    name="比較グラフ",
    description="メンバー全員、または指定した人を重ねて比較します",
)
@app_commands.describe(
    相手1="比較したい相手1",
    相手2="比較したい相手2",
    相手3="比較したい相手3",
    期間="例：「5月」「夏シーズン」「全期間」「全記録」など",
)
@user_cooldown(1, 30)
async def comp_graph(
    interaction: discord.Interaction,
    相手1: discord.Member = None,
    相手2: discord.Member = None,
    相手3: discord.Member = None,
    期間: str = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()

    # Local-only read. No Discord history.
    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)

    ty, ts, tm, ia, title, cont = parse_args_from_str(
        期間,
        sy,
        st,
        True,
    )

    all_d = build_data_from_records(
        current_cache_records_as_parsed()
    )

    targets = [interaction.user.id]

    for member in (相手1, 相手2, 相手3):
        if member:
            targets.append(member.id)

    targets = list(dict.fromkeys(targets))

    is_all_compare = len(targets) == 1
    target_ids = (
        list(all_d.keys())
        if is_all_compare
        else targets
    )

    plot_data = []

    for uid in target_ids:
        if uid not in all_d:
            continue

        recs = all_d[uid]["records"]

        if not ia and not cont:
            recs = [
                r for r in recs
                if is_record_in_period(
                    r["time"],
                    ty,
                    ts,
                    tm,
                )
            ]

        if recs:
            plot_data.append(
                (all_d[uid]["name"], recs)
            )

    omitted_msg = ""

    if (
        is_all_compare
        and len(plot_data) > COMPARE_ALL_MAX_USERS
    ):
        plot_data.sort(
            key=lambda x: x[1][-1]["xp"],
            reverse=True,
        )
        omitted = (
            len(plot_data)
            - COMPARE_ALL_MAX_USERS
        )
        plot_data = plot_data[
            :COMPARE_ALL_MAX_USERS
        ]
        omitted_msg = (
            f"\n⚠️ 全員比較対象が多いため、"
            f"最新XP上位{COMPARE_ALL_MAX_USERS}人のみ表示"
            f"（省略 {omitted}人）。"
        )

    if not plot_data:
        await interaction.followup.send(
            "⚠️ 比較するデータがありません。"
        )
        return

    plot_data = [
        (
            name,
            thin_records_for_plot(recs),
        )
        for name, recs in plot_data
    ]

    fig, ax = plt.subplots(figsize=(12, 6))

    max_len = max(
        len(recs)
        for _, recs in plot_data
    )

    label_recs = max(
        plot_data,
        key=lambda x: len(x[1]),
    )[1]

    for name, recs in plot_data:
        ax.plot(
            list(range(len(recs))),
            [r["xp"] for r in recs],
            marker="o",
            linewidth=1.5,
            markersize=4,
            label=name,
        )

    set_limited_xticks(
        ax,
        list(range(max_len)),
        [
            r["time"].strftime("%m/%d %H:%M")
            for r in label_recs
        ],
        force_all=(len(plot_data) <= 3),
    )

    graph_title = (
        "みんなのXP比較グラフ"
        if is_all_compare
        else "指定メンバーのXP比較グラフ"
    )

    ax.set_title(
        f"{graph_title} ({title})",
        fontsize=15,
    )
    ax.set_ylabel("XP")
    ax.grid(
        True,
        linestyle="--",
        alpha=0.6,
    )
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1, 1),
        fontsize=8,
    )

    plt.tight_layout()

    output = io.BytesIO()

    try:
        plt.savefig(
            output,
            format="png",
            dpi=100,
            bbox_inches="tight",
        )
        plt.close(fig)
        output.seek(0)

        await interaction.followup.send(
            content=omitted_msg or None,
            file=discord.File(
                output,
                filename="xp_compare.png",
            ),
        )
    finally:
        plt.close(fig)
        output.close()


# ============================================================
# Ranking / award
# ============================================================

@client.tree.command(
    name="ランキング",
    description="XPランキングを表示します",
)
@app_commands.describe(
    期間="例：「5月」「夏シーズン」「全期間」「全記録」など"
)
@user_cooldown(1, 15)
async def ranking(
    interaction: discord.Interaction,
    期間: str = None,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()

    await load_local_database()

    now = datetime.now(JST)
    sy, st = get_current_season(now)

    ty, ts, tm, ia, title, cont = parse_args_from_str(
        期間,
        sy,
        st,
        True,
    )

    data = build_data_from_records(
        current_cache_records_as_parsed()
    )

    ranking_list = []

    for uid, info in data.items():
        recs = info["records"]

        if not ia and not cont:
            recs = [
                r for r in recs
                if is_record_in_period(
                    r["time"],
                    ty,
                    ts,
                    tm,
                )
            ]

        if recs:
            ranking_list.append(
                (info["name"], recs[-1]["xp"])
            )

    if not ranking_list:
        await interaction.followup.send(
            f"⚠️ {title}のデータがありません。"
        )
        return

    ranking_list.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    result = f"🏆 **{title} ランキング** 🏆\n\n"

    for i, (name, xp) in enumerate(
        ranking_list[:10]
    ):
        medal = (
            ["🥇", "🥈", "🥉"][i]
            if i < 3
            else f"**{i + 1}位**"
        )
        result += f"{medal}：{name} ({xp} XP)\n"

    await interaction.followup.send(result)


@client.tree.command(
    name="表彰式",
    description="シーズン終了直後の表彰式を行います",
)
@user_cooldown(1, 30)
async def award(interaction: discord.Interaction):
    if not await require_ready(interaction):
        return

    await interaction.response.defer()

    now = datetime.now(JST)

    if now.month not in [3, 6, 9, 12]:
        await interaction.followup.send(
            "⚠️ 表彰式はシーズン終了直後の1週間限定です！"
        )
        return

    change_time = datetime(
        now.year,
        now.month,
        1,
        9,
        0,
        tzinfo=JST,
    )

    if not (
        change_time
        <= now
        < change_time + timedelta(days=7)
    ):
        await interaction.followup.send(
            "⚠️ 表彰式はシーズン終了直後の1週間限定です！"
        )
        return

    target_year, target_season = (
        get_previous_season_for_award(now)
    )

    data = build_data_from_records(
        current_cache_records_as_parsed()
    )

    most_played = []
    last_spurt = []

    for uid, info in data.items():
        recs = [
            r for r in info["records"]
            if is_record_in_period(
                r["time"],
                str(target_year),
                target_season,
                None,
            )
        ]

        if not recs:
            continue

        most_played.append(
            (info["name"], len(recs))
        )

        if len(recs) >= 2:
            base = recs[0]

            for r in reversed(recs):
                if (
                    r["time"]
                    <= recs[-1]["time"]
                    - timedelta(days=7)
                ):
                    base = r
                    break

            last_spurt.append(
                (
                    info["name"],
                    recs[-1]["xp"] - base["xp"],
                )
            )
        else:
            last_spurt.append(
                (info["name"], 0)
            )

    if not most_played:
        await interaction.followup.send(
            "⚠️ 表彰データがありません。"
        )
        return

    most_played.sort(
        key=lambda x: x[1],
        reverse=True,
    )
    last_spurt.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    await interaction.followup.send(
        f"🎉 **{target_year}年 {target_season} 表彰式** 🎉\n\n"
        f"🦑 **一番潜ったで賞**："
        f"{most_played[0][0]}さん "
        f"({most_played[0][1]}回)\n"
        f"🔥 **ラストスパート賞**："
        f"{last_spurt[0][0]}さん "
        f"(+{last_spurt[0][1]} XP)"
    )


# ============================================================
# Admin archive commands
# ============================================================

@client.tree.command(
    name="手動アーカイブ",
    description="【管理者専用】現在ログをシーズン別にアーカイブします",
)
@app_commands.describe(
    確認="実行する場合は ARCHIVE と入力"
)
@user_cooldown(1, 60)
async def manual_archive(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    if 確認 != "ARCHIVE":
        await interaction.followup.send(
            "⚠️ `ARCHIVE` と入力してください。"
        )
        return

    success, count, msg = (
        await auto_archive_if_needed(force=True)
    )

    await interaction.followup.send(
        (
            f"📦 手動アーカイブ完了：**{count}件**\n"
            f"{msg}"
        )
        if success
        else
        f"⚠️ 手動アーカイブ未実行：{msg}"
    )


@client.tree.command(
    name="アーカイブ済みログ掃除",
    description="【管理者専用】Discordに残った旧XPログを削除します",
)
@app_commands.describe(
    確認="実行する場合は CLEAN と入力"
)
@user_cooldown(1, 60)
async def cleanup_archived_logs(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    if 確認 != "CLEAN":
        await interaction.followup.send(
            "⚠️ `CLEAN` と入力してください。"
        )
        return

    # We deliberately do not use channel.history().
    # Cleanup can only remove log messages whose IDs are already
    # present in the local database.
    await load_local_database()

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    if not log_channel:
        await interaction.followup.send(
            "⚠️ ログチャンネルが見つかりません。"
        )
        return

    checked = 0
    deleted = 0

    all_with_logs = []
    for info in CACHE_BY_USER.values():
        all_with_logs.extend(
            r for r in info.get("records", [])
            if r.get("_log_msg_id")
        )

    for rec in list(all_with_logs):
        checked += 1

        if not rec.get("archived"):
            continue

        if rec.get("_log_msg_id"):
            if await safe_delete_message(
                log_channel,
                rec["_log_msg_id"],
            ):
                deleted += 1

            await asyncio.sleep(
                DELETE_SLEEP_SECONDS
            )

            rec["_log_msg_id"] = None

    await save_local_database()

    await interaction.followup.send(
        "🧹 **アーカイブ済みログ掃除完了**\n"
        f"ローカルデータ確認：**{checked}件**\n"
        f"削除処理：**{deleted}件**"
    )


# ============================================================
# Disaster recovery
# ============================================================

def parse_xp_log_message(content, log_message_id):
    try:
        obj = json.loads(content)
    except Exception:
        return None

    if obj.get("type") != "xp_record":
        return None

    try:
        dt = datetime.strptime(str(obj["time"]), "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        return {
            "user_id": int(obj["user_id"]),
            "user_name": str(obj.get("user_name", f"ID:{obj['user_id']}")),
            "xp": int(obj["xp"]),
            "time": dt,
            "season": str(obj["season"]),
            "message_id": int(obj.get("message_id", 0)),
            "_log_msg_id": int(log_message_id),
            "_source": "discord_restore",
            "archived": False,
        }
    except Exception:
        return None


@client.tree.command(
    name="データ復旧",
    description="【管理者専用】Discord XPログからローカルDBを復旧します",
)
@app_commands.describe(確認="実行する場合は RESTORE と入力")
@user_cooldown(1, 300)
async def restore_from_discord_logs(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    if not is_admin(interaction.user):
        await interaction.followup.send("⚠️ このコマンドは管理者専用です。")
        return

    if 確認 != "RESTORE":
        await interaction.followup.send(
            "⚠️ ローカルDBが失われた場合だけ使用してください。実行するには `RESTORE` と入力します。"
        )
        return

    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        await interaction.followup.send("⚠️ XPログチャンネルが見つかりません。")
        return

    await load_local_database()

    restored = 0
    skipped = 0
    scanned = 0

    # This is intentionally the ONLY normal path that reads channel.history().
    # It is explicit, administrator-only, heavily rate-limited, and never runs
    # during startup or graph generation.
    async for msg in log_channel.history(
        limit=RESTORE_MAX_MESSAGES,
        oldest_first=False,
    ):
        scanned += 1
        rec = parse_xp_log_message(msg.content, msg.id)
        if not rec:
            continue

        existing = CACHE_BY_SOURCE.get(rec["message_id"]) if rec.get("message_id") else None
        if existing:
            existing["_log_msg_id"] = msg.id
            if not existing.get("archived"):
                existing["archived"] = False
            skipped += 1
            continue

        _cache_add_single(rec)
        restored += 1

    await save_local_database()

    await interaction.followup.send(
        "🛟 **Discordログからのデータ復旧が完了しました。**\n"
        f"走査：**{scanned}件**\n"
        f"新規復旧：**{restored}件**\n"
        f"既存スキップ：**{skipped}件**\n"
        f"上限：**{RESTORE_MAX_MESSAGES}件**\n\n"
        "※この処理は通常起動・グラフ生成では実行されません。"
    )


# ============================================================
# Reset / delete commands
# ============================================================

@client.tree.command(
    name="リセット",
    description="自分の直近1件のXP記録を取り消します",
)
@user_cooldown(1, 10)
async def reset_last(
    interaction: discord.Interaction,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    await load_local_database()

    rec = _cache_find_latest_log_record(
        interaction.user.id
    )

    if not rec:
        await interaction.followup.send(
            "⚠️ 削除対象がありません。"
        )
        return

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    if log_channel and rec.get("_log_msg_id"):
        await safe_delete_message(
            log_channel,
            rec["_log_msg_id"],
        )

    _cache_remove_record(rec)
    await save_local_database()

    await interaction.followup.send(
        "🗑️ 直近の未アーカイブ記録を1件リセットしました！"
    )


@client.tree.command(
    name="マイデータ全削除",
    description="自分の未アーカイブデータを消去します",
)
@app_commands.describe(
    確認="本当に削除する場合は DELETE と入力"
)
@user_cooldown(1, 20)
async def delete_my_data(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    if 確認 != "DELETE":
        await interaction.followup.send(
            "⚠️ `DELETE` と入力してください。"
        )
        return

    await load_local_database()

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    records = list(
        _cache_user_log_records(
            interaction.user.id
        )
    )

    deleted_count = 0

    for rec in records:
        if log_channel and rec.get("_log_msg_id"):
            await safe_delete_message(
                log_channel,
                rec["_log_msg_id"],
            )
            await asyncio.sleep(
                DELETE_SLEEP_SECONDS
            )

        _cache_remove_record(rec)
        deleted_count += 1

    await save_local_database()

    await interaction.followup.send(
        f"✅ あなたの未アーカイブデータ "
        f"**{deleted_count}件**を消去しました！\n"
        "※ローカルアーカイブ済みデータも含めて削除対象にしたい場合は、"
        "手動でJSONをバックアップしてから管理してください。"
    )


@client.tree.command(
    name="メンバーデータ削除",
    description="【管理者専用】指定メンバーの未アーカイブデータを削除します",
)
@app_commands.describe(
    対象="データを削除するメンバー",
    確認="本当に削除する場合は RESET と入力",
)
@user_cooldown(1, 20)
async def delete_member_data(
    interaction: discord.Interaction,
    対象: discord.Member,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    if 確認 != "RESET":
        await interaction.followup.send(
            "⚠️ `RESET` と入力してください。"
        )
        return

    await load_local_database()

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    records = list(
        _cache_user_log_records(
            対象.id
        )
    )

    deleted_count = 0

    for rec in records:
        if log_channel and rec.get("_log_msg_id"):
            await safe_delete_message(
                log_channel,
                rec["_log_msg_id"],
            )
            await asyncio.sleep(
                DELETE_SLEEP_SECONDS
            )

        _cache_remove_record(rec)
        deleted_count += 1

    await save_local_database()

    await interaction.followup.send(
        f"🚨 管理者権限："
        f"{対象.display_name}さんの未アーカイブデータ "
        f"**{deleted_count}件**を削除しました。"
    )


@client.tree.command(
    name="全員のデータ強制リセット",
    description="【管理者専用】未アーカイブの全データを初期化します",
)
@app_commands.describe(
    確認="本当に全削除する場合は RESET と入力"
)
@user_cooldown(1, 30)
async def reset_all_data(
    interaction: discord.Interaction,
    確認: str,
):
    if not await require_ready(interaction):
        return

    await interaction.response.defer(
        ephemeral=True
    )

    if not is_admin(interaction.user):
        await interaction.followup.send(
            "⚠️ このコマンドは管理者専用です。"
        )
        return

    if 確認 != "RESET":
        await interaction.followup.send(
            "⚠️ `RESET` と入力してください。"
        )
        return

    await load_local_database()

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    records = list(_cache_all_log_records())
    deleted_count = 0

    for rec in records:
        if log_channel and rec.get("_log_msg_id"):
            await safe_delete_message(
                log_channel,
                rec["_log_msg_id"],
            )
            await asyncio.sleep(
                DELETE_SLEEP_SECONDS
            )

        _cache_remove_record(rec)
        deleted_count += 1

    await save_local_database()

    await interaction.followup.send(
        f"🚨 管理者権限：未アーカイブXPデータ "
        f"**{deleted_count}件**を初期化しました！"
    )


# ============================================================
# XP message handling
# ============================================================

def build_power_change_message(old_xp, new_xp):
    if old_xp is None:
        return random.choice(
            [
                "\n🆕 **初記録！** ここから伝説、始めちゃいますか！",
                "\n🦑 **初陣記録！** まずはここがスタートライン。",
                "\n📌 **初登録完了！** 成長の始まりです。",
            ]
        )

    diff = new_xp - old_xp

    if diff > 0:
        if diff >= 100:
            return random.choice(
                [
                    f"\n🚀 **爆伸び！** 前回から **+{diff} XP**！",
                    f"\n🔥 **大暴れ成功！** **+{diff} XP**！",
                ]
            )

        if diff >= 50:
            return random.choice(
                [
                    f"\n📈 **かなり良い伸び！** **+{diff} XP**！",
                    f"\n⚡ **ナイス上昇！** **+{diff} XP**！",
                ]
            )

        return random.choice(
            [
                f"\n✅ **微増ナイス！** **+{diff} XP**！",
                f"\n📊 **じわ伸び！** **+{diff} XP**。",
            ]
        )

    if diff == 0:
        return random.choice(
            [
                "\n🟰 **現状維持！**",
                "\n😐 **変動なし！**",
            ]
        )

    drop = abs(diff)

    if drop >= 150:
        return random.choice(
            [
                f"\n💥 **大事故発生！** **-{drop} XP**……",
                f"\n🫠 **溶けすぎ注意！** **-{drop} XP**。",
            ]
        )

    if drop >= 80:
        return random.choice(
            [
                f"\n😱 **けっこう痛い！** **-{drop} XP**。",
                f"\n🧯 **消火活動開始！** **-{drop} XP**。",
            ]
        )

    if drop >= 30:
        return random.choice(
            [
                f"\n😬 **ちょい痛い減少！** **-{drop} XP**。",
                f"\n📉 **少し後退！** **-{drop} XP**。",
            ]
        )

    return random.choice(
        [
            f"\n🤏 **微減！** **-{drop} XP**。",
            f"\n😌 **軽傷！** **-{drop} XP**。",
        ]
    )


@client.event
async def on_message(message):
    if (
        message.author == client.user
        or message.channel.id != TARGET_CHANNEL_ID
    ):
        return

    match = re.search(
        r"xp\s*([0-9]+)|([0-9]+)\s*xp",
        message.content,
        re.IGNORECASE,
    )

    if not match:
        return

    new_xp = int(
        match.group(1) or match.group(2)
    )

    if not (500 <= new_xp < 5000):
        await safe_send(
            message.channel,
            "⚠️ パワーは500〜5000で入力してください！",
        )
        return

    await load_local_database()

    now = datetime.now(JST)
    season_year, current_season_type = (
        get_current_season(now)
    )

    async with DATA_LOCK:
        user_records = CACHE_BY_USER.get(
            message.author.id,
            {},
        ).get("records", [])

        personal_best_xp = max(
            [
                r["xp"]
                for r in user_records
                if not r.get("archived", False)
            ],
            default=None,
        )

        current_season_xps = {}

        for uid, info in CACHE_BY_USER.items():
            recs = [
                r for r in info["records"]
                if is_record_in_period(
                    r["time"],
                    str(season_year),
                    current_season_type,
                    None,
                )
            ]

            if recs:
                recs.sort(
                    key=lambda x: x["time"]
                )
                current_season_xps[uid] = (
                    info["name"],
                    recs[-1]["xp"],
                )

        old_xp = current_season_xps.get(
            message.author.id,
            (
                message.author.display_name,
                None,
            ),
        )[1]

        if (
            old_xp is not None
            and abs(new_xp - old_xp) > 500
        ):
            await safe_send(
                message.channel,
                f"⚠️ 今シーズン前回記録({old_xp} XP)から"
                "±500以上の急激な増減があるため保存できません！",
            )
            return

    # Specified time avoids the external API completely.
    splat_time = parse_specified_time(
        message.content,
        now,
    )
    is_confident = True

    if not splat_time:
        splat_time = await update_and_get_last_area_time(
            now
        )

    if not splat_time:
        splat_time = get_last_splat_end_time(now)
        is_confident = False

    record_season_year, record_season_type = (
        get_record_season_for_shift_end(
            splat_time
        )
    )

    record_season_name = season_full(
        record_season_year,
        record_season_type,
    )

    log_channel = client.get_channel(
        LOG_CHANNEL_ID
    )

    # Discord log is only an audit copy. Local JSON is authoritative.
    log_message_id = None

    if log_channel:
        try:
            sent = await safe_send(
                log_channel,
                make_record_json(
                    message.author.id,
                    message.author.display_name,
                    new_xp,
                    splat_time,
                    record_season_name,
                    message.id,
                ),
            )
            log_message_id = sent.id
        except Exception as e:
            # Do NOT lose the XP record because the audit channel failed.
            print(f"XP audit log send error: {e}")

    async with DATA_LOCK:
        rec = _new_cache_rec(
            {
                "user_id": message.author.id,
                "user_name": message.author.display_name,
                "xp": new_xp,
                "time": splat_time,
                "season": record_season_name,
                "message_id": message.id,
            },
            source="local",
            log_msg_id=log_message_id,
        )

        _cache_add_single(rec)

        updated_xps = dict(current_season_xps)
        updated_xps[message.author.id] = (
            message.author.display_name,
            new_xp,
        )

        passed_users = []
        overtaken_users = []

        for uid, (name, xp) in current_season_xps.items():
            if uid == message.author.id:
                continue

            if (
                old_xp is not None
                and xp >= old_xp
                and new_xp > xp
            ) or (
                old_xp is None
                and new_xp > xp
            ):
                passed_users.append(name)

            if (
                old_xp is not None
                and xp < old_xp
                and new_xp < xp
            ):
                overtaken_users.append(name)

        sorted_ranking = sorted(
            updated_xps.items(),
            key=lambda x: x[1][1],
            reverse=True,
        )

        my_index = next(
            (
                i
                for i, (uid, _) in enumerate(
                    sorted_ranking
                )
                if uid == message.author.id
            ),
            0,
        )

        active_goal = CACHE_GOALS.get(
            message.author.id,
            {},
        ).get(record_season_name)

        if (
            active_goal
            and not active_goal.get("active", True)
        ):
            active_goal = None

        await save_local_database()

    goal_msg = ""

    if active_goal:
        target_xp = active_goal["target_xp"]

        if (
            new_xp >= target_xp
            and (
                old_xp is None
                or old_xp < target_xp
            )
        ):
            goal_msg += (
                f"\n🎯 **目標達成！** "
                f"目標 **{target_xp} XP** を突破！"
            )
        elif new_xp < target_xp:
            remain = target_xp - new_xp
            if remain <= 100:
                goal_msg += (
                    f"\n🎯 目標 **{target_xp} XP**まで"
                    f"あと **{remain} XP**！"
                )

    drama_msg = ""

    if BOT_SETTINGS.get("drama_enabled", True):
        drama_msg += build_power_change_message(
            old_xp,
            new_xp,
        )

        if (
            personal_best_xp is None
            or new_xp > personal_best_xp
        ):
            drama_msg += (
                f"\n🏅 **自己ベスト更新！** {new_xp} XP！"
            )

        if passed_users:
            drama_msg += (
                "\n⚔️ **【下剋上】** "
                + "、".join(passed_users)
                + "さんをブチ抜きました！"
            )
        elif overtaken_users:
            drama_msg += (
                "\n😱 **【悲報】** "
                + "、".join(overtaken_users)
                + "さんに抜かされてしまいました…"
            )

        if my_index == 0:
            drama_msg += "\n👑 **現在トップ独走中！**"

            if len(sorted_ranking) > 1:
                _, (
                    next_name,
                    next_xp,
                ) = sorted_ranking[1]

                drama_msg += (
                    f"（2位の{next_name}さんとは "
                    f"**XP {new_xp - next_xp}**差）"
                )
        else:
            _, (
                above_name,
                above_xp,
            ) = sorted_ranking[my_index - 1]

            drama_msg += (
                f"\n🎯 1つ上の{above_name}さんまで"
                f"あと **XP {above_xp - new_xp}**！"
            )

    start_time = splat_time - timedelta(hours=2)

    notice = (
        f"（記録枠："
        f"{start_time.strftime('%m/%d %H:%M')}-"
        f"{splat_time.strftime('%H:%M')}）"
    )

    if not is_confident:
        notice += (
            "\n💡 ※時間が違った場合は、チャットを編集して"
            "『17:00』のように終了時間を書き足してください！"
        )

    area_msg = ""

    if BOT_SETTINGS.get("area_notice_enabled", True):
        next_area = await get_next_area_shift(now)

        if next_area:
            ns = next_area["start"]
            ne = next_area["end"]

            stage_text = (
                " / ".join(next_area["stages"])
                if next_area["stages"]
                else "ステージ情報なし"
            )

            area_msg = (
                "\n\n🗓️ **次のガチエリア**\n"
                f"**{ns.strftime('%m/%d %H:%M')} - "
                f"{ne.strftime('%H:%M')}**\n"
                f"🗺️ ステージ：**{stage_text}**"
            )
        else:
            area_msg = (
                "\n\n🗓️ **次のガチエリア**\n"
                "現在、次回エリア情報を取得できませんでした。"
            )

    await safe_send(
        message.channel,
        f"✅ {new_xp} XP を保存しました！"
        f"{notice}{drama_msg}{goal_msg}{area_msg}",
    )

    success, archived_count, archive_msg = (
        await auto_archive_if_needed(force=False)
    )

    if success and archived_count:
        await safe_send(
            message.channel,
            f"📦 条件を満たした過去シーズンログ "
            f"**{archived_count}件**を"
            "シーズン別アーカイブしました！",
        )


# ============================================================
# Message delete/edit tracking
# ============================================================

@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return

    await load_local_database()

    async with DATA_LOCK:
        rec = CACHE_BY_SOURCE.get(
            payload.message_id
        )

        if not rec:
            return

        if rec.get("archived", False):
            # Archived data is immutable. Do not let deletion of the original
            # source message destroy historical season graphs.
            return

        log_channel = client.get_channel(
            LOG_CHANNEL_ID
        )

        if log_channel and rec.get("_log_msg_id"):
            await safe_delete_message(
                log_channel,
                rec["_log_msg_id"],
            )

        _cache_remove_record(rec)
        await save_local_database()


@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id != TARGET_CHANNEL_ID:
        return

    content = payload.data.get("content")
    if content is None:
        return

    await load_local_database()

    async with DATA_LOCK:
        rec = CACHE_BY_SOURCE.get(
            payload.message_id
        )

        if not rec:
            return

        if rec.get("archived", False):
            # Historical archive records are immutable.
            return

        match = re.search(
            r"xp\s*([0-9]+)|([0-9]+)\s*xp",
            content,
            re.IGNORECASE,
        )

        target_channel = client.get_channel(
            TARGET_CHANNEL_ID
        )

        if match:
            new_xp = int(
                match.group(1) or match.group(2)
            )

            if not (500 <= new_xp < 5000):
                if target_channel:
                    await safe_send(
                        target_channel,
                        "⚠️ 編集後のパワーも500〜5000で入力してください！",
                    )
                return

            rec["xp"] = new_xp

            spec_time = parse_specified_time(
                content,
                datetime.now(JST),
            )

            if spec_time:
                rec["time"] = spec_time

                sy, st = (
                    get_record_season_for_shift_end(
                        spec_time
                    )
                )

                rec["season"] = season_full(
                    sy,
                    st,
                )

                CACHE_BY_USER[
                    rec["user_id"]
                ]["records"].sort(
                    key=lambda x: x["time"]
                )

                if target_channel:
                    start_time = (
                        spec_time
                        - timedelta(hours=2)
                    )

                    await safe_send(
                        target_channel,
                        "🔄 記録枠を "
                        f"**{start_time.strftime('%H:%M')}"
                        "ー"
                        f"{spec_time.strftime('%H:%M')}**"
                        " に変更しました！",
                    )

            log_channel = client.get_channel(
                LOG_CHANNEL_ID
            )

            if (
                log_channel
                and rec.get("_log_msg_id")
            ):
                await safe_edit_message(
                    log_channel,
                    rec["_log_msg_id"],
                    make_record_json(
                        rec["user_id"],
                        rec["user_name"],
                        rec["xp"],
                        rec["time"],
                        rec["season"],
                        rec["message_id"],
                    ),
                )

            await save_local_database()

        else:
            log_channel = client.get_channel(
                LOG_CHANNEL_ID
            )

            if (
                log_channel
                and rec.get("_log_msg_id")
            ):
                await safe_delete_message(
                    log_channel,
                    rec["_log_msg_id"],
                )

            _cache_remove_record(rec)
            await save_local_database()


# ============================================================
# Global command error handling
# ============================================================

@client.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(
        error,
        app_commands.CommandOnCooldown,
    ):
        retry = max(
            0.1,
            float(error.retry_after),
        )

        message = (
            "⏳ コマンドを連続実行しすぎています。"
            f" **{retry:.1f}秒**後に再試行してください。"
        )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

        return

    print(
        "Slash command error:",
        repr(error),
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ コマンド実行中にエラーが発生しました。"
                "ログを確認してください。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ コマンド実行中にエラーが発生しました。"
                "ログを確認してください。",
                ephemeral=True,
            )
    except discord.HTTPException:
        pass






# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    ensure_data_dirs()
    client.run(TOKEN)
