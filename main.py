import os
import re
import json
import math
import sqlite3
import asyncio
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "ai_state.db")

THRESHOLD = int(os.getenv("THRESHOLD", "11"))
LOW_LABEL = os.getenv("LOW_LABEL", "Xỉu")
HIGH_LABEL = os.getenv("HIGH_LABEL", "Tài")

RECENT_WINDOW = int(os.getenv("RECENT_WINDOW", "80"))
MID_WINDOW = int(os.getenv("MID_WINDOW", "240"))
SHORT_WINDOW = int(os.getenv("SHORT_WINDOW", "24"))

MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))

MAX_LABELS_STORE = int(os.getenv("MAX_LABELS_STORE", "1200"))
MAX_DB_HISTORY = int(os.getenv("MAX_DB_HISTORY", "2500"))
MAX_HEALTH_LOG = int(os.getenv("MAX_HEALTH_LOG", "240"))

MAX_PATTERN_MEMORY = int(os.getenv("MAX_PATTERN_MEMORY", "1800"))
PATTERN_DECAY = float(os.getenv("PATTERN_DECAY", "0.990"))

MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "15"))
MAX_CYCLE_SCAN = int(os.getenv("MAX_CYCLE_SCAN", "5000"))
CYCLE_LOOKBACK = int(os.getenv("CYCLE_LOOKBACK", "360"))
MARKOV_LOOKBACK = int(os.getenv("MARKOV_LOOKBACK", "180"))

MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "62"))
ANALYSIS_DELAY_SECONDS = float(os.getenv("ANALYSIS_DELAY_SECONDS", "3"))

DRIFT_MIN_SHIFT = float(os.getenv("DRIFT_MIN_SHIFT", "0.18"))
DRIFT_STRONG_SHIFT = float(os.getenv("DRIFT_STRONG_SHIFT", "0.30"))
WHITE_SHIFT_THRESHOLD = float(os.getenv("WHITE_SHIFT_THRESHOLD", "0.32"))
WHITE_MIN_SCORE = float(os.getenv("WHITE_MIN_SCORE", "62"))

GHOST_HARD_CLEAN = float(os.getenv("GHOST_HARD_CLEAN", "85"))
GHOST_SOFT_CLEAN = float(os.getenv("GHOST_SOFT_CLEAN", "65"))
GHOST_WARN = float(os.getenv("GHOST_WARN", "55"))

if not TOKEN:
    raise RuntimeError("❌ Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()

users: Dict[int, Dict[str, Any]] = {}
analysis_tasks: Dict[int, asyncio.Task] = {}
analysis_versions: Dict[int, int] = defaultdict(int)


# =========================
# STATE / DB
# =========================
def new_user() -> Dict[str, Any]:
    return {
        "history": [],
        "values": [],
        "labels": [],
        "low_count": 0,
        "high_count": 0,
        "mode": "WARMUP",
        "ghost_mode": False,
        "updates": 0,
        "cleanups": 0,
        "last_clean_score": 0.0,
        "health_log": [],
        "monitor_log": [],
        "recheck_log": [],
        "recheck_count": 0,
        "last_recheck_score": 0.0,
        "stability_score": 100.0,
        "error_count": 0,
        "pattern_memory": defaultdict(float),
    }


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _ensure_column(conn: sqlite3.Connection, table: str, col_def: str) -> None:
    col_name = col_def.split()[0]
    cols = _table_columns(conn, table)
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                low_count INTEGER NOT NULL DEFAULT 0,
                high_count INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'WARMUP',
                ghost_mode INTEGER NOT NULL DEFAULT 0,
                updates INTEGER NOT NULL DEFAULT 0,
                cleanups INTEGER NOT NULL DEFAULT 0,
                last_clean_score REAL NOT NULL DEFAULT 0.0,
                stability_score REAL NOT NULL DEFAULT 100.0,
                error_count INTEGER NOT NULL DEFAULT 0,
                recheck_count INTEGER NOT NULL DEFAULT 0,
                last_recheck_score REAL NOT NULL DEFAULT 0.0,
                health_log_json TEXT NOT NULL DEFAULT '[]',
                monitor_log_json TEXT NOT NULL DEFAULT '[]',
                recheck_log_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                raw_value INTEGER NOT NULL,
                label TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'real',
                conf REAL NOT NULL DEFAULT 1.0,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);

            CREATE TABLE IF NOT EXISTS pattern_memory (
                chat_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, pattern)
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_chat_id ON pattern_memory(chat_id);
            """
        )
        _ensure_column(conn, "users", "ghost_mode INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "cleanups INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "last_clean_score REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "users", "stability_score REAL NOT NULL DEFAULT 100.0")
        _ensure_column(conn, "users", "error_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "recheck_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "users", "last_recheck_score REAL NOT NULL DEFAULT 0.0")
        _ensure_column(conn, "users", "monitor_log_json TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "users", "recheck_log_json TEXT NOT NULL DEFAULT '[]'")
        conn.commit()


async def wipe_all_state() -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute("DELETE FROM history")
            conn.execute("DELETE FROM pattern_memory")
            conn.execute("DELETE FROM users")
            conn.commit()


async def delete_chat_state(chat_id: int) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM pattern_memory WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
            conn.commit()


def _deserialize_float_map(src: Dict[str, float]) -> defaultdict:
    out = defaultdict(float)
    for k, v in (src or {}).items():
        out[k] = float(v)
    return out


def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)


def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    if limit <= 0:
        return []
    if len(seq) <= limit:
        return list(seq)
    return list(seq[-limit:])


def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), MAX_LABELS_STORE)
    d["labels"] = _safe_tail(d.get("labels", []), MAX_LABELS_STORE)
    d["history"] = _safe_tail(d.get("history", []), 50)
    d["health_log"] = _safe_tail(d.get("health_log", []), MAX_HEALTH_LOG)
    d["monitor_log"] = _safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG)
    d["recheck_log"] = _safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG)


def trim_pattern_memory(d: Dict[str, Any]) -> None:
    pm = d.get("pattern_memory", {})
    if not pm:
        return
    if len(pm) > MAX_PATTERN_MEMORY:
        items = sorted(pm.items(), key=lambda x: x[1], reverse=True)
        keep = dict(items[:MAX_PATTERN_MEMORY])
        pm.clear()
        pm.update(keep)


def prune_chat_rows(conn: sqlite3.Connection, chat_id: int, keep_limit: int) -> None:
    row = conn.execute(
        "SELECT id FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (chat_id, max(0, keep_limit - 1)),
    ).fetchone()
    if row:
        cutoff_id = int(row["id"])
        conn.execute("DELETE FROM history WHERE chat_id = ? AND id < ?", (chat_id, cutoff_id))


def rebuild_counters(d: Dict[str, Any]) -> None:
    labels = d.get("labels", [])
    d["low_count"] = labels.count(LOW_LABEL)
    d["high_count"] = labels.count(HIGH_LABEL)
    d["updates"] = len(labels)


def repair_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d.get("history"), list):
        d["history"] = []
    if not isinstance(d.get("values"), list):
        d["values"] = []
    if not isinstance(d.get("labels"), list):
        d["labels"] = []
    if not isinstance(d.get("health_log"), list):
        d["health_log"] = []
    if not isinstance(d.get("monitor_log"), list):
        d["monitor_log"] = []
    if not isinstance(d.get("recheck_log"), list):
        d["recheck_log"] = []
    if not isinstance(d.get("pattern_memory"), defaultdict):
        d["pattern_memory"] = _deserialize_float_map(dict(d.get("pattern_memory", {})))

    n = min(len(d["values"]), len(d["labels"]))
    if n == 0:
        d["values"] = []
        d["labels"] = []
    else:
        d["values"] = d["values"][-n:]
        d["labels"] = d["labels"][-n:]

    d.setdefault("mode", "WARMUP")
    d.setdefault("ghost_mode", False)
    d.setdefault("cleanups", 0)
    d.setdefault("last_clean_score", 0.0)
    d.setdefault("stability_score", 100.0)
    d.setdefault("error_count", 0)
    d.setdefault("recheck_count", 0)
    d.setdefault("last_recheck_score", 0.0)

    rebuild_counters(d)
    trim_state_memory(d)
    trim_pattern_memory(d)
    return d


async def load_user(chat_id: int) -> Dict[str, Any]:
    if chat_id in users:
        return repair_state(users[chat_id])

    state = new_user()

    async with DB_LOCK:
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT low_count, high_count, mode, ghost_mode, updates,
                       cleanups, last_clean_score, stability_score, error_count,
                       recheck_count, last_recheck_score,
                       health_log_json, monitor_log_json, recheck_log_json
                FROM users WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()

            if row:
                state["low_count"] = int(row["low_count"])
                state["high_count"] = int(row["high_count"])
                state["mode"] = row["mode"] or "WARMUP"
                state["ghost_mode"] = bool(int(row["ghost_mode"] or 0))
                state["updates"] = int(row["updates"])
                state["cleanups"] = int(row["cleanups"] or 0)
                state["last_clean_score"] = float(row["last_clean_score"] or 0.0)
                state["stability_score"] = float(row["stability_score"] or 100.0)
                state["error_count"] = int(row["error_count"] or 0)
                state["recheck_count"] = int(row["recheck_count"] or 0)
                state["last_recheck_score"] = float(row["last_recheck_score"] or 0.0)

                try:
                    state["health_log"] = list(json.loads(row["health_log_json"] or "[]"))
                except Exception:
                    state["health_log"] = []
                try:
                    state["monitor_log"] = list(json.loads(row["monitor_log_json"] or "[]"))
                except Exception:
                    state["monitor_log"] = []
                try:
                    state["recheck_log"] = list(json.loads(row["recheck_log_json"] or "[]"))
                except Exception:
                    state["recheck_log"] = []

            hist_rows = conn.execute(
                "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, MAX_DB_HISTORY),
            ).fetchall()

            for r in reversed(hist_rows):
                raw_value = int(r["raw_value"])
                label = r["label"]
                if label in (LOW_LABEL, HIGH_LABEL):
                    state["values"].append(raw_value)
                    state["labels"].append(label)

            pat_rows = conn.execute(
                """
                SELECT pattern, weight
                FROM pattern_memory
                WHERE chat_id = ?
                ORDER BY weight DESC, pattern ASC
                LIMIT ?
                """,
                (chat_id, MAX_PATTERN_MEMORY),
            ).fetchall()
            state["pattern_memory"] = _deserialize_float_map({r["pattern"]: float(r["weight"]) for r in pat_rows})

    state["history"] = [
        {"value": v, "label": l, "source": "real", "conf": 1.0}
        for v, l in list(zip(state["values"], state["labels"]))[-50:]
    ]
    repair_state(state)
    users[chat_id] = state
    trim_cache()
    return state


async def save_user_meta(chat_id: int, d: Dict[str, Any]) -> None:
    async with DB_LOCK:
        health_json = json.dumps(_safe_tail(d.get("health_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
        monitor_json = json.dumps(_safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
        recheck_json = json.dumps(_safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    chat_id, low_count, high_count, mode, ghost_mode,
                    updates, cleanups, last_clean_score, stability_score, error_count,
                    recheck_count, last_recheck_score,
                    health_log_json, monitor_log_json, recheck_log_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    low_count=excluded.low_count,
                    high_count=excluded.high_count,
                    mode=excluded.mode,
                    ghost_mode=excluded.ghost_mode,
                    updates=excluded.updates,
                    cleanups=excluded.cleanups,
                    last_clean_score=excluded.last_clean_score,
                    stability_score=excluded.stability_score,
                    error_count=excluded.error_count,
                    recheck_count=excluded.recheck_count,
                    last_recheck_score=excluded.last_recheck_score,
                    health_log_json=excluded.health_log_json,
                    monitor_log_json=excluded.monitor_log_json,
                    recheck_log_json=excluded.recheck_log_json
                """,
                (
                    chat_id,
                    int(d.get("low_count", 0)),
                    int(d.get("high_count", 0)),
                    d.get("mode", "WARMUP"),
                    1 if d.get("ghost_mode", False) else 0,
                    int(d.get("updates", 0)),
                    int(d.get("cleanups", 0)),
                    float(d.get("last_clean_score", 0.0)),
                    float(d.get("stability_score", 100.0)),
                    int(d.get("error_count", 0)),
                    int(d.get("recheck_count", 0)),
                    float(d.get("last_recheck_score", 0.0)),
                    health_json,
                    monitor_json,
                    recheck_json,
                ),
            )
            conn.commit()


