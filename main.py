import os
import re
import csv
import json
import time
import shutil
import sqlite3
import logging
import tempfile
import threading
from pathlib import Path
from collections import Counter, defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tx_tool")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("TX_TOOL_DB", "tx_tool.db")
BACKUP_DIR = os.getenv("TX_TOOL_BACKUP_DIR", "backups")
ADMIN_IDS_RAW = os.getenv("TX_TOOL_ADMINS", "")

APP_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
LOGIC_VERSION = os.getenv("TX_TOOL_LOGIC_VERSION", "vip-3.0")
AUTO_RESET_TIME = os.getenv("TX_TOOL_AUTO_RESET_TIME", "00:00")  # HH:MM
AUTO_RESET_ENABLED_DEFAULT = os.getenv("TX_TOOL_AUTO_RESET_ENABLED", "1") == "1"

if not TOKEN:
    raise RuntimeError("❌ Thiếu TELEGRAM_BOT_TOKEN")

MAX_HISTORY = 300
MIN_HISTORY = 12
RECENT_PRED_LEN = 6
MIN_MARKOV_SUPPORT = 5
MIN_CONFIDENCE = 70
MAX_NUMS_PER_MESSAGE = 10

DEFAULT_ENGINE_STATE = {
    "markov": {"win": 0, "loss": 0},
    "pattern": {"win": 0, "loss": 0},
    "cycle": {"win": 0, "loss": 0},
    "trend": {"win": 0, "loss": 0},
}

db_lock = threading.RLock()
state_lock = threading.RLock()

Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)


# =========================
# UTILS
# =========================
def now_vn() -> datetime:
    return datetime.now(APP_TZ)


def vn_date() -> str:
    return now_vn().strftime("%Y-%m-%d")


def fresh_engine_state():
    return {k: {"win": 0, "loss": 0} for k in DEFAULT_ENGINE_STATE}


def safe_json_load(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def parse_hhmm(text: str):
    m = re.fullmatch(r"(\d{2}):(\d{2})", (text or "").strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}"
    return None


def is_admin(update: Update):
    if not update.effective_user:
        return False
    uid = update.effective_user.id
    admin_ids = set()
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            admin_ids.add(int(x))
    return uid in admin_ids


def get_key(update: Update):
    return update.effective_user.id if update.effective_user else update.effective_chat.id


def safe_int(text: str, default=None):
    try:
        return int(text)
    except Exception:
        return default


def to_tx(n: int) -> str:
    return "Tài" if n >= 11 else "Xỉu"


def parse_input(text: str):
    nums = []
    for s in re.findall(r"\d+", text or ""):
        n = int(s)
        if 1 <= n <= 18:
            nums.append(n)
    return nums[:MAX_NUMS_PER_MESSAGE]


def history_tx(history):
    return [x[0] for x in history]


def history_bar(history):
    if not history:
        return "—"
    return " ".join("⚫" if x[0] == "Tài" else "⚪" for x in history[-20:])


def update_engine_perf(engine_perf, votes, actual):
    for engine, pred in votes.items():
        if engine not in engine_perf:
            engine_perf[engine] = {"win": 0, "loss": 0}
        if pred == actual:
            engine_perf[engine]["win"] += 1
        else:
            engine_perf[engine]["loss"] += 1


def engine_weight(engine_perf, engine_name):
    s = engine_perf.get(engine_name, {"win": 0, "loss": 0})
    w = s.get("win", 0)
    l = s.get("loss", 0)
    acc = (w + 1) / (w + l + 2)
    return 0.75 + acc * 1.5


def format_prediction_detail(state):
    pred = state.get("pending_prediction")
    conf = state.get("pending_conf", 0)
    reason = state.get("pending_reason", "")
    votes = state.get("pending_votes", {})

    if not pred:
        return (
            "🔮 Dự đoán tiếp theo: BỎ KÈO\n"
            f"📝 {reason}"
        )

    vote_text = ", ".join(f"{k}:{v}" for k, v in votes.items()) if votes else "—"
    return (
        f"🔮 Dự đoán tiếp theo: {'⚫ TÀI' if pred == 'Tài' else '⚪ XỈU'}\n"
        f"📈 Tỷ lệ: {conf}%\n"
        f"🧠 {reason}\n"
        f"🗳️ Votes: {vote_text}"
    )


# =========================
# DB
# =========================
def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table_name: str):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r["name"] for r in rows}


def ensure_column(conn, table_name: str, column_def: str):
    col_name = column_def.split()[0]
    cols = table_columns(conn, table_name)
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")


def init_db():
    with db_lock:
        with db_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    win INTEGER NOT NULL DEFAULT 0,
                    lose INTEGER NOT NULL DEFAULT 0,
                    session_win INTEGER NOT NULL DEFAULT 0,
                    session_lose INTEGER NOT NULL DEFAULT 0,
                    streak_win INTEGER NOT NULL DEFAULT 0,
                    streak_lose INTEGER NOT NULL DEFAULT 0,
                    ai_mode TEXT NOT NULL DEFAULT 'WAIT',
                    pending_prediction TEXT,
                    pending_prediction_id INTEGER,
                    pending_conf REAL NOT NULL DEFAULT 0,
                    pending_reason TEXT,
                    pending_votes_json TEXT,
                    recent_preds_json TEXT NOT NULL DEFAULT '[]',
                    engine_perf_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    actual TEXT NOT NULL,
                    number INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL,
                    predicted TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    reason TEXT,
                    votes_json TEXT NOT NULL DEFAULT '{}',
                    actual TEXT,
                    actual_number INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            ensure_column(conn, "users", "win INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "lose INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "session_win INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "session_lose INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "streak_win INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "streak_lose INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "ai_mode TEXT NOT NULL DEFAULT 'WAIT'")
            ensure_column(conn, "users", "pending_prediction TEXT")
            ensure_column(conn, "users", "pending_prediction_id INTEGER")
            ensure_column(conn, "users", "pending_conf REAL NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "pending_reason TEXT")
            ensure_column(conn, "users", "pending_votes_json TEXT NOT NULL DEFAULT '{}'")
            ensure_column(conn, "users", "recent_preds_json TEXT NOT NULL DEFAULT '[]'")
            ensure_column(conn, "users", "engine_perf_json TEXT NOT NULL DEFAULT '{}'")
            ensure_column(conn, "users", "updated_at INTEGER NOT NULL DEFAULT 0")
            ensure_column(conn, "users", "created_at INTEGER NOT NULL DEFAULT 0")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user_ts ON history(user_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pred_user_ts ON predictions(user_id, id)")
            conn.commit()


def get_meta(key: str, default=None):
    with db_lock:
        with db_conn() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default


def set_meta(key: str, value: str):
    with db_lock:
        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO app_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()


def ensure_user(uid: int):
    now = int(time.time())
    with db_lock:
        with db_conn() as conn:
            row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, created_at, updated_at,
                        recent_preds_json, engine_perf_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (uid, now, now, "[]", json.dumps(DEFAULT_ENGINE_STATE, ensure_ascii=False)),
                )
                conn.commit()


def load_state(uid: int):
    ensure_user(uid)
    with db_lock:
        with db_conn() as conn:
            u = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            history_rows = conn.execute(
                """
                SELECT actual, number, ts
                FROM history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (uid, MAX_HISTORY),
            ).fetchall()

            history = [(r["actual"], int(r["number"]), int(r["ts"])) for r in reversed(history_rows)]
            pending_votes = safe_json_load(u["pending_votes_json"] or "{}", {})
            recent_preds = deque(safe_json_load(u["recent_preds_json"] or "[]", []), maxlen=RECENT_PRED_LEN)
            engine_perf = safe_json_load(u["engine_perf_json"] or "{}", {})

            for k in DEFAULT_ENGINE_STATE:
                engine_perf.setdefault(k, {"win": 0, "loss": 0})

            return {
                "user_id": uid,
                "history": history,
                "win": int(u["win"]),
                "lose": int(u["lose"]),
                "session_win": int(u["session_win"]),
                "session_lose": int(u["session_lose"]),
                "streak_win": int(u["streak_win"]),
                "streak_lose": int(u["streak_lose"]),
                "ai_mode": u["ai_mode"] or "WAIT",
                "pending_prediction": u["pending_prediction"],
                "pending_prediction_id": u["pending_prediction_id"],
                "pending_conf": float(u["pending_conf"] or 0),
                "pending_reason": u["pending_reason"] or "",
                "pending_votes": pending_votes,
                "recent_preds": recent_preds,
                "engine_perf": engine_perf,
            }


def save_user_state(state):
    uid = state["user_id"]
    with db_lock:
        with db_conn() as conn:
            conn.execute(
                """
                UPDATE users SET
                    updated_at = ?,
                    win = ?,
                    lose = ?,
                    session_win = ?,
                    session_lose = ?,
                    streak_win = ?,
                    streak_lose = ?,
                    ai_mode = ?,
                    pending_prediction = ?,
                    pending_prediction_id = ?,
                    pending_conf = ?,
                    pending_reason = ?,
                    pending_votes_json = ?,
                    recent_preds_json = ?,
                    engine_perf_json = ?
                WHERE user_id = ?
                """,
                (
                    int(time.time()),
                    state["win"],
                    state["lose"],
                    state["session_win"],
                    state["session_lose"],
                    state["streak_win"],
                    state["streak_lose"],
                    state["ai_mode"],
                    state["pending_prediction"],
                    state["pending_prediction_id"],
                    float(state["pending_conf"]),
                    state["pending_reason"],
                    json.dumps(state["pending_votes"], ensure_ascii=False),
                    json.dumps(list(state["recent_preds"]), ensure_ascii=False),
                    json.dumps(state["engine_perf"], ensure_ascii=False),
                    uid,
                ),
            )
            conn.commit()


def insert_history(uid: int, actual: str, number: int):
    with db_lock:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO history (user_id, ts, actual, number) VALUES (?, ?, ?, ?)",
                (uid, int(time.time()), actual, number),
            )
            conn.commit()


def insert_prediction(uid: int, predicted, confidence, reason, votes):
    with db_lock:
        with db_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO predictions (user_id, ts, predicted, confidence, status, reason, votes_json)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (uid, int(time.time()), predicted, float(confidence), reason, json.dumps(votes, ensure_ascii=False)),
            )
            conn.commit()
            return cur.lastrowid


def close_prediction(predicted_id: int, actual: str, actual_number: int):
    with db_lock:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT predicted FROM predictions WHERE id = ?",
                (predicted_id,),
            ).fetchone()
            if row is None:
                return
            status = "WIN" if row["predicted"] == actual else "LOSS"
            conn.execute(
                """
                UPDATE predictions
                SET status = ?, actual = ?, actual_number = ?
                WHERE id = ?
                """,
                (status, actual, actual_number, predicted_id),
            )
            conn.commit()


def count_rows(uid: int):
    with db_lock:
        with db_conn() as conn:
            h = conn.execute("SELECT COUNT(*) AS c FROM history WHERE user_id = ?", (uid,)).fetchone()["c"]
            p = conn.execute("SELECT COUNT(*) AS c FROM predictions WHERE user_id = ?", (uid,)).fetchone()["c"]
            u = conn.execute("SELECT updated_at FROM users WHERE user_id = ?", (uid,)).fetchone()
            return int(h), int(p), int(u["updated_at"] if u else 0)


# =========================
# ADMIN / RESET / META
# =========================
def make_backup_copy():
    if not os.path.exists(DB_PATH):
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"tx_tool_{stamp}.db")
    shutil.copy2(DB_PATH, dest)
    return dest


def reset_user_state(uid: int, clear_history: bool = False):
    ensure_user(uid)
    with state_lock:
        with db_lock:
            with db_conn() as conn:
                if clear_history:
                    conn.execute("DELETE FROM history WHERE user_id = ?", (uid,))
                    conn.execute("DELETE FROM predictions WHERE user_id = ?", (uid,))

                conn.execute(
                    """
                    UPDATE users SET
                        updated_at = ?,
                        win = 0,
                        lose = 0,
                        session_win = 0,
                        session_lose = 0,
                        streak_win = 0,
                        streak_lose = 0,
                        ai_mode = 'WAIT',
                        pending_prediction = NULL,
                        pending_prediction_id = NULL,
                        pending_conf = 0,
                        pending_reason = NULL,
                        pending_votes_json = ?,
                        recent_preds_json = '[]',
                        engine_perf_json = ?
                    WHERE user_id = ?
                    """,
                    (
                        int(time.time()),
                        json.dumps({}, ensure_ascii=False),
                        json.dumps(DEFAULT_ENGINE_STATE, ensure_ascii=False),
                        uid,
                    ),
                )
                conn.commit()


def reset_all_users(clear_history: bool = True):
    with state_lock:
        with db_lock:
            with db_conn() as conn:
                if clear_history:
                    conn.execute("DELETE FROM history")
                    conn.execute("DELETE FROM predictions")

                conn.execute(
                    """
                    UPDATE users SET
                        updated_at = ?,
                        win = 0,
                        lose = 0,
                        session_win = 0,
                        session_lose = 0,
                        streak_win = 0,
                        streak_lose = 0,
                        ai_mode = 'WAIT',
                        pending_prediction = NULL,
                        pending_prediction_id = NULL,
                        pending_conf = 0,
                        pending_reason = NULL,
                        pending_votes_json = ?,
                        recent_preds_json = '[]',
                        engine_perf_json = ?
                    """,
                    (
                        int(time.time()),
                        json.dumps({}, ensure_ascii=False),
                        json.dumps(DEFAULT_ENGINE_STATE, ensure_ascii=False),
                    ),
                )
                conn.commit()


def get_auto_reset_time():
    return parse_hhmm(get_meta("auto_reset_time", AUTO_RESET_TIME)) or "00:00"


def get_auto_reset_enabled():
    raw = get_meta("auto_reset_enabled", "1" if AUTO_RESET_ENABLED_DEFAULT else "0")
    return str(raw).strip() == "1"


def should_auto_reset_now():
    if not get_auto_reset_enabled():
        return False

    now = now_vn()
    target = get_auto_reset_time()
    hh, mm = map(int, target.split(":"))
    today = now.strftime("%Y-%m-%d")
    last_done = get_meta("last_auto_reset_date", "")
    return now.hour == hh and now.minute == mm and last_done != today


def apply_logic_migration():
    saved = get_meta("logic_version", "")
    if saved != LOGIC_VERSION:
        logger.warning("Logic version changed: %s -> %s", saved, LOGIC_VERSION)
        if os.path.exists(DB_PATH):
            make_backup_copy()
        reset_all_users(clear_history=True)
        set_meta("logic_version", LOGIC_VERSION)


def reset_scheduler_loop():
    while True:
        try:
            if should_auto_reset_now():
                logger.info("Auto reset triggered at %s", now_vn().strftime("%Y-%m-%d %H:%M:%S"))
                make_backup_copy()
                reset_all_users(clear_history=True)
                set_meta("last_auto_reset_date", vn_date())
        except Exception:
            logger.exception("Auto reset scheduler failed")
        time.sleep(20)


# =========================
# AI LOGIC
# =========================
def meta_ai(state):
    h = history_tx(state["history"])

    if len(h) < MIN_HISTORY:
        return "WAIT", "📊 Đang học"

    tail = h[-8:]
    if len(tail) >= 8 and all(tail[i] != tail[i - 1] for i in range(1, len(tail))):
        return "NOISE", "🚫 Cầu nhiễu"

    if state["streak_lose"] >= 3:
        return "STOP", "🛑 Thua chuỗi"

    if len(h) >= 6 and len(set(h[-6:])) == 1:
        return "STRONG", "🔥 Bệt mạnh"

    t = h.count("Tài")
    x = h.count("Xỉu")
    if abs(t - x) >= 12:
        return "BIAS", "⚖️ Lệch mạnh"

    return "OK", "✅ Ổn định"


def predict_markov(state):
    h = history_tx(state["history"])
    if len(h) < 4:
        return None, 0, "Markov: thiếu dữ liệu"

    key = tuple(h[-4:-1])
    c = Counter()

    for i in range(len(h) - 3):
        if tuple(h[i:i + 3]) == key and i + 3 < len(h):
            c[h[i + 3]] += 1

    total = sum(c.values())
    if total < MIN_MARKOV_SUPPORT:
        return None, 0, f"Markov: support thấp ({total})"

    p = c.most_common(1)[0][0]
    conf = (c[p] / total) * 100
    return p, conf, f"Markov({total})"


def predict_pattern(state):
    h = history_tx(state["history"])
    if len(h) < 4:
        return None, 0, "Pattern: thiếu dữ liệu"

    key = tuple(h[-3:])
    score = Counter()

    for i in range(len(h) - 3):
        if tuple(h[i:i + 3]) == key and i + 3 < len(h):
            score[h[i + 3]] += 1

    if not score:
        return None, 0, "Pattern: không khớp"

    p = score.most_common(1)[0][0]
    total = sum(score.values())
    conf = (score[p] / total) * 100
    return p, conf, f"Pattern({total})"


def predict_cycle(state):
    h = history_tx(state["history"][-40:])
    if len(h) < 8:
        return None, 0, "Cycle: chưa đủ"

    best_pred = None
    best_score = 0

    for size in range(2, 6):
        if len(h) <= size + 1:
            continue

        seq = tuple(h[-size:])
        followers = []

        for i in range(len(h) - size):
            if tuple(h[i:i + size]) == seq and i + size < len(h):
                followers.append(h[i + size])

        if len(followers) >= 2:
            c = Counter(followers)
            p = c.most_common(1)[0][0]
            score = len(followers) * 10
            if score > best_score:
                best_pred = p
                best_score = score

    if best_pred is None:
        return None, 0, "Cycle: không có chu kỳ"

    return best_pred, min(best_score, 80), "Cycle"


def predict_trend(state):
    h = history_tx(state["history"][-10:])
    if len(h) < 6:
        return None, 0, "Trend: thiếu dữ liệu"

    t = h.count("Tài")
    x = h.count("Xỉu")
    if max(t, x) >= 5:
        p = "Tài" if t > x else "Xỉu"
        conf = 55 + (max(t, x) - 4) * 5
        return p, min(conf, 75), f"Trend({t}:{x})"

    return None, 0, "Trend: yếu"


def stability_check(state, new_pred):
    recent = list(state["recent_preds"])
    recent.append(new_pred)

    if len(recent) < 4:
        return False, "⏳ Chưa đủ ổn định"

    if len(set(recent[-5:])) > 2:
        return False, "🚫 Tín hiệu nhảy quá nhanh"

    return True, "✅ Ổn định"


def final_ai(state):
    mode, note = meta_ai(state)
    state["ai_mode"] = mode

    if mode in {"WAIT", "NOISE", "STOP"}:
        return None, 0, note, {}

    engines = [
        ("markov", predict_markov),
        ("pattern", predict_pattern),
        ("cycle", predict_cycle),
        ("trend", predict_trend),
    ]

    votes = {}
    score = defaultdict(float)

    for name, fn in engines:
        p, conf, _ = fn(state)
        if p:
            votes[name] = p
            score[p] += conf * engine_weight(state["engine_perf"], name)

    if len(votes) < 2:
        return None, 0, "❓ Chưa đủ tín hiệu", votes

    cnt = Counter(votes.values())
    if cnt.most_common(1)[0][1] < 2:
        return None, 0, "🚫 Không đồng thuận", votes

    best = max(score, key=score.get)
    total = sum(score.values())
    conf = int((score[best] / total) * 100) if total else 0

    stable, stable_note = stability_check(state, best)
    if not stable:
        return None, conf, stable_note, votes

    if state["session_lose"] >= 2:
        conf = int(conf * 0.75)

    if conf < MIN_CONFIDENCE:
        return None, conf, "🚫 Kèo chưa đủ mạnh", votes

    state["recent_preds"].append(best)
    return best, min(conf, 95), f"{note} | {stable_note} | 🤖 AI LOCK", votes


# =========================
# CORE PROCESS
# =========================
def process_number(state, n: int):
    with state_lock:
        actual = to_tx(n)
        verdict = None

        if state["pending_prediction"] is not None and state["pending_prediction_id"] is not None:
            if state["pending_prediction"] == actual:
                state["win"] += 1
                state["session_win"] += 1
                state["streak_win"] += 1
                state["streak_lose"] = 0
                verdict = "✅ ĐÚNG"
            else:
                state["lose"] += 1
                state["session_lose"] += 1
                state["streak_lose"] += 1
                state["streak_win"] = 0
                verdict = "❌ SAI"

            update_engine_perf(state["engine_perf"], state["pending_votes"], actual)
            close_prediction(state["pending_prediction_id"], actual, n)

        state["history"].append((actual, n, int(time.time())))
        if len(state["history"]) > MAX_HISTORY:
            state["history"] = state["history"][-MAX_HISTORY:]

        insert_history(state["user_id"], actual, n)

        pred, conf, reason, votes = final_ai(state)
        state["pending_prediction"] = pred
        state["pending_conf"] = conf
        state["pending_reason"] = reason
        state["pending_votes"] = votes

        if pred is not None:
            pid = insert_prediction(state["user_id"], pred, conf, reason, votes)
            state["pending_prediction_id"] = pid
        else:
            state["pending_prediction_id"] = None

        save_user_state(state)
        return actual, verdict, pred, conf, reason


def format_pending(pred, conf):
    if not pred:
        return "🚫 BỎ KÈO"
    return f"🎯 {'⚫ TÀI' if pred == 'Tài' else '⚪ XỈU'} | 🔥 {conf}%"


def format_stats_text(state):
    return (
        "📊 THỐNG KÊ\n"
        f"• Win: {state['win']}\n"
        f"• Lose: {state['lose']}\n"
        f"• Session Win: {state['session_win']}\n"
        f"• Session Lose: {state['session_lose']}\n"
        f"• Streak Win: {state['streak_win']}\n"
        f"• Streak Lose: {state['streak_lose']}\n"
        f"• Mode: {state['ai_mode']}\n"
        f"• Pending: {format_pending(state['pending_prediction'], state['pending_conf'])}\n"
        f"• History: {history_bar(state['history'])}"
    )


def health_report(uid: int):
    h_count, p_count, updated_at = count_rows(uid)
    db_ok = os.path.exists(DB_PATH)
    db_size = os.path.getsize(DB_PATH) if db_ok else 0
    state = load_state(uid)
    return (
        "🩺 HEALTH\n"
        f"• DB: {'OK' if db_ok else 'MISSING'}\n"
        f"• DB size: {db_size} bytes\n"
        f"• History rows: {h_count}\n"
        f"• Prediction rows: {p_count}\n"
        f"• Last update: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated_at)) if updated_at else '—'}\n"
        f"• Mode: {state['ai_mode']}\n"
        f"• Pending: {format_pending(state['pending_prediction'], state['pending_conf'])}"
    )


def export_combined_csv(uid: int):
    ensure_user(uid)
    fd, path = tempfile.mkstemp(prefix=f"tx_export_{uid}_", suffix=".csv")
    os.close(fd)

    with db_lock:
        with db_conn() as conn, open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "row_type", "id", "ts",
                "actual", "number",
                "predicted", "confidence",
                "status", "reason", "votes_json"
            ])

            rows_h = conn.execute(
                "SELECT id, ts, actual, number FROM history WHERE user_id = ? ORDER BY id ASC",
                (uid,),
            ).fetchall()
            for r in rows_h:
                writer.writerow([
                    "history", r["id"], r["ts"],
                    r["actual"], r["number"],
                    "", "", "", "", ""
                ])

            rows_p = conn.execute(
                """
                SELECT id, ts, predicted, confidence, status, reason, votes_json, actual, actual_number
                FROM predictions
                WHERE user_id = ?
                ORDER BY id ASC
                """,
                (uid,),
            ).fetchall()
            for r in rows_p:
                writer.writerow([
                    "prediction", r["id"], r["ts"],
                    r["actual"] or "", r["actual_number"] or "",
                    r["predicted"] or "", r["confidence"],
                    r["status"] or "", r["reason"] or "", r["votes_json"] or "{}"
                ])

    return path


def backtest_core(history_sample):
    sim = {
        "history": [],
        "streak_lose": 0,
        "recent_preds": deque(maxlen=RECENT_PRED_LEN),
        "engine_perf": fresh_engine_state(),
        "ai_mode": "WAIT",
        "session_lose": 0,
    }

    correct = 0
    total_pred = 0
    coverage = 0

    for actual, number, ts in history_sample:
        if sim["history"]:
            pred, conf, reason, votes = final_ai(sim)
            if pred is not None:
                total_pred += 1
                coverage += 1
                if pred == actual:
                    correct += 1
                update_engine_perf(sim["engine_perf"], votes, actual)

        sim["history"].append((actual, number, ts))
        if len(sim["history"]) > MAX_HISTORY:
            sim["history"] = sim["history"][-MAX_HISTORY:]

    acc = (correct / total_pred * 100) if total_pred else 0
    cov = (coverage / max(len(history_sample) - 1, 1)) * 100
    return {
        "samples": len(history_sample),
        "signals": total_pred,
        "correct": correct,
        "accuracy": acc,
        "coverage": cov,
        "engine_perf": sim["engine_perf"],
    }


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    ensure_user(uid)
    await update.message.reply_text(
        "🤖 Bot đã sẵn sàng.\n"
        "Gửi số 1–18, có thể nhập nhiều số cùng lúc.\n"
        "Ví dụ: 3-11-7-18\n\n"
        "Lệnh: /stats /engine /backtest /export /backup /health /reset /help /config /version"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "• Gửi số 1–18 để nhập kết quả\n"
        "• /stats xem thống kê\n"
        "• /engine xem hiệu suất từng engine\n"
        "• /backtest [n] chạy test lịch sử gần nhất\n"
        "• /export xuất dữ liệu CSV\n"
        "• /backup tạo bản sao DB\n"
        "• /health kiểm tra trạng thái\n"
        "• /reset xóa dữ liệu user hiện tại\n"
        "• /resetall reset toàn bộ hệ thống (admin)\n"
        "• /setreset HH:MM đặt lịch reset tự động (admin)\n"
        "• /autoreset on|off bật/tắt reset tự động (admin)\n"
        "• /config xem cấu hình hiện tại\n"
        "• /version xem phiên bản logic"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    make_backup_copy()
    reset_user_state(uid, clear_history=True)
    await update.message.reply_text("♻️ Đã reset toàn bộ dữ liệu của user này.")


async def cmd_resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Lệnh này chỉ dành cho admin.")
        return

    keep_history = False
    if context.args and context.args[0].lower() == "keep":
        keep_history = True

    make_backup_copy()
    reset_all_users(clear_history=not keep_history)
    set_meta("last_auto_reset_date", vn_date())

    await update.message.reply_text(
        f"✅ Đã reset toàn bộ hệ thống.\n"
        f"• Keep history: {keep_history}\n"
        f"• Logic version: {LOGIC_VERSION}"
    )


async def cmd_setreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Lệnh này chỉ dành cho admin.")
        return

    if not context.args:
        await update.message.reply_text("Dùng: /setreset HH:MM")
        return

    t = parse_hhmm(context.args[0])
    if not t:
        await update.message.reply_text("Giờ không hợp lệ. Ví dụ đúng: /setreset 00:00")
        return

    set_meta("auto_reset_time", t)
    set_meta("last_auto_reset_date", "")
    await update.message.reply_text(f"✅ Đã đặt lịch reset tự động mỗi ngày lúc {t} (giờ VN).")


async def cmd_autoreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Lệnh này chỉ dành cho admin.")
        return

    if not context.args:
        current = "ON" if get_auto_reset_enabled() else "OFF"
        await update.message.reply_text(
            f"⚙️ Auto reset hiện tại: {current}\n"
            f"Dùng: /autoreset on hoặc /autoreset off"
        )
        return

    arg = context.args[0].lower().strip()
    if arg in {"on", "1", "true", "yes"}:
        set_meta("auto_reset_enabled", "1")
        await update.message.reply_text("✅ Đã bật auto reset.")
    elif arg in {"off", "0", "false", "no"}:
        set_meta("auto_reset_enabled", "0")
        await update.message.reply_text("✅ Đã tắt auto reset.")
    else:
        await update.message.reply_text("Dùng: /autoreset on hoặc /autoreset off")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧩 VIP VERSION\n"
        f"• Logic: {LOGIC_VERSION}\n"
        f"• Auto reset: {'ON' if get_auto_reset_enabled() else 'OFF'}\n"
        f"• Reset time: {get_auto_reset_time()} (VN)\n"
        f"• DB: {DB_PATH}"
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ CONFIG\n"
        f"• Logic version: {LOGIC_VERSION}\n"
        f"• Auto reset: {'ON' if get_auto_reset_enabled() else 'OFF'}\n"
        f"• Auto reset time: {get_auto_reset_time()} (VN)\n"
        f"• DB path: {DB_PATH}\n"
        f"• Backup dir: {BACKUP_DIR}\n"
        f"• Max history: {MAX_HISTORY}\n"
        f"• Min history: {MIN_HISTORY}\n"
        f"• Min confidence: {MIN_CONFIDENCE}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    state = load_state(uid)
    await update.message.reply_text(format_stats_text(state))


async def cmd_engine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    state = load_state(uid)
    ep = state["engine_perf"]

    def line(name):
        s = ep.get(name, {"win": 0, "loss": 0})
        w, l = s.get("win", 0), s.get("loss", 0)
        total = w + l
        acc = (w / total * 100) if total else 0
        return f"• {name}: W{w} / L{l} | {acc:.1f}%"

    await update.message.reply_text(
        "🧠 ENGINE PERFORMANCE\n" + "\n".join(line(k) for k in ["markov", "pattern", "cycle", "trend"])
    )


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    state = load_state(uid)

    limit = safe_int(context.args[0], 120) if getattr(context, "args", None) else 120
    limit = max(20, min(limit, len(state["history"])))

    sample = state["history"][-limit:]
    if len(sample) < MIN_HISTORY:
        await update.message.reply_text("Chưa đủ dữ liệu để backtest.")
        return

    result = backtest_core(sample)
    await update.message.reply_text(
        "📉 BACKTEST\n"
        f"• Mẫu: {result['samples']}\n"
        f"• Kèo đã ra: {result['signals']}\n"
        f"• Đúng: {result['correct']}\n"
        f"• Accuracy: {result['accuracy']:.1f}%\n"
        f"• Coverage: {result['coverage']:.1f}%"
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    path = export_combined_csv(uid)
    try:
        await update.message.reply_document(
            document=InputFile(path),
            caption="📦 Export dữ liệu CSV",
        )
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("DB chưa tồn tại.")
        return
    dest = make_backup_copy()
    if not dest:
        await update.message.reply_text("Backup thất bại.")
        return
    await update.message.reply_text(f"✅ Đã backup DB:\n{dest}")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = get_key(update)
    await update.message.reply_text(health_report(uid))


async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Lệnh này chỉ dành cho admin.")
        return

    if not context.args:
        await update.message.reply_text("Dùng: /purge <user_id>")
        return

    target = safe_int(context.args[0], None)
    if target is None:
        await update.message.reply_text("user_id không hợp lệ.")
        return

    with state_lock:
        with db_lock:
            with db_conn() as conn:
                conn.execute("DELETE FROM history WHERE user_id = ?", (target,))
                conn.execute("DELETE FROM predictions WHERE user_id = ?", (target,))
                conn.execute("DELETE FROM users WHERE user_id = ?", (target,))
                conn.commit()

    await update.message.reply_text(f"🗑️ Đã xóa toàn bộ dữ liệu của user {target}.")


# =========================
# MESSAGE HANDLER
# =========================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    uid = get_key(update)
    state = load_state(uid)

    nums = parse_input(update.message.text)
    if not nums:
        return

    blocks = []
    last_reason = None

    for n in nums:
        actual, verdict, pred, conf, reason = process_number(state, n)
        last_reason = reason

        line = f"• {n} → {actual}"
        if verdict:
            line += f" | {verdict}"

        blocks.append(line)
        blocks.append(format_prediction_detail(state))
        blocks.append("")

    text = (
        "╔══ 🤖 ULTIMATE TOOL AI ══╗\n"
        f"{last_reason or '—'}\n\n"
        + "\n".join(blocks)
        + "\n"
        + f"📊 {history_bar(state['history'])}\n"
        + f"📈 W:{state['win']} | L:{state['lose']}\n"
        + f"🔮 {format_pending(state['pending_prediction'], state['pending_conf'])}\n"
        + "╚════════════════════════╝"
    )
    await update.message.reply_text(text)


# =========================
# ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update error: %s", context.error)


# =========================
# RUN
# =========================
def main():
    init_db()
    apply_logic_migration()
    logger.info("Database ready: %s", DB_PATH)

    scheduler_thread = threading.Thread(target=reset_scheduler_loop, daemon=True)
    scheduler_thread.start()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("resetall", cmd_resetall))
    app.add_handler(CommandHandler("setreset", cmd_setreset))
    app.add_handler(CommandHandler("autoreset", cmd_autoreset))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("engine", cmd_engine))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_error_handler(error_handler)

    print("🔥 ULTIMATE TOOL AI RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()