async def persist_snapshot(
    chat_id: int,
    d: Dict[str, Any],
    entries: List[Tuple[int, str, str, float]],
) -> None:
    ghost_mode = bool(d.get("ghost_mode", False))
    keep_limit = max(400, MAX_DB_HISTORY // 2) if ghost_mode else MAX_DB_HISTORY

    async with DB_LOCK:
        with db_connect() as conn:
            for raw_value, label, source, conf in entries:
                conn.execute(
                    "INSERT INTO history (chat_id, raw_value, label, source, conf) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, int(raw_value), label, source, float(conf)),
                )

            conn.execute("DELETE FROM pattern_memory WHERE chat_id = ?", (chat_id,))
            for pattern, weight in d.get("pattern_memory", {}).items():
                if float(weight) >= 0.05:
                    conn.execute(
                        "INSERT INTO pattern_memory (chat_id, pattern, weight) VALUES (?, ?, ?)",
                        (chat_id, pattern, float(weight)),
                    )

            health_json = json.dumps(_safe_tail(d.get("health_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
            monitor_json = json.dumps(_safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
            recheck_json = json.dumps(_safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO users (
                    chat_id, low_count, high_count, mode, ghost_mode,
                    updates, cleanups, last_clean_score, stability_score, error_count,
                    recheck_count, last_recheck_score,
                    health_log_json, monitor_log_json, recheck_log_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    low_count=excluded.low_count,
                    high_count=excluded.high_count,
                    mode=excluded.mode,
                    ghost_mode=excluded.ghost_mode,
                    updates=excluded.updates,
                    cleanups=excluded.cleanups,
                    last_clean_score=excluded.last_clean_score,
                    stability_score=excluded.stability_score,
                    error_count=excluded.error_count,
                    recheck_count=excluded.recheck_count,
                    last_recheck_score=excluded.last_recheck_score,
                    health_log_json=excluded.health_log_json,
                    monitor_log_json=excluded.monitor_log_json,
                    recheck_log_json=excluded.recheck_log_json
                """,
                (
                    chat_id,
                    int(d.get("low_count", 0)),
                    int(d.get("high_count", 0)),
                    d.get("mode", "WARMUP"),
                    1 if ghost_mode else 0,
                    int(d.get("updates", 0)),
                    int(d.get("cleanups", 0)),
                    float(d.get("last_clean_score", 0.0)),
                    float(d.get("stability_score", 100.0)),
                    int(d.get("error_count", 0)),
                    int(d.get("recheck_count", 0)),
                    float(d.get("last_recheck_score", 0.0)),
                    health_json,
                    monitor_json,
                    recheck_json,
                ),
            )

            prune_chat_rows(conn, chat_id, keep_limit)
            conn.commit()


# =========================
# UTILS
# =========================
def get_key(update: Update) -> int:
    return update.effective_chat.id


def map_value(n: int) -> str:
    return HIGH_LABEL if n >= THRESHOLD else LOW_LABEL


def parse_input(text: str) -> List[int]:
    nums: List[int] = []
    for x in re.findall(r"\d+", text or ""):
        try:
            n = int(x)
            if n > 0:
                nums.append(n)
        except ValueError:
            pass
    return nums[:MAX_INPUT_NUMS]


def format_history(labels: List[str], tail: int = 24) -> str:
    out = []
    for lb in labels[-tail:]:
        out.append("⬛" if lb == HIGH_LABEL else "⬜" if lb == LOW_LABEL else "·")
    return "".join(out) if out else "(trống)"


def format_pattern_lines(patterns: List[Tuple[str, float]]) -> str:
    if not patterns:
        return "Không có"
    return " | ".join([f"{p}({w:.1f})" for p, w in patterns[:3]])


def format_prob_inline(probs: Dict[str, float]) -> str:
    low_p = probs.get(LOW_LABEL, 0.5) * 100
    high_p = probs.get(HIGH_LABEL, 0.5) * 100
    return f"{LOW_LABEL}: {low_p:.1f}% | {HIGH_LABEL}: {high_p:.1f}%"


def normalize_probs(scores: Dict[str, float]) -> Dict[str, float]:
    for k in (LOW_LABEL, HIGH_LABEL):
        scores.setdefault(k, 0.0)
    for k in list(scores.keys()):
        if scores[k] < 0:
            scores[k] = 0.0
    total = scores[LOW_LABEL] + scores[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {
        LOW_LABEL: scores[LOW_LABEL] / total,
        HIGH_LABEL: scores[HIGH_LABEL] / total,
    }


def split_layers(labels: List[str]) -> Tuple[List[str], List[str], List[str]]:
    recent = labels[-RECENT_WINDOW:] if len(labels) > RECENT_WINDOW else labels[:]
    mid_end = max(0, len(labels) - len(recent))
    mid_start = max(0, mid_end - MID_WINDOW)
    mid = labels[mid_start:mid_end]
    old = labels[:mid_start]
    return recent, mid, old


def layer_ratio(labels: List[str]) -> Dict[str, float]:
    if not labels:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(labels)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: c[LOW_LABEL] / total, HIGH_LABEL: c[HIGH_LABEL] / total}


def jensen_shannon_divergence(p: Dict[str, float], q: Dict[str, float]) -> float:
    def kl(a: Dict[str, float], b: Dict[str, float]) -> float:
        s = 0.0
        for k in (LOW_LABEL, HIGH_LABEL):
            av = max(a.get(k, 0.0), 1e-12)
            bv = max(b.get(k, 0.0), 1e-12)
            s += av * math.log2(av / bv)
        return s

    m = {
        LOW_LABEL: (p.get(LOW_LABEL, 0.5) + q.get(LOW_LABEL, 0.5)) / 2.0,
        HIGH_LABEL: (p.get(HIGH_LABEL, 0.5) + q.get(HIGH_LABEL, 0.5)) / 2.0,
    }
    return max(0.0, min(1.0, 0.5 * kl(p, m) + 0.5 * kl(q, m)))


def decay_pattern_memory(d: Dict[str, Any]) -> None:
    pm = d.get("pattern_memory", {})
    if not pm:
        return
    for k in list(pm.keys()):
        pm[k] *= PATTERN_DECAY
        if pm[k] < 0.05:
            del pm[k]
    trim_pattern_memory(d)


def update_pattern_memory_in_memory(d: Dict[str, Any]) -> None:
    labels = d.get("labels", [])
    if len(labels) >= 4:
        d["pattern_memory"]["|".join(labels[-4:])] += 1.0
    if len(labels) >= 5:
        d["pattern_memory"]["|".join(labels[-5:])] += 0.7
    if len(labels) >= 6:
        d["pattern_memory"]["|".join(labels[-6:])] += 0.5
    decay_pattern_memory(d)


def current_streak(labels: List[str]) -> Tuple[Optional[str], int]:
    if not labels:
        return None, 0
    last = labels[-1]
    streak = 1
    for i in range(len(labels) - 2, -1, -1):
        if labels[i] == last:
            streak += 1
        else:
            break
    return last, streak


def alternating_analysis(labels: List[str]) -> Tuple[bool, float]:
    if len(labels) < 6:
        return False, 0.0
    tail = labels[-6:]
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    ratio = changes / (len(tail) - 1)
    return all(tail[i] != tail[i - 1] for i in range(1, len(tail))), ratio


def volatility_score(labels: List[str]) -> float:
    tail = labels[-12:]
    if len(tail) < 8:
        return 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    return changes / (len(tail) - 1)


def entropy_score(labels: List[str]) -> float:
    tail = labels[-20:]
    if len(tail) < 8:
        return 0.0
    c = Counter(tail)
    total = sum(c.values())
    ent = 0.0
    for v in c.values():
        p = v / total
        ent -= p * math.log2(p)
    return ent / math.log2(2)


def long_bias(labels: List[str]) -> Tuple[str, int]:
    if len(labels) < 20:
        return "BALANCED", 0
    low = labels.count(LOW_LABEL)
    high = labels.count(HIGH_LABEL)
    gap = abs(low - high)
    if gap >= 20:
        return "IMBALANCED_STRONG", 3
    if gap >= 12:
        return "IMBALANCED_MEDIUM", 2
    if gap >= 8:
        return "IMBALANCED_LIGHT", 1
    return "BALANCED", 0


def detect_repeat_cycle(labels: List[str]) -> Tuple[Optional[int], float]:
    if len(labels) < 10:
        return None, 0.0

    h = labels[-min(MAX_CYCLE_SCAN, CYCLE_LOOKBACK):]
    best_cycle = None
    best_score = 0.0
    max_size = min(10, len(h) // 2)

    for size in range(2, max_size + 1):
        suffix = tuple(h[-size:])
        occur = 0
        next_counts = Counter()

        for i in range(len(h) - size):
            if tuple(h[i:i + size]) == suffix and i + size < len(h):
                occur += 1
                next_counts[h[i + size]] += 1

        if occur >= 2 and next_counts:
            _, cnt = next_counts.most_common(1)[0]
            score = min((cnt / occur) * 100, 90.0)
            if score > best_score:
                best_score = score
                best_cycle = size

    return best_cycle, best_score


def predict_cycle_next(labels: List[str], cycle_len: Optional[int]) -> Tuple[Optional[str], float]:
    if not cycle_len or cycle_len < 2 or len(labels) < cycle_len + 2:
        return None, 0.0

    suffix = tuple(labels[-cycle_len:])
    next_counts = Counter()
    total_occ = 0

    for i in range(len(labels) - cycle_len):
        if tuple(labels[i:i + cycle_len]) == suffix and i + cycle_len < len(labels):
            nxt = labels[i + cycle_len]
            if nxt in (LOW_LABEL, HIGH_LABEL):
                next_counts[nxt] += 1
                total_occ += 1

    if not next_counts or total_occ <= 0:
        return None, 0.0

    best, cnt = next_counts.most_common(1)[0]
    return best, min((cnt / total_occ) * 100.0, 90.0)


def summarize_patterns(d: Dict[str, Any], top_n: int = 3) -> List[Tuple[str, float]]:
    items = list(d.get("pattern_memory", {}).items())
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:top_n]


# =========================
# RECHECK LAYER
# =========================
def deep_recheck_new_rhythm(d: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    labels = d.get("labels", [])
    total = len(labels)

    if total < MIN_ANALYSIS_LEN:
        return {
            "enabled": False,
            "status": "DISABLED",
            "confidence": 0,
            "note": "Chưa đủ dữ liệu để recheck",
            "ghost_hit": False,
            "drift": 0.0,
            "micro_drift": 0.0,
            "recent_score": 0.0,
            "old_score": 0.0,
        }

    recent, mid, old = split_layers(labels)
    recent = recent[-max(SHORT_WINDOW, 10):] if recent else []
    micro = labels[-max(12, SHORT_WINDOW // 2):] if len(labels) >= 12 else labels[:]

    recent_ratio = layer_ratio(recent)
    old_ratio = layer_ratio(old) if old else layer_ratio(labels)
    micro_ratio = layer_ratio(micro)

    drift = abs(recent_ratio[LOW_LABEL] - old_ratio[LOW_LABEL])
    micro_drift = abs(micro_ratio[LOW_LABEL] - recent_ratio[LOW_LABEL])

    recent_entropy = entropy_score(recent)
    micro_entropy = entropy_score(micro)
    recent_vol = volatility_score(recent)
    micro_vol = volatility_score(micro)

    ghost_hit = False
    status = "RECHECK_CLEAN"
    note = "Nhịp mới đã tách khỏi dữ liệu cũ"

    if (
        drift >= 0.22
        and micro_drift >= 0.10
        and (recent_entropy >= 0.65 or micro_entropy >= 0.65)
        and (recent_vol >= 0.35 or micro_vol >= 0.35)
    ):
        ghost_hit = True
        status = "RECHECK_GHOST"
        note = "Nhịp mới vẫn bị ám bởi dữ liệu cũ"
    elif drift >= 0.16 and (recent_entropy >= 0.72 or micro_entropy >= 0.72):
        status = "RECHECK_SHIFT"
        note = "Đã đổi nhịp nhưng còn nhiễu"
    elif drift >= 0.12:
        status = "RECHECK_WATCH"
        note = "Có đổi nhịp nhẹ, cần theo dõi thêm"

    confidence = int(
        max(
            0,
            min(
                100,
                40
                + drift * 55
                + micro_drift * 25
                + max(recent_entropy, micro_entropy) * 12
                + max(recent_vol, micro_vol) * 8,
            ),
        )
    )

    d["recheck_count"] = int(d.get("recheck_count", 0)) + 1
    d["last_recheck_score"] = round(drift * 100.0, 2)
    d.setdefault("recheck_log", [])
    d["recheck_log"].append(status)
    d["recheck_log"] = _safe_tail(d["recheck_log"], 50)

    if ghost_hit:
        d["ghost_mode"] = True
        d["monitor_log"].append("recheck_ghost")
    elif status == "RECHECK_SHIFT":
        d["monitor_log"].append("recheck_shift")
    elif status == "RECHECK_WATCH":
        d["monitor_log"].append("recheck_watch")
    else:
        d["monitor_log"].append("recheck_clean")

    trim_state_memory(d)

    return {
        "enabled": True,
        "status": status,
        "confidence": confidence,
        "note": note,
        "ghost_hit": ghost_hit,
        "drift": drift,
        "micro_drift": micro_drift,
        "recent_score": recent_entropy,
        "old_score": entropy_score(old) if old else 0.0,
    }


# =========================
# ANALYSIS
# =========================
def detect_ghost_pressure(labels: List[str]) -> Dict[str, Any]:
    total = len(labels)
    recent, mid, old = split_layers(labels)

    if total < 12 or len(recent) < 8:
        return {
            "ghost_score": 0.0,
            "white_score": 0.0,
            "shift_score": 0.0,
            "white_type": "CHƯA RÕ CẦU TRẮNG",
            "white_detail": "Chưa đủ dữ liệu ngắn hạn",
            "recent_entropy": 0.0,
            "recent_volatility": 0.0,
            "old_bias": 0.0,
            "recent_bias": 0.0,
            "recent_low_ratio": 0.5,
            "old_low_ratio": 0.5,
            "freshness": 0.0,
            "mid_bias": 0.0,
            "drift_js": 0.0,
        }

    recent_ratio = layer_ratio(recent)
    mid_ratio = layer_ratio(mid)
    old_ratio = layer_ratio(old)

    recent_low_ratio = recent_ratio[LOW_LABEL]
    old_low_ratio = old_ratio[LOW_LABEL]

    recent_entropy = entropy_score(recent)
    recent_vol = volatility_score(recent)
    recent_bias = abs(recent_ratio[LOW_LABEL] - recent_ratio[HIGH_LABEL])
    old_bias = abs(old_ratio[LOW_LABEL] - old_ratio[HIGH_LABEL])
    mid_bias = abs(mid_ratio[LOW_LABEL] - mid_ratio[HIGH_LABEL]) if mid else 0.0

    shift_score = abs(recent_low_ratio - old_low_ratio)
    freshness = max(0.0, min(1.0, len(recent) / max(1, total)))
    drift_js = jensen_shannon_divergence(recent_ratio, old_ratio)

    ghost_score = (
        shift_score * 40.0
        + old_bias * 18.0
        + drift_js * 22.0
        + max(0.0, recent_entropy - 0.55) * 12.0
        + recent_vol * 8.0
    )
    ghost_score = max(0.0, min(100.0, ghost_score))

    if shift_score >= WHITE_SHIFT_THRESHOLD and old_bias >= 0.18:
        if recent_entropy >= 0.72:
            white_type = "CẦU TRẮNG BỊ ÁM"
            white_detail = "Nhịp mới đang bị dữ liệu cũ kéo lệch"
        else:
            white_type = "CẦU CHUYỂN PHA"
            white_detail = "Pha mới xuất hiện nhưng chưa khóa chắc"
    elif recent_entropy >= 0.82 and recent_vol >= 0.50 and old_bias >= 0.12:
        white_type = "CẦU TRẮNG KHÁNG ÁM"
        white_detail = "Dữ liệu gần nhất đủ sạch để ưu tiên"
    elif recent_entropy >= 0.72 and recent_vol >= 0.50:
        white_type = "CẦU TRẮNG"
        white_detail = "Tín hiệu ngắn hạn đang mở"
    elif drift_js >= 0.22:
        white_type = "CẦU CHUYỂN PHA"
        white_detail = "Có dấu hiệu đổi pha nhưng chưa rõ"
    else:
        white_type = "CHƯA RÕ CẦU TRẮNG"
        white_detail = "Chưa thấy tín hiệu trắng đủ mạnh"

    return {
        "ghost_score": ghost_score,
        "white_score": ghost_score,
        "shift_score": shift_score,
        "white_type": white_type,
        "white_detail": white_detail,
        "recent_entropy": recent_entropy,
        "recent_volatility": recent_vol,
        "old_bias": old_bias,
        "recent_bias": recent_bias,
        "recent_low_ratio": recent_low_ratio,
        "old_low_ratio": old_low_ratio,
        "freshness": freshness,
        "mid_bias": mid_bias,
        "drift_js": drift_js,
    }


def detect_cau_structure(report: Dict[str, Any]) -> Tuple[str, str]:
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    shift_score = float(report.get("shift_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))

    if alt and alt_ratio >= 0.85:
        return "CẦU XEN KẼ", "Chuỗi đổi nhịp liên tục"
    if streak >= 5 and last_label in (LOW_LABEL, HIGH_LABEL):
        return "CẦU BỆT", f"{last_label} x{streak}"
    if cycle_score >= 70 and cycle_len:
        return "CẦU LẶP", f"Chu kỳ {cycle_len}"
    if shift_score >= WHITE_SHIFT_THRESHOLD:
        return "CẦU CHUYỂN PHA", "Nhịp dữ liệu đang đổi"
    if ghost_score >= GHOST_SOFT_CLEAN:
        return "CẦU TRẮNG", "Dữ liệu gần đây đang chiếm ưu thế"
    if vol >= 0.70:
        return "CẦU RUNG", "Dao động mạnh"
    if ent < 0.65:
        return "CẦU ỔN ĐỊNH", "Biến động thấp"
    if ent >= 0.80 and vol >= 0.55:
        return "CẦU HỖN LOẠN", "Nhiễu tương đối cao"
    if len(report.get("patterns", [])) > 0:
        return "CẦU ĐUÔI MỚI", "Có mẫu lặp gần đây"
    return "CHƯA RÕ CẦU", "Tín hiệu chưa đủ mạnh"


def detect_mode(d: Dict[str, Any], report: Dict[str, Any]) -> Tuple[str, str]:
    labels = d.get("labels", [])
    if len(labels) < 10:
        return "WARMUP", "Đang học dữ liệu"

    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    white_score = float(report.get("white_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    low = report.get("low", 0)
    high = report.get("high", 0)
    gap = abs(low - high)

    if white_type in ("CẦU TRẮNG BỊ ÁM", "CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA") and white_score >= WHITE_MIN_SCORE:
        if white_type == "CẦU TRẮNG BỊ ÁM":
            return "ANTI_GHOST", "Cầu mới đang bị dữ liệu cũ ám"
        if white_type == "CẦU TRẮNG KHÁNG ÁM":
            return "WHITE_CLEAN", "Cầu mới đã tách khỏi ám cũ"
        return "WHITE_SHIFT", "Đang hình thành cầu mới"

    if ghost_score >= GHOST_HARD_CLEAN:
        return "ANTI_GHOST", "Ám lịch sử rất mạnh"
    if ghost_score >= GHOST_SOFT_CLEAN and shift_score >= DRIFT_MIN_SHIFT:
        return "ANTI_GHOST", "Đang có độ lệch lịch sử"
    if alt and alt_ratio >= 1.0:
        return "ALT", "Xen kẽ mạnh"
    if streak >= 6:
        return "STREAK", f"Chuỗi dài ({last_label} x{streak})"
    if cycle_score >= 70 and cycle_len:
        return "CYCLE", f"Vòng lặp rõ (size {cycle_len})"
    if gap >= 20:
        return "BIAS_STRONG", "Lệch tổng mạnh"
    if gap >= 12:
        return "BIAS_MEDIUM", "Lệch tổng vừa"
    if vol >= 0.85 and ent >= 0.85:
        return "NOISY", "Nhiễu cao"
    if len(labels) >= 20 and abs(low - high) <= 2:
        return "BALANCED", "Cân bằng"
    return "NORMAL", "Ổn định"


def analyze_sequence(d: Dict[str, Any]) -> Dict[str, Any]:
    repair_state(d)
    labels = d.get("labels", [])
    total = len(labels)
    low = labels.count(LOW_LABEL)
    high = labels.count(HIGH_LABEL)

    last_label, streak = current_streak(labels)
    alt, alt_ratio = alternating_analysis(labels)
    vol = volatility_score(labels)
    ent = entropy_score(labels)
    cycle_len, cycle_score = detect_repeat_cycle(labels)
    cycle_consistency = cycle_score

    ghost_info = detect_ghost_pressure(labels)
    white_type = ghost_info.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    white_detail = ghost_info.get("white_detail", "")
    ghost_score = float(ghost_info.get("ghost_score", 0.0))
    shift_score = float(ghost_info.get("shift_score", 0.0))
    white_score = float(ghost_info.get("white_score", ghost_score))

    cau_type, cau_detail = detect_cau_structure(
        {
            "last_label": last_label,
            "streak": streak,
            "alternating": alt,
            "alt_ratio": alt_ratio,
            "volatility": vol,
            "entropy": ent,
            "cycle_score": cycle_score,
            "cycle_len": cycle_len,
            "shift_score": shift_score,
            "ghost_score": ghost_score,
            "low": low,
            "high": high,
            "patterns": summarize_patterns(d, top_n=5),
        }
    )

    mode, note = detect_mode(
        d,
        {
            "white_type": white_type,
            "white_score": white_score,
            "ghost_score": ghost_score,
            "shift_score": shift_score,
            "volatility": vol,
            "entropy": ent,
            "alternating": alt,
            "alt_ratio": alt_ratio,
            "cycle_score": cycle_score,
            "cycle_len": cycle_len,
            "last_label": last_label,
            "streak": streak,
            "low": low,
            "high": high,
        },
    )

    long_mode, long_score = long_bias(labels)

    if white_type in ("CẦU TRẮNG BỊ ÁM", "CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA") and white_score >= WHITE_MIN_SCORE:
        cau_type = white_type
        cau_detail = white_detail

    d["mode"] = mode

    return {
        "mode": mode,
        "note": note,
        "total": total,
        "low": low,
        "high": high,
        "last_label": last_label,
        "streak": streak,
        "alternating": alt,
        "alt_ratio": alt_ratio,
        "volatility": vol,
        "entropy": ent,
        "cycle_len": cycle_len,
        "cycle_score": cycle_score,
        "cycle_consistency": cycle_consistency,
        "long_mode": long_mode,
        "long_score": long_score,
        "cau_type": cau_type,
        "cau_detail": cau_detail,
        "white_type": white_type,
        "white_detail": white_detail,
        "white_score": white_score,
        "ghost_score": ghost_score,
        "shift_score": shift_score,
        "recent_entropy": ghost_info.get("recent_entropy", 0.0),
        "recent_volatility": ghost_info.get("recent_volatility", 0.0),
        "old_bias": ghost_info.get("old_bias", 0.0),
        "recent_bias": ghost_info.get("recent_bias", 0.0),
        "recent_low_ratio": ghost_info.get("recent_low_ratio", 0.5),
        "old_low_ratio": ghost_info.get("old_low_ratio", 0.5),
        "freshness": ghost_info.get("freshness", 0.0),
        "mid_bias": ghost_info.get("mid_bias", 0.0),
        "drift_js": ghost_info.get("drift_js", 0.0),
        "patterns": summarize_patterns(d, top_n=5),
    }


# =========================
# AI ENGINES
# =========================
def weighted_label_probs(labels: List[str], window: int, decay: float) -> Dict[str, float]:
    if not labels:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    tail = labels[-window:]
    scores = {LOW_LABEL: 0.0, HIGH_LABEL: 0.0}
    for i, lb in enumerate(reversed(tail)):
        w = decay ** i
        if lb in scores:
            scores[lb] += w
    return normalize_probs(scores)


def global_label_probs(labels: List[str]) -> Dict[str, float]:
    tail = labels[-MAX_LABELS_STORE:] if len(labels) > MAX_LABELS_STORE else labels
    return layer_ratio(tail)


def markov_next_probs(labels: List[str]) -> Dict[str, float]:
    if len(labels) < 2:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    source = labels[-min(MARKOV_LOOKBACK, len(labels)):]
    if len(source) < 2:
        return global_label_probs(source)

    trans = {LOW_LABEL: Counter(), HIGH_LABEL: Counter()}
    for a, b in zip(source[:-1], source[1:]):
        if a in trans and b in trans:
            trans[a][b] += 1

    last = source[-1]
    if last not in trans or not trans[last]:
        return global_label_probs(source)

    total = sum(trans[last].values())
    if total <= 0:
        return global_label_probs(source)

    return {
        LOW_LABEL: trans[last][LOW_LABEL] / total,
        HIGH_LABEL: trans[last][HIGH_LABEL] / total,
    }


def ai_new_rhythm(chat_id: int, report: Dict[str, Any], labels: List[str]) -> Dict[str, Any]:
    recent_labels, mid_labels, old_labels = split_layers(labels)

    recent_probs = weighted_label_probs(recent_labels, window=max(8, min(RECENT_WINDOW, len(recent_labels))), decay=0.89)
    mid_probs = weighted_label_probs(mid_labels, window=max(8, min(MID_WINDOW, len(mid_labels))), decay=0.95)
    markov_probs = markov_next_probs(recent_labels if recent_labels else labels)
    global_probs = global_label_probs(labels)

    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    cycle_consistency = float(report.get("cycle_consistency", cycle_score))
    drift_js = float(report.get("drift_js", 0.0))

    recent_ratio = layer_ratio(recent_labels)
    old_ratio = layer_ratio(old_labels) if old_labels else global_label_probs(labels)
    shift = abs(recent_ratio[LOW_LABEL] - old_ratio[LOW_LABEL])

    scores = {
        LOW_LABEL: recent_probs[LOW_LABEL] * 0.56 + mid_probs[LOW_LABEL] * 0.14 + markov_probs[LOW_LABEL] * 0.20 + global_probs[LOW_LABEL] * 0.10,
        HIGH_LABEL: recent_probs[HIGH_LABEL] * 0.56 + mid_probs[HIGH_LABEL] * 0.14 + markov_probs[HIGH_LABEL] * 0.20 + global_probs[HIGH_LABEL] * 0.10,
    }

    if streak >= 4 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] += 0.10
    if alt and alt_ratio >= 0.80 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] += 0.10
    if cycle_score >= 60 and cycle_len:
        cycle_pred, conf = predict_cycle_next(labels, cycle_len)
        if cycle_pred in (LOW_LABEL, HIGH_LABEL):
            boost = 0.12
            if cycle_consistency < 60:
                boost = 0.06
            if cycle_consistency < 45:
                boost = 0.03
            scores[cycle_pred] += conf / 100.0 * boost
    if vol >= 0.65:
        scores[LOW_LABEL] += 0.02
        scores[HIGH_LABEL] += 0.02
    if drift_js >= 0.20:
        scores[LOW_LABEL] += 0.01
        scores[HIGH_LABEL] += 0.01

    probs = normalize_probs(scores)
    best = max(probs, key=probs.get)
    delta = abs(probs[LOW_LABEL] - probs[HIGH_LABEL])

    confidence = int(
        max(
            0,
            min(
                100,
                42
                + delta * 42
                + (8 if len(labels) >= 30 else 4 if len(labels) >= 15 else 0)
                + (4 if shift >= DRIFT_MIN_SHIFT else 0)
                + (3 if ent >= 0.70 else 0),
            ),
        )
    )

    return {
        "engine": "NEW_RHYTHM",
        "status": "NHỊP MỚI",
        "best_label": best,
        "confidence": confidence,
        "probs": probs,
        "shift": shift,
        "note": "Bám nhịp mới và ưu tiên dữ liệu gần nhất",
    }


def anti_ghost_weights(report: Dict[str, Any], labels: List[str]) -> Tuple[float, float, float, float]:
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    recent_entropy = float(report.get("recent_entropy", 0.0))
    recent_volatility = float(report.get("recent_volatility", 0.0))
    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")

    rec_w = 0.64
    mid_w = 0.20
    old_w = 0.04
    markov_w = 0.12

    if white_type == "CẦU TRẮNG KHÁNG ÁM":
        rec_w, mid_w, old_w, markov_w = 0.74, 0.16, 0.00, 0.10
    elif white_type == "CẦU TRẮNG BỊ ÁM":
        rec_w, mid_w, old_w, markov_w = 0.82, 0.10, 0.00, 0.08
    elif white_type == "CẦU CHUYỂN PHA":
        rec_w, mid_w, old_w, markov_w = 0.80, 0.12, 0.00, 0.08
    elif white_type == "CẦU TRẮNG":
        rec_w, mid_w, old_w, markov_w = 0.70, 0.18, 0.02, 0.10

    if ghost_score >= 80 or shift_score >= DRIFT_STRONG_SHIFT:
        rec_w += 0.12
        mid_w += 0.02
        old_w = 0.0
        markov_w = max(0.05, markov_w - 0.08)
    elif ghost_score >= 65 or shift_score >= DRIFT_MIN_SHIFT:
        rec_w += 0.08
        mid_w += 0.01
        old_w = max(0.0, old_w - 0.03)

    if recent_entropy >= 0.75 or recent_volatility >= 0.50:
        rec_w += 0.04
        old_w = max(0.0, old_w - 0.02)

    if len(labels) >= MAX_LABELS_STORE * 0.8:
        old_w *= 0.75

    total_w = rec_w + mid_w + old_w + markov_w
    return rec_w / total_w, mid_w / total_w, old_w / total_w, markov_w / total_w


def ai_anti_ghost(chat_id: int, report: Dict[str, Any], labels: List[str]) -> Dict[str, Any]:
    recent_labels, mid_labels, old_labels = split_layers(labels)
    recent_probs = weighted_label_probs(recent_labels, window=max(8, min(RECENT_WINDOW, len(recent_labels))), decay=0.91)
    mid_probs = weighted_label_probs(mid_labels, window=max(8, min(MID_WINDOW, len(mid_labels))), decay=0.96)
    old_probs = global_label_probs(old_labels) if old_labels else global_label_probs(labels)
    global_probs = global_label_probs(labels)
    markov_probs = markov_next_probs(labels)

    rec_w, mid_w, old_w, markov_w = anti_ghost_weights(report, labels)

    scores = {
        LOW_LABEL: recent_probs[LOW_LABEL] * rec_w + mid_probs[LOW_LABEL] * mid_w + old_probs[LOW_LABEL] * old_w + markov_probs[LOW_LABEL] * markov_w + global_probs[LOW_LABEL] * 0.02,
        HIGH_LABEL: recent_probs[HIGH_LABEL] * rec_w + mid_probs[HIGH_LABEL] * mid_w + old_probs[HIGH_LABEL] * old_w + markov_probs[HIGH_LABEL] * markov_w + global_probs[HIGH_LABEL] * 0.02,
    }

    cau_type = report.get("cau_type", "CHƯA RÕ CẦU")
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")

    cycle_pred, cycle_pred_conf = predict_cycle_next(labels, cycle_len if cycle_score >= 70 else None)

    if cau_type == "CẦU XEN KẼ" and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] *= 1.26
        scores[other] += 0.16
    elif cau_type == "CẦU BỆT" and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] *= 1.28
        scores[other] += 0.18
    elif cau_type == "CẦU LẶP" and cycle_pred in (LOW_LABEL, HIGH_LABEL):
        scores[cycle_pred] *= 1.32
        scores[cycle_pred] += (cycle_pred_conf / 100.0) * 0.14
    elif cau_type == "CẦU RUNG":
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.98 + 0.01
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.98 + 0.01
    elif cau_type == "CẦU ỔN ĐỊNH":
        scores[LOW_LABEL] *= 1.02
        scores[HIGH_LABEL] *= 1.02

    if streak >= 5 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] *= 1.08

    if alt and alt_ratio >= 0.90 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] *= 1.04

    if report.get("mode") == "NOISY" or (vol >= 0.85 and ent >= 0.85):
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.56 + 0.22
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.56 + 0.22

    if report.get("white_type") == "CẦU TRẮNG BỊ ÁM":
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.96 + 0.02
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.96 + 0.02

    probs = normalize_probs(scores)
    best_label = max(probs, key=probs.get)
    delta = abs(probs[LOW_LABEL] - probs[HIGH_LABEL])

    quality = 0
    total = int(report.get("total", 0))
    if total >= 30:
        quality += 8
    elif total >= 15:
        quality += 4

    mode = report.get("mode", "NORMAL")
    if mode in ("STREAK", "CYCLE"):
        quality += 7
    elif mode in ("BALANCED", "NORMAL"):
        quality += 2
    elif mode == "NOISY":
        quality -= 10

    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    white_score = float(report.get("white_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))

    if white_type in ("CẦU TRẮNG KHÁNG ÁM", "CẦU TRẮNG BỊ ÁM", "CẦU CHUYỂN PHA"):
        quality += 5
        if white_score >= 75:
            quality += 2

    if ghost_score >= 80:
        quality += 5
    elif ghost_score >= 65:
        quality += 3
    elif shift_score >= DRIFT_MIN_SHIFT:
        quality += 2

    confidence = int(max(0, min(100, 48 + delta * 45 + quality)))

    if total < MIN_ANALYSIS_LEN:
        return {
            "engine": "ANTI_GHOST",
            "status": "WARMUP",
            "confidence": 0,
            "message": "Chưa đủ dữ liệu.",
            "best_label": None,
            "other_label": None,
            "probs": {LOW_LABEL: 0.5, HIGH_LABEL: 0.5},
            "cycle_pred": cycle_pred,
            "cycle_pred_conf": cycle_pred_conf,
        }

    if confidence < MIN_CONFIDENCE:
        return {
            "engine": "ANTI_GHOST",
            "status": "UNCERTAIN",
            "confidence": confidence,
            "message": "Tín hiệu còn yếu.",
            "best_label": best_label,
            "other_label": HIGH_LABEL if best_label == LOW_LABEL else LOW_LABEL,
            "probs": probs,
            "cycle_pred": cycle_pred,
            "cycle_pred_conf": cycle_pred_conf,
        }

    status = "TRENDING" if delta >= 0.08 else "BALANCED"
    message = "Hai phía khá cân bằng." if status == "BALANCED" else f"Xu hướng theo cầu nghiêng về {best_label}."

    if white_type == "CẦU TRẮNG KHÁNG ÁM":
        status = "WHITE_CLEAN"
        message = "Cầu trắng sạch, ưu tiên tín hiệu gần nhất."
    elif white_type == "CẦU TRẮNG BỊ ÁM":
        status = "WHITE_GHOST"
        message = "Cầu mới có dấu hiệu bị dữ liệu cũ ám."
    elif white_type == "CẦU CHUYỂN PHA":
        status = "WHITE_SHIFT"
        message = "Đang chuyển pha, chỉ đọc đuôi gần nhất."

    if streak >= 6:
        status = "STREAK"
        message = f"Chuỗi hiện tại rất mạnh: {last_label} x{streak}."

    if cycle_score >= 70 and cycle_len:
        status = "CYCLE"
        message = f"Phát hiện vòng lặp khá rõ (size {cycle_len})."

    return {
        "engine": "ANTI_GHOST",
        "status": status,
        "confidence": confidence,
        "message": message,
        "best_label": best_label,
        "other_label": HIGH_LABEL if best_label == LOW_LABEL else LOW_LABEL,
        "probs": probs,
        "cycle_pred": cycle_pred,
        "cycle_pred_conf": cycle_pred_conf,
    }


def monitor_ai(
    chat_id: int,
    d: Dict[str, Any],
    report: Dict[str, Any],
    rhythm_ai: Dict[str, Any],
    ghost_ai: Dict[str, Any],
    recheck: Dict[str, Any],
) -> Dict[str, Any]:
    labels = d.get("labels", [])
    total = len(labels)
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    freshness = float(report.get("freshness", 0.0))
    mode = report.get("mode", "NORMAL")
    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    recent_entropy = float(report.get("recent_entropy", 0.0))
    recent_vol = float(report.get("recent_volatility", 0.0))
    drift_js = float(report.get("drift_js", 0.0))
    recheck_status = recheck.get("status", "DISABLED")
    recheck_conf = int(recheck.get("confidence", 0))

    stability = 100.0
    stability -= ghost_score * 0.50
    stability -= shift_score * 35.0
    stability -= drift_js * 20.0
    stability += freshness * 10.0
    stability += 5.0 if mode in ("NORMAL", "BALANCED", "WHITE_CLEAN") else 0.0
    stability -= 10.0 if mode == "NOISY" else 0.0

    if recheck_status == "RECHECK_GHOST":
        stability -= 12.0
    elif recheck_status == "RECHECK_SHIFT":
        stability -= 4.0
    elif recheck_status == "RECHECK_CLEAN":
        stability += 4.0

    stability = max(0.0, min(100.0, stability))

    actions: List[str] = []
    if recheck_status == "RECHECK_GHOST":
        actions.append("nhịp mới còn bị ám cũ")
        actions.append("ưu tiên đuôi gần nhất")
    elif recheck_status == "RECHECK_SHIFT":
        actions.append("đã đổi nhịp, còn nhiễu")
    elif recheck_status == "RECHECK_CLEAN":
        actions.append("nhịp mới đã sạch hơn")

    if ghost_score >= GHOST_HARD_CLEAN:
        actions.append("xóa mẫu cũ")
        actions.append("giữ đuôi mới")
    elif ghost_score >= GHOST_SOFT_CLEAN:
        actions.append("giảm trọng số lịch sử")
    elif ghost_score >= GHOST_WARN:
        actions.append("theo dõi ám cũ")

    if freshness >= 0.60:
        actions.append("ưu tiên dữ liệu mới")
    if recent_entropy >= 0.75 or recent_vol >= 0.50:
        actions.append("đọc nhịp gần nhất")
    if white_type in ("CẦU TRẮNG BỊ ÁM", "CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA"):
        actions.append("khóa theo cầu trắng")
    if total < MIN_ANALYSIS_LEN:
        actions.append("chưa đủ dữ liệu")

    if not actions:
        actions.append("ổn định")

    if ghost_score >= GHOST_HARD_CLEAN:
        d["ghost_mode"] = True
        d["monitor_log"].append("monitor_hard_fix")
        d["health_log"].append(0)
        d["pattern_memory"].clear()
        d["labels"] = _safe_tail(d["labels"], max(120, RECENT_WINDOW))
        d["values"] = _safe_tail(d["values"], max(120, RECENT_WINDOW))
        d["history"] = _safe_tail(d["history"], 50)
        rebuild_counters(d)
    elif ghost_score >= GHOST_SOFT_CLEAN:
        d["ghost_mode"] = True
        d["monitor_log"].append("monitor_soft_fix")
    else:
        d["ghost_mode"] = False
        d["monitor_log"].append("monitor_ok")

    d["stability_score"] = stability
    d["last_clean_score"] = max(float(d.get("last_clean_score", 0.0)), ghost_score)
    d["last_recheck_score"] = max(float(d.get("last_recheck_score", 0.0)), float(recheck.get("drift", 0.0)) * 100.0)

    rhythm_conf = int(rhythm_ai.get("confidence", 0))
    ghost_conf = int(ghost_ai.get("confidence", 0))
    if rhythm_conf < 30 and ghost_conf < 30:
        d["error_count"] = int(d.get("error_count", 0)) + 1
    else:
        d["error_count"] = max(0, int(d.get("error_count", 0)) - 1)

    severity = "LOW"
    if stability < 45:
        severity = "HIGH"
    elif stability < 70:
        severity = "MEDIUM"

    return {
        "stability": stability,
        "severity": severity,
        "actions": actions,
        "ghost_mode": bool(d.get("ghost_mode", False)),
        "error_count": int(d.get("error_count", 0)),
        "recheck_status": recheck_status,
        "recheck_confidence": recheck_conf,
    }


def final_decision(
    report: Dict[str, Any],
    rhythm_ai: Dict[str, Any],
    ghost_ai: Dict[str, Any],
    recheck: Dict[str, Any],
    monitor: Dict[str, Any],
    d: Dict[str, Any],
) -> Dict[str, Any]:
    r_probs = normalize_probs(dict(rhythm_ai.get("probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})))
    g_probs = normalize_probs(dict(ghost_ai.get("probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})))

    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    freshness = float(report.get("freshness", 0.0))
    mode = report.get("mode", "NORMAL")
    stability = float(monitor.get("stability", 100.0))
    recheck_status = recheck.get("status", "DISABLED")
    recheck_conf = int(recheck.get("confidence", 0))
    ghost_hit = bool(recheck.get("ghost_hit", False))

    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    cycle_consistency = float(report.get("cycle_consistency", cycle_score))

    if recheck_status == "RECHECK_GHOST":
        w_r, w_g = 0.38, 0.62
    elif recheck_status == "RECHECK_SHIFT":
        w_r, w_g = 0.66, 0.34
    elif white_type in ("CẦU TRẮNG KHÁNG ÁM", "CẦU TRẮNG BỊ ÁM", "CẦU CHUYỂN PHA") or ghost_score >= 65 or shift_score >= DRIFT_MIN_SHIFT:
        w_r, w_g = 0.72, 0.28
    elif mode == "NOISY":
        w_r, w_g = 0.55, 0.45
    else:
        w_r, w_g = 0.50, 0.50

    if freshness >= 0.70:
        w_r += 0.05
        w_g -= 0.05

    recent_labels = d.get("labels", [])[-20:]
    recent_low = recent_labels.count(LOW_LABEL)
    recent_high = recent_labels.count(HIGH_LABEL)
    history_gap = abs(recent_low - recent_high)
    if history_gap >= 6:
        if recent_low > recent_high:
            w_r -= 0.02
            w_g += 0.02
        else:
            w_r += 0.02
            w_g -= 0.02

    scores = {
        LOW_LABEL: r_probs[LOW_LABEL] * w_r + g_probs[LOW_LABEL] * w_g,
        HIGH_LABEL: r_probs[HIGH_LABEL] * w_r + g_probs[HIGH_LABEL] * w_g,
    }

    if ghost_hit:
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.95 + 0.025
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.95 + 0.025

    if stability < 45:
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.96 + 0.02
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.96 + 0.02

    probs = normalize_probs(scores)
    diff = abs(probs[LOW_LABEL] - probs[HIGH_LABEL])

    if diff < 0.025:
        recent_low_all = d.get("labels", []).count(LOW_LABEL)
        recent_high_all = d.get("labels", []).count(HIGH_LABEL)
        if recent_low_all == recent_high_all:
            last_label = report.get("last_label")
            if last_label == LOW_LABEL and report.get("streak", 0) >= 2:
                best_label = HIGH_LABEL
            elif last_label == HIGH_LABEL and report.get("streak", 0) >= 2:
                best_label = LOW_LABEL
            else:
                if report.get("cycle_len") and report.get("cycle_consistency", 100.0) < 60:
                    best_label = HIGH_LABEL if report.get("cycle_score", 0.0) >= 70 else LOW_LABEL
                else:
                    best_label = HIGH_LABEL if report.get("volatility", 0.0) >= 0.45 else LOW_LABEL
        elif recent_low_all > recent_high_all:
            best_label = HIGH_LABEL
        else:
            best_label = LOW_LABEL
    else:
        if probs[HIGH_LABEL] > probs[LOW_LABEL]:
            best_label = HIGH_LABEL
        else:
            best_label = LOW_LABEL

    other_label = HIGH_LABEL if best_label == LOW_LABEL else LOW_LABEL
    delta = abs(probs[LOW_LABEL] - probs[HIGH_LABEL])

    confidence = int(
        max(
            0,
            min(
                100,
                (rhythm_ai.get("confidence", 0) * 0.38)
                + (ghost_ai.get("confidence", 0) * 0.38)
                + (recheck_conf * 0.16)
                + delta * 28
                + (5 if stability >= 70 else 0)
                - (8 if stability < 45 else 0),
            ),
        )
    )

    if diff < 0.03:
        confidence = min(confidence, 68)
    if cycle_score >= 70 and cycle_len and cycle_consistency < 60:
        confidence = min(confidence, 67)

    return {
        "final_label": best_label,
        "other_label": other_label,
        "confidence": confidence,
        "final_probs": probs,
        "recheck_status": recheck_status,
        "recheck_confidence": recheck_conf,
        "source": "FINAL_LOCK",
    }


# =========================
# UI
# =========================
def build_stats_message(report: Dict[str, Any], d: Dict[str, Any]) -> str:
    patterns = format_pattern_lines(report.get("patterns", []))
    hist = format_history(d.get("labels", []))
    low_p = (report["low"] / report["total"] * 100.0) if report["total"] else 0.0
    high_p = (report["high"] / report["total"] * 100.0) if report["total"] else 0.0
    labels = d.get("labels", [])
    rec = layer_ratio(labels[-RECENT_WINDOW:])
    old = layer_ratio(labels[:-RECENT_WINDOW] if len(labels) > RECENT_WINDOW else [])

    return (
        "╔════════════════════════════╗\n"
        "║        📊 BẢNG CẦU         ║\n"
        "╠════════════════════════════╣\n"
        f"║ {LOW_LABEL}: {report['low']} ({low_p:.1f}%)\n"
        f"║ {HIGH_LABEL}: {report['high']} ({high_p:.1f}%)\n"
        f"║ Tổng: {report['total']} | Mode: {report['mode']}\n"
        f"║ Ghost: {'ON' if d.get('ghost_mode') else 'OFF'} | Cleanups: {d.get('cleanups', 0)}\n"
        f"║ Stable: {d.get('stability_score', 100.0):.1f} | Errors: {d.get('error_count', 0)}\n"
        f"║ Recheck: {d.get('recheck_count', 0)} | Last: {d.get('last_recheck_score', 0.0):.1f}\n"
        "╠════════════════════════════╣\n"
        f"║ Cầu chính: {report.get('cau_type', '-')}\n"
        f"║ Chi tiết : {report.get('cau_detail', '-')}\n"
        f"║ Cầu trắng: {report.get('white_type', '-')} | {report.get('white_score', 0):.0f}\n"
        f"║ Ghost    : {report.get('ghost_score', 0.0):.0f} | Drift {report.get('drift_js', 0.0):.2f}\n"
        f"║ Ám cũ    : {report.get('shift_score', 0.0):.2f}\n"
        f"║ Fresh    : {report.get('freshness', 0.0):.2f}\n"
        f"║ Recent   : {rec.get(LOW_LABEL, 0.5)*100:.1f}% / {rec.get(HIGH_LABEL, 0.5)*100:.1f}%\n"
        f"║ Old      : {old.get(LOW_LABEL, 0.5)*100:.1f}% / {old.get(HIGH_LABEL, 0.5)*100:.1f}%\n"
        f"║ Xen kẽ   : {'Có' if report['alternating'] else 'Không'} | {report['alt_ratio']:.2f}\n"
        f"║ Bệt      : {report['last_label'] if report['last_label'] else '—'} x{report['streak']}\n"
        f"║ Lặp      : {report['cycle_len'] if report['cycle_len'] else '—'} | {report['cycle_score']:.0f}\n"
        f"║ Rung     : {report['volatility']:.2f}\n"
        f"║ Ổn định  : {report['entropy']:.2f}\n"
        f"║ Long     : {report.get('long_mode', '-')}\n"
        f"║ Pattern  : {patterns}\n"
        f"║ Lịch sử  : {hist}\n"
        "╚════════════════════════════╝"
    )


def build_loading_message(
    report: Dict[str, Any],
    rhythm_ai: Dict[str, Any],
    ghost_ai: Dict[str, Any],
    monitor: Dict[str, Any],
    recheck: Dict[str, Any],
) -> str:
    return (
        "╔════════════════════════════╗\n"
        "║      ⏳ ĐANG PHÂN TÍCH      ║\n"
        "╠════════════════════════════╣\n"
        f"║ Cầu: {report.get('cau_type', '-')}\n"
        f"║ Trắng: {report.get('white_type', '-')}\n"
        f"║ Trạng thái: {report.get('mode', '-')}\n"
        f"║ RECHECK: {recheck.get('status', '-') } | {recheck.get('confidence', 0)}%\n"
        f"║ AI mới: {rhythm_ai.get('status', rhythm_ai.get('engine', '-'))} | {rhythm_ai.get('confidence', 0)}%\n"
        f"║ AI ám : {ghost_ai.get('status', ghost_ai.get('engine', '-'))} | {ghost_ai.get('confidence', 0)}%\n"
        f"║ Giám sát: {monitor.get('severity', '-')} | {monitor.get('stability', 0):.1f}\n"
        "║ Hệ thống đang khóa tín hiệu cũ và kiểm tra nhịp mới...\n"
        "╚════════════════════════════╝"
    )


def build_analysis_message(
    report: Dict[str, Any],
    rhythm_ai: Dict[str, Any],
    ghost_ai: Dict[str, Any],
    meta: Dict[str, Any],
    monitor: Dict[str, Any],
    recheck: Dict[str, Any],
) -> str:
    return (
        "╔════════════════════════════╗\n"
        "║       🔍 PHÂN TÍCH         ║\n"
        "╠════════════════════════════╣\n"
        f"║ Dựa trên: {report.get('cau_type', '-')}\n"
        f"║ {report.get('cau_detail', '-')}\n"
        f"║ Trắng   : {report.get('white_type', '-')} | {report.get('white_score', 0):.0f}\n"
        f"║ Ghost   : {report.get('ghost_score', 0.0):.0f}\n"
        f"║ Drift   : {report.get('drift_js', 0.0):.2f}\n"
        f"║ RECHECK : {recheck.get('status', '-') } | {recheck.get('confidence', 0)}%\n"
        f"║ RE-DRFT : {recheck.get('drift', 0.0):.2f}\n"
        f"║ AI mới  : {rhythm_ai.get('status', '-') } | {rhythm_ai.get('confidence', 0)}%\n"
        f"║ PX mới  : {format_prob_inline(rhythm_ai.get('probs', {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}))}\n"
        f"║ AI ám   : {ghost_ai.get('status', '-') } | {ghost_ai.get('confidence', 0)}%\n"
        f"║ PX ám   : {format_prob_inline(ghost_ai.get('probs', {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}))}\n"
        f"║ Giám sát: {monitor.get('severity', '-') } | {monitor.get('stability', 0):.1f}\n"
        f"║ Chốt    : {meta.get('final_label', '-')} | {meta.get('confidence', 0)}%\n"
        f"║ Phụ     : {meta.get('other_label', '-')}\n"
        f"║ PX chốt : {format_prob_inline(meta.get('final_probs', {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}))}\n"
        "╚════════════════════════════╝"
    )


def build_final_message(meta: Dict[str, Any]) -> str:
    label = meta.get("final_label", "-")
    conf = meta.get("confidence", 0)
    return f"DỰ ĐOÁN: {label}\nTỶ LỆ: {conf}%"


# =========================
# CLEANUP / PRED TASK
# =========================
def anti_ghost_cleanup(d: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    ghost_score = float(report.get("ghost_score", 0.0))
    pm = d.get("pattern_memory", {})
    result = {
        "cleaned": False,
        "level": "none",
        "ghost_score": ghost_score,
        "message": "",
    }

    d["last_clean_score"] = ghost_score

    if ghost_score >= GHOST_HARD_CLEAN:
        d["ghost_mode"] = True
        pm.clear()
        d["cleanups"] = int(d.get("cleanups", 0)) + 1
        d["health_log"].append(0)
        d["monitor_log"].append("hard_clean")
        d["labels"] = _safe_tail(d["labels"], max(180, RECENT_WINDOW * 2))
        d["values"] = _safe_tail(d["values"], max(180, RECENT_WINDOW * 2))
        d["history"] = _safe_tail(d["history"], 50)
        rebuild_counters(d)
        trim_state_memory(d)
        result.update(
            {
                "cleaned": True,
                "level": "hard",
                "message": f"🧹 Tự động dọn ám cũ mức {ghost_score:.0f}%. Hệ thống đã ưu tiên nhịp mới và xóa mẫu cũ.",
            }
        )
        return result

    if ghost_score >= GHOST_SOFT_CLEAN:
        d["ghost_mode"] = True
        factor = 0.35
        d["cleanups"] = int(d.get("cleanups", 0)) + 1
        d["monitor_log"].append("soft_clean")
        for k in list(pm.keys()):
            pm[k] *= factor
            if pm[k] < 0.05:
                del pm[k]
        trim_pattern_memory(d)
        result.update(
            {
                "cleaned": True,
                "level": "soft",
                "message": f"🧹 Tự động giảm ảnh hưởng lịch sử cũ, mức ám {ghost_score:.0f}%. Bot đang bám nhịp mới.",
            }
        )
        return result

    if ghost_score >= GHOST_WARN:
        d["ghost_mode"] = True
        factor = 0.65
        d["monitor_log"].append("warn_clean")
        for k in list(pm.keys()):
            pm[k] *= factor
            if pm[k] < 0.05:
                del pm[k]
        trim_pattern_memory(d)
        result.update(
            {
                "cleaned": True,
                "level": "warn",
                "message": f"⚠️ Bot vừa tự làm nhẹ dữ liệu cũ, mức ám {ghost_score:.0f}%. Đang chuyển sang ưu tiên tín hiệu gần nhất.",
            }
        )
        return result

    d["ghost_mode"] = False
    d["monitor_log"].append("stable")
    return result


# =========================
# TASK CONTROL
# =========================
def cancel_pending_analysis(chat_id: int) -> None:
    task = analysis_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def cancel_all_pending() -> None:
    for chat_id in list(analysis_tasks.keys()):
        task = analysis_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()


async def delayed_analysis_job(chat_id: int, version: int, loading_msg) -> None:
    try:
        await asyncio.sleep(ANALYSIS_DELAY_SECONDS)
        if analysis_versions.get(chat_id, 0) != version:
            return

        d = await load_user(chat_id)
        repair_state(d)

        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)
        rebuild_counters(d)
        trim_state_memory(d)
        report = analyze_sequence(d)

        recheck = deep_recheck_new_rhythm(d, report)
        rhythm_ai = ai_new_rhythm(chat_id, report, d.get("labels", []))
        ghost_ai = ai_anti_ghost(chat_id, report, d.get("labels", []))
        monitor = monitor_ai(chat_id, d, report, rhythm_ai, ghost_ai, recheck)
        meta = final_decision(report, rhythm_ai, ghost_ai, recheck, monitor, d)

        if analysis_versions.get(chat_id, 0) != version:
            return

        try:
            await loading_msg.edit_text(build_analysis_message(report, rhythm_ai, ghost_ai, meta, monitor, recheck))
        except Exception:
            try:
                await loading_msg.reply_text(build_analysis_message(report, rhythm_ai, ghost_ai, meta, monitor, recheck))
            except Exception:
                pass

        await asyncio.sleep(0.05)

        try:
            await loading_msg.reply_text(build_final_message(meta))
        except Exception:
            pass

        await save_user_meta(chat_id, d)

    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("delayed_analysis_job failed: %s", e)
        try:
            await loading_msg.reply_text("❌ Lỗi khi phân tích")
        except Exception:
            pass


async def start_delayed_analysis(context: ContextTypes.DEFAULT_TYPE, chat_id: int, loading_msg) -> None:
    analysis_versions[chat_id] += 1
    version = analysis_versions[chat_id]
    cancel_pending_analysis(chat_id)

    task = context.application.create_task(
        delayed_analysis_job(chat_id=chat_id, version=version, loading_msg=loading_msg)
    )
    analysis_tasks[chat_id] = task

    def _cleanup(_task: asyncio.Task) -> None:
        if analysis_tasks.get(chat_id) is _task:
            analysis_tasks.pop(chat_id, None)

    task.add_done_callback(_cleanup)


# =========================
# COMMANDS
# =========================
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        cancel_pending_analysis(chat_id)
        users.pop(chat_id, None)
        await delete_chat_state(chat_id)
        if update.message:
            await update.message.reply_text("🔄 Đã reset chat hiện tại.")
    except Exception as e:
        logger.exception("reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi reset")


async def factory_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cancel_all_pending()
        users.clear()
        await wipe_all_state()
        init_db()
        if update.message:
            await update.message.reply_text("🧼 Đã xóa sạch toàn bộ dữ liệu.")
    except Exception as e:
        logger.exception("factory_reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi factory reset")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        d = await load_user(chat_id)
        report = analyze_sequence(d)
        if update.message:
            await update.message.reply_text(build_stats_message(report, d))
        await save_user_meta(chat_id, d)
    except Exception as e:
        logger.exception("stats failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xem thống kê")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = await load_user(chat_id)
        report = analyze_sequence(d)
        recheck = deep_recheck_new_rhythm(d, report)
        rhythm_ai = ai_new_rhythm(chat_id, report, d.get("labels", []))
        ghost_ai = ai_anti_ghost(chat_id, report, d.get("labels", []))
        monitor = monitor_ai(chat_id, d, report, rhythm_ai, ghost_ai, recheck)

        await update.message.reply_text(
            "🔧 DEBUG\n"
            f"ghost_mode: {d.get('ghost_mode')}\n"
            f"stability: {monitor.get('stability', 0):.1f}\n"
            f"severity: {monitor.get('severity')}\n"
            f"recheck: {monitor.get('recheck_status', '-')}\n"
            f"actions: {', '.join(monitor.get('actions', []))}\n"
            f"errors: {monitor.get('error_count', 0)}\n"
            f"last_clean: {d.get('last_clean_score', 0.0):.1f}\n"
            f"last_recheck: {d.get('last_recheck_score', 0.0):.1f}"
        )
        await save_user_meta(chat_id, d)
    except Exception as e:
        logger.exception("debug failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi debug")


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = await load_user(chat_id)
        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)

        await persist_snapshot(chat_id, d, [])
        await save_user_meta(chat_id, d)

        if cleanup.get("cleaned"):
            await update.message.reply_text(cleanup.get("message", "🧹 Đã dọn dữ liệu cũ."))
        else:
            await update.message.reply_text("✅ Hiện tại chưa cần dọn dữ liệu cũ.")
    except Exception as e:
        logger.exception("clean_cmd failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi dọn dữ liệu")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        await update.message.reply_text(
            f"📘 TRỢ GIÚP\n"
            f"/stats - xem bảng cầu\n"
            f"/ai - xem phân tích + chốt\n"
            f"/next - như /ai\n"
            f"/debug - xem trạng thái giám sát\n"
            f"/clean - kích hoạt dọn ám thủ công\n"
            f"/reset - xóa dữ liệu chat hiện tại\n"
            f"/factory_reset - xóa sạch toàn bộ bot\n\n"
            f"Bot dùng 4 lớp: AI nhịp mới, AI chống ám, RECHECK, AI giám sát.\n"
            f"Khi tự dọn ám, bot sẽ báo lên ngay trong chat.\n"
            f"Quy đổi: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}."
        )
    except Exception as e:
        logger.exception("help failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi mở trợ giúp")


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = await load_user(chat_id)
        repair_state(d)

        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)
        rebuild_counters(d)
        trim_state_memory(d)
        report = analyze_sequence(d)

        recheck = deep_recheck_new_rhythm(d, report)
        rhythm_ai = ai_new_rhythm(chat_id, report, d.get("labels", []))
        ghost_ai = ai_anti_ghost(chat_id, report, d.get("labels", []))
        monitor = monitor_ai(chat_id, d, report, rhythm_ai, ghost_ai, recheck)
        meta = final_decision(report, rhythm_ai, ghost_ai, recheck, monitor, d)

        await update.message.reply_text(build_stats_message(report, d))
        if cleanup.get("cleaned"):
            await update.message.reply_text(cleanup.get("message", "🧹 Bot vừa tự dọn dữ liệu cũ."))

        loading_msg = await update.message.reply_text(build_loading_message(report, rhythm_ai, ghost_ai, monitor, recheck))

        await persist_snapshot(chat_id, d, [])
        await save_user_meta(chat_id, d)

        await start_delayed_analysis(context, chat_id, loading_msg)
    except Exception as e:
        logger.exception("ai_cmd failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi AI kết luận")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ai_cmd(update, context)


# =========================
# MAIN HANDLE
# =========================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return

        chat_id = get_key(update)

        async with STATE_LOCK:
            d = repair_state(await load_user(chat_id))
            nums = parse_input(update.message.text)
            if not nums:
                return

            entries: List[Tuple[int, str, str, float]] = []

            for n in nums:
                label = map_value(n)
                d["values"].append(n)
                d["labels"].append(label)
                d["updates"] += 1

                if label == LOW_LABEL:
                    d["low_count"] += 1
                else:
                    d["high_count"] += 1

                d["history"].append({"value": n, "label": label, "source": "real", "conf": 1.0})
                d["health_log"].append(1)

                update_pattern_memory_in_memory(d)
                entries.append((n, label, "real", 1.0))

            rebuild_counters(d)
            trim_state_memory(d)

            report_now = analyze_sequence(d)
            cleanup = anti_ghost_cleanup(d, report_now)
            repair_state(d)
            rebuild_counters(d)
            trim_state_memory(d)
            report_now = analyze_sequence(d)

            recheck = deep_recheck_new_rhythm(d, report_now)
            rhythm_ai = ai_new_rhythm(chat_id, report_now, d.get("labels", []))
            ghost_ai = ai_anti_ghost(chat_id, report_now, d.get("labels", []))
            monitor = monitor_ai(chat_id, d, report_now, rhythm_ai, ghost_ai, recheck)
            meta = final_decision(report_now, rhythm_ai, ghost_ai, recheck, monitor, d)

            trim_state_memory(d)
            await persist_snapshot(chat_id, d, entries)
            await save_user_meta(chat_id, d)

        if update.message:
            await update.message.reply_text(build_stats_message(report_now, d))

            if cleanup.get("cleaned"):
                await update.message.reply_text(cleanup.get("message", "🧹 Bot vừa tự dọn dữ liệu cũ."))

            loading_msg = await update.message.reply_text(
                build_loading_message(report_now, rhythm_ai, ghost_ai, monitor, recheck)
            )
            await start_delayed_analysis(context, chat_id, loading_msg)

    except Exception as e:
        logger.exception("handle failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xử lý dữ liệu")


# =========================
# RUN
# =========================
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("clean", clean_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("factory_reset", factory_reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 SEQUENCE ANALYZER RUNNING...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
