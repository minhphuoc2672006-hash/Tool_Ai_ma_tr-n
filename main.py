import os
import re
import json
import math
import sqlite3
import asyncio
import logging
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "ai_state.db")

THRESHOLD = int(os.getenv("THRESHOLD", "11"))
LOW_LABEL = os.getenv("LOW_LABEL", "Xỉu")
HIGH_LABEL = os.getenv("HIGH_LABEL", "Tài")

RECENT_WINDOW = int(os.getenv("RECENT_WINDOW", "24"))
MID_WINDOW = int(os.getenv("MID_WINDOW", "80"))
SHORT_WINDOW = int(os.getenv("SHORT_WINDOW", "12"))

MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))
MAX_DB_HISTORY = int(os.getenv("MAX_DB_HISTORY", "2500"))
MAX_HEALTH_LOG = int(os.getenv("MAX_HEALTH_LOG", "240"))
MAX_PATTERN_MEMORY = int(os.getenv("MAX_PATTERN_MEMORY", "1800"))

MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "10"))
WHITE_SHIFT_THRESHOLD = float(os.getenv("WHITE_SHIFT_THRESHOLD", "0.32"))
WHITE_MIN_SCORE = float(os.getenv("WHITE_MIN_SCORE", "62"))

GHOST_HARD_CLEAN = float(os.getenv("GHOST_HARD_CLEAN", "85"))
GHOST_SOFT_CLEAN = float(os.getenv("GHOST_SOFT_CLEAN", "65"))
GHOST_WARN = float(os.getenv("GHOST_WARN", "55"))

MAX_DB_RETRIES = int(os.getenv("MAX_DB_RETRIES", "5"))
DB_RETRY_BASE_DELAY = float(os.getenv("DB_RETRY_BASE_DELAY", "0.35"))

if not TOKEN:
    raise RuntimeError("❌ Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
users: Dict[int, Dict[str, Any]] = {}


# =========================
# DB
# =========================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def safe_db_call(fn, retries: int = MAX_DB_RETRIES, base_delay: float = DB_RETRY_BASE_DELAY):
    last_exc = None
    for i in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            last_exc = e
            time.sleep(base_delay * (i + 1))
        except sqlite3.DatabaseError as e:
            last_exc = e
            time.sleep(base_delay * (i + 1))
    if last_exc:
        raise last_exc


async def run_db_work(fn):
    return await asyncio.to_thread(lambda: safe_db_call(fn))


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _ensure_column(conn: sqlite3.Connection, table: str, col_def: str) -> None:
    col_name = col_def.split()[0]
    if col_name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def init_db() -> None:
    def _work():
        with db_connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    low_count INTEGER NOT NULL DEFAULT 0,
                    high_count INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'NORMAL',
                    updates INTEGER NOT NULL DEFAULT 0,
                    ghost_mode INTEGER NOT NULL DEFAULT 0,
                    stability_score REAL NOT NULL DEFAULT 100.0,
                    last_clean_score REAL NOT NULL DEFAULT 0.0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    raw_value INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);

                CREATE TABLE IF NOT EXISTS patterns (
                    chat_id INTEGER NOT NULL,
                    pattern TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, pattern)
                );

                CREATE INDEX IF NOT EXISTS idx_patterns_chat_id ON patterns(chat_id);
                """
            )
            _ensure_column(conn, "users", "ghost_mode INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "stability_score REAL NOT NULL DEFAULT 100.0")
            _ensure_column(conn, "users", "last_clean_score REAL NOT NULL DEFAULT 0.0")
            _ensure_column(conn, "users", "error_count INTEGER NOT NULL DEFAULT 0")
            _ensure_column(conn, "users", "meta_json TEXT NOT NULL DEFAULT '{}'")
            conn.commit()

    safe_db_call(_work)


# =========================
# STATE
# =========================

def new_user() -> Dict[str, Any]:
    return {
        "values": [],
        "labels": [],
        "history": [],
        "low_count": 0,
        "high_count": 0,
        "mode": "NORMAL",
        "ghost_mode": False,
        "updates": 0,
        "stability_score": 100.0,
        "last_clean_score": 0.0,
        "error_count": 0,
        "monitor_log": [],
        "recheck_log": [],
        "pattern_memory": defaultdict(float),
        "pred_ema_probs": {LOW_LABEL: 0.5, HIGH_LABEL: 0.5},
        "last_prediction_label": None,
        "last_prediction_conf": 0,
        "last_decision_hint": "TRUNG_LẬP",
        "last_decision_note": "",
        "fast_break_score": 0.0,
        "noise_score": 0.0,
    }


def ensure_state(d: Any) -> Dict[str, Any]:
    if not isinstance(d, dict):
        d = new_user()
    return repair_state(d)


def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    if limit <= 0:
        return []
    return list(seq[-limit:]) if len(seq) > limit else list(seq)


def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)


def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), MAX_DB_HISTORY)
    d["labels"] = _safe_tail(d.get("labels", []), MAX_DB_HISTORY)
    d["history"] = _safe_tail(d.get("history", []), 80)
    d["monitor_log"] = _safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG)
    d["recheck_log"] = _safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG)


def _deserialize_float_map(src: Dict[str, float]) -> defaultdict:
    out = defaultdict(float)
    for k, v in (src or {}).items():
        out[k] = float(v)
    return out


def rebuild_counters(d: Dict[str, Any]) -> None:
    labels = d.get("labels", [])
    d["low_count"] = labels.count(LOW_LABEL)
    d["high_count"] = labels.count(HIGH_LABEL)
    d["updates"] = len(labels)


def repair_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        d = new_user()

    for k in ("values", "labels", "history", "monitor_log", "recheck_log"):
        if not isinstance(d.get(k), list):
            d[k] = []

    if not isinstance(d.get("pattern_memory"), defaultdict):
        try:
            d["pattern_memory"] = _deserialize_float_map(dict(d.get("pattern_memory", {})))
        except Exception:
            d["pattern_memory"] = defaultdict(float)

    if not isinstance(d.get("pred_ema_probs"), dict):
        d["pred_ema_probs"] = {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    n = min(len(d["values"]), len(d["labels"]))
    d["values"] = d["values"][-n:] if n else []
    d["labels"] = d["labels"][-n:] if n else []

    d.setdefault("mode", "NORMAL")
    d.setdefault("ghost_mode", False)
    d.setdefault("stability_score", 100.0)
    d.setdefault("last_clean_score", 0.0)
    d.setdefault("error_count", 0)
    d.setdefault("last_decision_hint", "TRUNG_LẬP")
    d.setdefault("last_decision_note", "")
    d.setdefault("fast_break_score", 0.0)
    d.setdefault("noise_score", 0.0)
    d.setdefault("pred_ema_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})
    d.setdefault("last_prediction_label", None)
    d.setdefault("last_prediction_conf", 0)

    try:
        d["pred_ema_probs"][LOW_LABEL] = float(d["pred_ema_probs"].get(LOW_LABEL, 0.5))
        d["pred_ema_probs"][HIGH_LABEL] = float(d["pred_ema_probs"].get(HIGH_LABEL, 0.5))
        d["pred_ema_probs"] = normalize_probs(d["pred_ema_probs"])
    except Exception:
        d["pred_ema_probs"] = {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    rebuild_counters(d)
    trim_state_memory(d)
    return d


async def load_user(chat_id: int) -> Dict[str, Any]:
    if chat_id in users:
        return ensure_state(users[chat_id])

    state = new_user()

    def _work():
        with db_connect() as conn:
            row = conn.execute(
                """
                SELECT low_count, high_count, mode, updates, ghost_mode,
                       stability_score, last_clean_score, error_count, meta_json
                FROM users WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            hist_rows = conn.execute(
                "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, MAX_DB_HISTORY),
            ).fetchall()
            pat_rows = conn.execute(
                "SELECT pattern, weight FROM patterns WHERE chat_id = ? ORDER BY weight DESC, pattern ASC LIMIT ?",
                (chat_id, MAX_PATTERN_MEMORY),
            ).fetchall()
            return row, hist_rows, pat_rows

    try:
        async with DB_LOCK:
            row, hist_rows, pat_rows = await run_db_work(_work)
    except Exception as e:
        logger.exception("load_user failed: %s", e)
        users[chat_id] = state
        return state

    if row:
        state["low_count"] = int(row["low_count"])
        state["high_count"] = int(row["high_count"])
        state["mode"] = row["mode"] or "NORMAL"
        state["updates"] = int(row["updates"] or 0)
        state["ghost_mode"] = bool(int(row["ghost_mode"] or 0))
        state["stability_score"] = float(row["stability_score"] or 100.0)
        state["last_clean_score"] = float(row["last_clean_score"] or 0.0)
        state["error_count"] = int(row["error_count"] or 0)
        try:
            meta = json.loads(row["meta_json"] or "{}")
            state["monitor_log"] = list(meta.get("monitor_log", []))
            state["recheck_log"] = list(meta.get("recheck_log", []))
            state["pred_ema_probs"] = normalize_probs(meta.get("pred_ema_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}))
            state["last_prediction_label"] = meta.get("last_prediction_label")
            state["last_prediction_conf"] = int(meta.get("last_prediction_conf", 0) or 0)
            state["last_decision_hint"] = meta.get("last_decision_hint", "TRUNG_LẬP")
            state["last_decision_note"] = meta.get("last_decision_note", "")
            state["fast_break_score"] = float(meta.get("fast_break_score", 0.0) or 0.0)
            state["noise_score"] = float(meta.get("noise_score", 0.0) or 0.0)
        except Exception:
            pass

    for r in reversed(hist_rows):
        state["values"].append(int(r["raw_value"]))
        state["labels"].append(r["label"])

    state["pattern_memory"] = _deserialize_float_map({r["pattern"]: float(r["weight"]) for r in pat_rows})
    state["history"] = [{"value": v, "label": l, "source": "real", "conf": 1.0} for v, l in list(zip(state["values"], state["labels"]))[-80:]]
    repair_state(state)
    users[chat_id] = state
    trim_cache()
    return ensure_state(state)


async def save_user(chat_id: int, d: Dict[str, Any]) -> None:
    d = ensure_state(d)
    try:
        async with DB_LOCK:
            def _work():
                with db_connect() as conn:
                    meta = {
                        "monitor_log": _safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG),
                        "recheck_log": _safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG),
                        "pred_ema_probs": normalize_probs(d.get("pred_ema_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})),
                        "last_prediction_label": d.get("last_prediction_label"),
                        "last_prediction_conf": int(d.get("last_prediction_conf", 0) or 0),
                        "last_decision_hint": d.get("last_decision_hint", "TRUNG_LẬP"),
                        "last_decision_note": d.get("last_decision_note", ""),
                        "fast_break_score": float(d.get("fast_break_score", 0.0) or 0.0),
                        "noise_score": float(d.get("noise_score", 0.0) or 0.0),
                    }
                    conn.execute(
                        """
                        INSERT INTO users (
                            chat_id, low_count, high_count, mode, updates, ghost_mode,
                            stability_score, last_clean_score, error_count, meta_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            low_count=excluded.low_count,
                            high_count=excluded.high_count,
                            mode=excluded.mode,
                            updates=excluded.updates,
                            ghost_mode=excluded.ghost_mode,
                            stability_score=excluded.stability_score,
                            last_clean_score=excluded.last_clean_score,
                            error_count=excluded.error_count,
                            meta_json=excluded.meta_json
                        """,
                        (
                            chat_id,
                            int(d.get("low_count", 0)),
                            int(d.get("high_count", 0)),
                            d.get("mode", "NORMAL"),
                            int(d.get("updates", 0)),
                            1 if d.get("ghost_mode", False) else 0,
                            float(d.get("stability_score", 100.0)),
                            float(d.get("last_clean_score", 0.0)),
                            int(d.get("error_count", 0)),
                            json.dumps(meta, ensure_ascii=False),
                        ),
                    )
                    conn.commit()

            await run_db_work(_work)
    except Exception as e:
        logger.exception("save_user failed: %s", e)


def prune_chat_rows(conn: sqlite3.Connection, chat_id: int, keep_limit: int) -> None:
    row = conn.execute(
        "SELECT id FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (chat_id, max(0, keep_limit - 1)),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM history WHERE chat_id = ? AND id < ?", (chat_id, int(row["id"])))


async def persist_snapshot(chat_id: int, d: Dict[str, Any], entries: List[Tuple[int, str]]) -> None:
    d = ensure_state(d)
    try:
        async with DB_LOCK:
            def _work():
                with db_connect() as conn:
                    for raw_value, label in entries:
                        conn.execute(
                            "INSERT INTO history (chat_id, raw_value, label) VALUES (?, ?, ?)",
                            (chat_id, int(raw_value), label),
                        )

                    conn.execute("DELETE FROM patterns WHERE chat_id = ?", (chat_id,))
                    for pattern, weight in d.get("pattern_memory", {}).items():
                        if float(weight) >= 0.05:
                            conn.execute(
                                "INSERT INTO patterns (chat_id, pattern, weight) VALUES (?, ?, ?)",
                                (chat_id, pattern, float(weight)),
                            )

                    meta = {
                        "monitor_log": _safe_tail(d.get("monitor_log", []), MAX_HEALTH_LOG),
                        "recheck_log": _safe_tail(d.get("recheck_log", []), MAX_HEALTH_LOG),
                        "pred_ema_probs": normalize_probs(d.get("pred_ema_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})),
                        "last_prediction_label": d.get("last_prediction_label"),
                        "last_prediction_conf": int(d.get("last_prediction_conf", 0) or 0),
                        "last_decision_hint": d.get("last_decision_hint", "TRUNG_LẬP"),
                        "last_decision_note": d.get("last_decision_note", ""),
                        "fast_break_score": float(d.get("fast_break_score", 0.0) or 0.0),
                        "noise_score": float(d.get("noise_score", 0.0) or 0.0),
                    }
                    conn.execute(
                        """
                        INSERT INTO users (
                            chat_id, low_count, high_count, mode, updates, ghost_mode,
                            stability_score, last_clean_score, error_count, meta_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            low_count=excluded.low_count,
                            high_count=excluded.high_count,
                            mode=excluded.mode,
                            updates=excluded.updates,
                            ghost_mode=excluded.ghost_mode,
                            stability_score=excluded.stability_score,
                            last_clean_score=excluded.last_clean_score,
                            error_count=excluded.error_count,
                            meta_json=excluded.meta_json
                        """,
                        (
                            chat_id,
                            int(d.get("low_count", 0)),
                            int(d.get("high_count", 0)),
                            d.get("mode", "NORMAL"),
                            int(d.get("updates", 0)),
                            1 if d.get("ghost_mode", False) else 0,
                            float(d.get("stability_score", 100.0)),
                            float(d.get("last_clean_score", 0.0)),
                            int(d.get("error_count", 0)),
                            json.dumps(meta, ensure_ascii=False),
                        ),
                    )

                    keep_limit = MAX_DB_HISTORY if not d.get("ghost_mode", False) else max(400, MAX_DB_HISTORY // 2)
                    prune_chat_rows(conn, chat_id, keep_limit)
                    conn.commit()

            await run_db_work(_work)
    except Exception as e:
        logger.exception("persist_snapshot failed: %s", e)


# =========================
# CORE HELPERS
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
        except Exception:
            pass
    return nums[:MAX_INPUT_NUMS]


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def format_prob_inline(probs: Dict[str, float]) -> str:
    low_p = probs.get(LOW_LABEL, 0.5) * 100
    high_p = probs.get(HIGH_LABEL, 0.5) * 100
    return f"{LOW_LABEL}: {low_p:.1f}% | {HIGH_LABEL}: {high_p:.1f}%"


def format_history(labels: List[str], tail: int = 24) -> str:
    if not labels:
        return "(trống)"
    view = labels[-tail:] if len(labels) > tail else labels[:]
    return "".join("⬛" if lb == HIGH_LABEL else "⬜" if lb == LOW_LABEL else "·" for lb in view)


def format_pattern_lines(patterns: List[Tuple[str, float]], top_n: int = 3) -> str:
    if not patterns:
        return "Không có"
    items = []
    for p, w in patterns[:top_n]:
        short_p = p if len(p) <= 24 else p[:21] + "..."
        items.append(f"{short_p}({w:.1f})")
    return " | ".join(items)


def make_bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def layer_ratio(labels: List[str]) -> Dict[str, float]:
    if not labels:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(labels)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: c[LOW_LABEL] / total, HIGH_LABEL: c[HIGH_LABEL] / total}


def split_layers(labels: List[str]) -> Tuple[List[str], List[str], List[str]]:
    recent = labels[-RECENT_WINDOW:] if len(labels) > RECENT_WINDOW else labels[:]
    mid_end = max(0, len(labels) - len(recent))
    mid_start = max(0, mid_end - MID_WINDOW)
    mid = labels[mid_start:mid_end]
    old = labels[:mid_start]
    return recent, mid, old


def normalize_probs(scores: Dict[str, float]) -> Dict[str, float]:
    scores = {LOW_LABEL: max(0.0, scores.get(LOW_LABEL, 0.0)), HIGH_LABEL: max(0.0, scores.get(HIGH_LABEL, 0.0))}
    total = scores[LOW_LABEL] + scores[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: scores[LOW_LABEL] / total, HIGH_LABEL: scores[HIGH_LABEL] / total}


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


def volatility_score(labels: List[str]) -> float:
    tail = labels[-12:]
    if len(tail) < 8:
        return 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    return changes / (len(tail) - 1)


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


def detect_repeat_cycle(labels: List[str]) -> Tuple[Optional[int], float]:
    if len(labels) < 10:
        return None, 0.0
    h = labels[-min(360, len(labels)):]
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


def summarize_patterns(d: Dict[str, Any], top_n: int = 5) -> List[Tuple[str, float]]:
    items = list(d.get("pattern_memory", {}).items())
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:top_n]


def decay_pattern_memory(d: Dict[str, Any]) -> None:
    pm = d.get("pattern_memory", {})
    for k in list(pm.keys()):
        pm[k] *= 0.99
        if pm[k] < 0.05:
            del pm[k]


def update_pattern_memory_in_memory(d: Dict[str, Any]) -> None:
    d = ensure_state(d)
    labels = d.get("labels", [])
    if len(labels) >= 4:
        d["pattern_memory"]["|".join(labels[-4:])] += 1.0
    if len(labels) >= 5:
        d["pattern_memory"]["|".join(labels[-5:])] += 0.7
    if len(labels) >= 6:
        d["pattern_memory"]["|".join(labels[-6:])] += 0.5
    decay_pattern_memory(d)
    if len(d["pattern_memory"]) > MAX_PATTERN_MEMORY:
        items = sorted(d["pattern_memory"].items(), key=lambda x: x[1], reverse=True)
        d["pattern_memory"].clear()
        d["pattern_memory"].update(dict(items[:MAX_PATTERN_MEMORY]))


def weighted_label_probs(labels: List[str], window: int = 24, decay: float = 0.90) -> Dict[str, float]:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if not tail:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    scores = {LOW_LABEL: 0.0, HIGH_LABEL: 0.0}
    weight = 1.0
    for lb in reversed(tail):
        if lb in scores:
            scores[lb] += weight
        weight *= decay
    return normalize_probs(scores)


def ensemble_recent_probs(labels: List[str]) -> Dict[str, float]:
    if not labels:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    p4 = weighted_label_probs(labels, window=4, decay=0.82)
    p6 = weighted_label_probs(labels, window=6, decay=0.85)
    p8 = weighted_label_probs(labels, window=8, decay=0.88)
    p12 = weighted_label_probs(labels, window=12, decay=0.91)
    p24 = weighted_label_probs(labels, window=24, decay=0.94)

    scores = {
        LOW_LABEL: p4[LOW_LABEL] * 0.30 + p6[LOW_LABEL] * 0.26 + p8[LOW_LABEL] * 0.20 + p12[LOW_LABEL] * 0.14 + p24[LOW_LABEL] * 0.10,
        HIGH_LABEL: p4[HIGH_LABEL] * 0.30 + p6[HIGH_LABEL] * 0.26 + p8[HIGH_LABEL] * 0.20 + p12[HIGH_LABEL] * 0.14 + p24[HIGH_LABEL] * 0.10,
    }
    return normalize_probs(scores)


def smooth_prediction_probs(d: Dict[str, Any], current_probs: Dict[str, float]) -> Dict[str, float]:
    prev = normalize_probs(d.get("pred_ema_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}))
    current_probs = normalize_probs(current_probs)
    alpha = 0.72 if len(d.get("labels", [])) >= 20 else 0.80
    smoothed = {
        LOW_LABEL: alpha * current_probs[LOW_LABEL] + (1.0 - alpha) * prev[LOW_LABEL],
        HIGH_LABEL: alpha * current_probs[HIGH_LABEL] + (1.0 - alpha) * prev[HIGH_LABEL],
    }
    smoothed = normalize_probs(smoothed)
    d["pred_ema_probs"] = smoothed
    return smoothed


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
            "early_shift_score": 0.0,
            "turn_score": 0.0,
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

    last4 = labels[-4:]
    prev4 = labels[-8:-4] if len(labels) >= 8 else labels[:-4]
    if prev4 and len(prev4) >= 2:
        last4_ratio = layer_ratio(last4)
        prev4_ratio = layer_ratio(prev4)
        micro_shift_score = abs(last4_ratio[LOW_LABEL] - prev4_ratio[LOW_LABEL])
    else:
        micro_shift_score = shift_score * 0.85

    ultra_recent = labels[-8:]
    prev_block = labels[-16:-8] if len(labels) >= 16 else labels[:-8]
    if prev_block and len(prev_block) >= 4:
        ultra_ratio = layer_ratio(ultra_recent)
        prev_ratio = layer_ratio(prev_block)
        early_shift_score = abs(ultra_ratio[LOW_LABEL] - prev_ratio[LOW_LABEL])
    else:
        early_shift_score = shift_score * 0.85

    turn_score = max(early_shift_score, micro_shift_score)

    ghost_score = (
        shift_score * 26.0
        + turn_score * 36.0
        + old_bias * 12.0
        + drift_js * 18.0
        + max(0.0, recent_entropy - 0.55) * 10.0
        + recent_vol * 8.0
    )
    ghost_score = max(0.0, min(100.0, ghost_score))

    if turn_score >= 0.22 and recent_vol >= 0.35:
        white_type = "CẦU CHUYỂN PHA"
        white_detail = "Đuôi gần nhất đang đổi nhịp sớm"
    elif shift_score >= WHITE_SHIFT_THRESHOLD and old_bias >= 0.18:
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
        "early_shift_score": early_shift_score,
        "turn_score": turn_score,
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
    turn_score = float(report.get("turn_score", 0.0))
    early_shift_score = float(report.get("early_shift_score", 0.0))

    if alt and alt_ratio >= 0.82:
        return "CẦU XEN KẼ", "Chuỗi đổi nhịp liên tục"
    if streak >= 4 and last_label in (LOW_LABEL, HIGH_LABEL):
        return "CẦU BỆT", f"{last_label} x{streak}"
    if cycle_score >= 68 and cycle_len:
        return "CẦU LẶP", f"Chu kỳ {cycle_len}"
    if turn_score >= 0.22 or early_shift_score >= 0.18 or shift_score >= WHITE_SHIFT_THRESHOLD:
        return "CẦU CHUYỂN PHA", "Nhịp dữ liệu đang đổi sớm"
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
    turn_score = float(report.get("turn_score", 0.0))
    early_shift_score = float(report.get("early_shift_score", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    streak = int(report.get("streak", 0))

    if white_type in ("CẦU TRẮNG BỊ ÁM", "CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA") and white_score >= WHITE_MIN_SCORE:
        if white_type == "CẦU TRẮNG BỊ ÁM":
            return "ANTI_GHOST", "Cầu mới đang bị dữ liệu cũ ám"
        if white_type == "CẦU TRẮNG KHÁNG ÁM":
            return "WHITE_CLEAN", "Cầu mới đã tách khỏi ám cũ"
        return "WHITE_SHIFT", "Đang hình thành cầu mới"

    if ghost_score >= GHOST_HARD_CLEAN:
        return "ANTI_GHOST", "Ám lịch sử rất mạnh"
    if ghost_score >= GHOST_SOFT_CLEAN and (shift_score >= WHITE_SHIFT_THRESHOLD or turn_score >= 0.18 or early_shift_score >= 0.15):
        return "ANTI_GHOST", "Đang có độ lệch lịch sử"
    if alt and alt_ratio >= 1.0:
        return "ALT", "Xen kẽ mạnh"
    if streak >= 6:
        return "STREAK", f"Chuỗi dài x{streak}"
    if cycle_score >= 68 and cycle_len:
        return "CYCLE", f"Vòng lặp rõ (size {cycle_len})"
    if vol >= 0.85 and ent >= 0.85:
        return "NOISY", "Nhiễu cao"
    if len(labels) >= 20 and abs(d.get("low_count", 0) - d.get("high_count", 0)) <= 2:
        return "BALANCED", "Cân bằng"
    return "NORMAL", "Ổn định"


def decide_bet_or_follow(report: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[str, str]:
    cau_type = report.get("cau_type", "CHƯA RÕ CẦU")
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    turn_score = float(report.get("turn_score", 0.0))
    early_shift_score = float(report.get("early_shift_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))

    if cau_type == "CẦU XEN KẼ":
        return "BẺ", "Xen kẽ rõ, ưu tiên đảo nhịp."
    if cau_type == "CẦU BỆT":
        return "THEO", "Cầu bệt rõ, ưu tiên bám chuỗi."
    if cau_type == "CẦU LẶP":
        return "THEO", "Có chu kỳ, ưu tiên bám mẫu lặp."
    if cau_type in ("CẦU CHUYỂN PHA", "CẦU ĐUÔI MỚI"):
        return "THEO", "Chuyển pha, ưu tiên đuôi mới."
    if cau_type == "CẦU TRẮNG KHÁNG ÁM":
        return "THEO", "Cầu mới đủ sạch, ưu tiên theo nhịp mới."
    if cau_type == "CẦU TRẮNG BỊ ÁM":
        return "THEO", "Có ám cũ, vẫn ưu tiên nhịp mới nhưng thận trọng."
    if cau_type == "CẦU RUNG":
        if turn_score >= 0.22 or early_shift_score >= 0.18:
            return "BẺ", "Rung mạnh và đổi pha sớm."
        return "TRUNG_LẬP", "Rung nhẹ, chưa đủ chắc để bẻ."
    if cau_type == "CẦU HỖN LOẠN":
        if ghost_score >= 70 or turn_score >= 0.24:
            return "BẺ", "Nhiễu cao, ưu tiên bẻ theo nhịp gần."
        return "TRUNG_LẬP", "Nhiễu cao nhưng chưa khóa được hướng."
    if alt and alt_ratio >= 0.80:
        return "BẺ", "Nhịp đảo cao, ưu tiên bẻ."
    if last_label in (LOW_LABEL, HIGH_LABEL) and streak >= 4 and turn_score < 0.12:
        return "THEO", "Chuỗi đủ dài và chưa có dấu hiệu bẻ."
    if turn_score >= 0.20 or early_shift_score >= 0.18:
        return "BẺ", "Tín hiệu đổi pha sớm."
    return "TRUNG_LẬP", "Chưa đủ tín hiệu."


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
    ghost_info = detect_ghost_pressure(labels)

    white_type = ghost_info["white_type"]
    white_detail = ghost_info["white_detail"]
    ghost_score = float(ghost_info["ghost_score"])
    shift_score = float(ghost_info["shift_score"])
    white_score = float(ghost_info["white_score"])

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
            "patterns": summarize_patterns(d, top_n=5),
        }
    )

    mode = detect_mode(
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
    )[0]

    if white_type in ("CẦU TRẮNG BỊ ÁM", "CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA") and white_score >= WHITE_MIN_SCORE:
        cau_type = white_type
        cau_detail = white_detail

    d["mode"] = mode
    return {
        "mode": mode,
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
        "cycle_consistency": cycle_score,
        "cau_type": cau_type,
        "cau_detail": cau_detail,
        "white_type": white_type,
        "white_detail": white_detail,
        "white_score": white_score,
        "ghost_score": ghost_score,
        "shift_score": shift_score,
        "early_shift_score": ghost_info["early_shift_score"],
        "turn_score": ghost_info["turn_score"],
        "recent_entropy": ghost_info["recent_entropy"],
        "recent_volatility": ghost_info["recent_volatility"],
        "old_bias": ghost_info["old_bias"],
        "recent_bias": ghost_info["recent_bias"],
        "recent_low_ratio": ghost_info["recent_low_ratio"],
        "old_low_ratio": ghost_info["old_low_ratio"],
        "freshness": ghost_info["freshness"],
        "mid_bias": ghost_info["mid_bias"],
        "drift_js": ghost_info["drift_js"],
        "patterns": summarize_patterns(d, top_n=5),
    }


def fast_break_score(report: Dict[str, Any]) -> float:
    turn_score = float(report.get("turn_score", 0.0))
    early_shift_score = float(report.get("early_shift_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))
    vol = float(report.get("volatility", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    recent_gap = abs(float(report.get("recent_low_ratio", 0.5)) - float(report.get("old_low_ratio", 0.5)))

    score = (
        turn_score * 46.0
        + early_shift_score * 32.0
        + shift_score * 16.0
        + recent_gap * 12.0
        + cycle_score / 12.0
        - vol * 12.0
        - max(0.0, ghost_score - 45.0) * 0.18
    )
    return max(0.0, min(100.0, score))


def noise_filter_score(report: Dict[str, Any]) -> float:
    ghost_score = float(report.get("ghost_score", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    drift_js = float(report.get("drift_js", 0.0))
    recent_vol = float(report.get("recent_volatility", 0.0))
    mid_bias = float(report.get("mid_bias", 0.0))

    score = (
        ghost_score * 0.42
        + vol * 22.0
        + ent * 18.0
        + drift_js * 20.0
        + recent_vol * 10.0
        + mid_bias * 12.0
    )
    return max(0.0, min(100.0, score))


def merge_two_layers(report: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    fast_score = fast_break_score(report)
    noise_score = noise_filter_score(report)

    hint = meta.get("decision_hint", "TRUNG_LẬP")
    note = meta.get("decision_note", "")
    conf = int(meta.get("confidence", 0) or 0)

    if noise_score >= 72:
        hint = "TRUNG_LẬP"
        note = "Nhiễu cao, chưa bẻ vội."
        conf = max(30, conf - 14)
    elif fast_score >= 68:
        hint = "BẺ"
        note = note or "Tín hiệu đổi nhịp sớm."
        conf = min(100, conf + 8)
    elif fast_score >= 52 and noise_score < 55:
        hint = "THEO"
        note = note or "Tín hiệu đủ sạch, ưu tiên bám."
        conf = min(100, conf + 4)
    elif hint == "TRUNG_LẬP":
        if float(report.get("turn_score", 0.0)) >= 0.18 or float(report.get("early_shift_score", 0.0)) >= 0.16:
            hint = "BẺ"
            note = "Có dấu hiệu đổi sớm."
        elif float(report.get("volatility", 0.0)) <= 0.45:
            hint = "THEO"
            note = "Độ nhiễu thấp, ưu tiên bám nhịp."

    meta["decision_hint"] = hint
    meta["decision_note"] = note
    meta["fast_break_score"] = round(fast_score, 1)
    meta["noise_score"] = round(noise_score, 1)
    meta["confidence"] = max(0, min(100, conf))
    return meta


def ai_level2_decision(d: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    labels = d.get("labels", [])
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    cau_type = report.get("cau_type", "CHƯA RÕ CẦU")
    white_type = report.get("white_type", "CHƯA RÕ CẦU TRẮNG")
    cycle_len = report.get("cycle_len")
    shift_score = float(report.get("shift_score", 0.0))
    early_shift_score = float(report.get("early_shift_score", 0.0))
    turn_score = float(report.get("turn_score", 0.0))
    ghost_score = float(report.get("ghost_score", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))

    recent_labels, _, _ = split_layers(labels)
    recent_probs = ensemble_recent_probs(recent_labels)
    recent_best = max(recent_probs, key=recent_probs.get)
    recent_gap = abs(recent_probs[LOW_LABEL] - recent_probs[HIGH_LABEL])

    def opposite(lb: Optional[str]) -> Optional[str]:
        if lb == LOW_LABEL:
            return HIGH_LABEL
        if lb == HIGH_LABEL:
            return LOW_LABEL
        return None

    def pack(label: Optional[str], conf: int, note: str, source: str) -> Dict[str, Any]:
        if label not in (LOW_LABEL, HIGH_LABEL):
            label = recent_best if recent_best in (LOW_LABEL, HIGH_LABEL) else (last_label or LOW_LABEL)
        return {
            "final_label": label,
            "confidence": max(0, min(100, int(conf))),
            "timing_state": "ON_TIME",
            "timing_note": note,
            "source": source,
            "cau_type": cau_type,
            "white_type": white_type,
            "decision_hint": "TRUNG_LẬP",
            "decision_note": "",
            "recent_probs": recent_probs,
        }

    if len(labels) < MIN_ANALYSIS_LEN:
        meta = pack(recent_best, 0, "Chưa đủ dữ liệu.", "WARMUP")
        meta["decision_note"] = "Chưa đủ dữ liệu."
        return meta

    decision_hint, decision_note = decide_bet_or_follow(report, {})

    if cau_type == "CẦU XEN KẼ":
        if last_label in (LOW_LABEL, HIGH_LABEL):
            meta = pack(opposite(last_label), 82, "Cầu xen kẽ: bẻ theo nhịp đảo.", "CẦU XEN KẼ")
            meta["decision_hint"] = "BẺ"
            meta["decision_note"] = "Xen kẽ rõ, ưu tiên đảo nhịp."
            return meta

    if cau_type == "CẦU BỆT":
        if last_label in (LOW_LABEL, HIGH_LABEL):
            meta = pack(last_label, 84, f"Cầu bệt: theo {last_label}.", "CẦU BỆT")
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = f"Cầu bệt rõ, ưu tiên bám {last_label}."
            return meta

    if cau_type == "CẦU LẶP":
        cycle_pred, cycle_conf = predict_cycle_next(labels, cycle_len)
        if cycle_pred in (LOW_LABEL, HIGH_LABEL):
            conf = 80 if cycle_conf >= 70 else 72 if cycle_conf >= 55 else 66
            meta = pack(cycle_pred, conf, f"Cầu lặp: theo chu kỳ {cycle_len}.", "CẦU LẶP")
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = f"Có chu kỳ {cycle_len}, ưu tiên theo mẫu."
            return meta

    if cau_type in ("CẦU CHUYỂN PHA", "CẦU ĐUÔI MỚI"):
        short_recent = labels[-6:] if len(labels) >= 6 else labels[:]
        short_probs = ensemble_recent_probs(short_recent)
        short_best = max(short_probs, key=short_probs.get)
        if short_best in (LOW_LABEL, HIGH_LABEL):
            conf = 76 if recent_gap >= 0.08 else 70
            if turn_score >= 0.22 or early_shift_score >= 0.18:
                conf += 6
            if white_type in ("CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA"):
                conf += 2
            meta = pack(short_best, conf, "Cầu chuyển pha: ưu tiên đuôi gần nhất.", cau_type)
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = "Chuyển pha, ưu tiên đuôi mới nhất."
            return meta

    if cau_type == "CẦU TRẮNG KHÁNG ÁM":
        meta = pack(recent_best, 78 if recent_gap >= 0.06 else 70, "Cầu trắng kháng ám: ưu tiên nhịp mới sạch.", cau_type)
        meta["decision_hint"] = "THEO"
        meta["decision_note"] = "Cầu mới sạch, ưu tiên theo nhịp mới."
        return meta

    if cau_type == "CẦU TRẮNG BỊ ÁM":
        meta = pack(recent_best, 70 if recent_gap >= 0.06 else 64, "Cầu trắng bị ám: đọc đuôi mới nhưng thận trọng.", cau_type)
        meta["decision_hint"] = "THEO"
        meta["decision_note"] = "Có ám cũ, vẫn theo nhịp mới nhưng giảm tin."
        return meta

    if cau_type == "CẦU TRẮNG":
        meta = pack(recent_best, 72 if recent_gap >= 0.06 else 66, "Cầu trắng: ưu tiên tín hiệu gần nhất.", cau_type)
        meta["decision_hint"] = "THEO"
        meta["decision_note"] = "Tín hiệu ngắn hạn đủ rõ."
        return meta

    if cau_type == "CẦU RUNG":
        if recent_gap >= 0.10:
            meta = pack(recent_best, 66, "Cầu rung: dùng đuôi gần nhất.", cau_type)
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = "Rung nhưng đuôi gần vẫn rõ hơn."
            return meta
        if last_label in (LOW_LABEL, HIGH_LABEL):
            meta = pack(opposite(last_label), 62, "Cầu rung: ưu tiên đảo nhịp ngắn hạn.", cau_type)
            meta["decision_hint"] = "BẺ"
            meta["decision_note"] = "Rung và có dấu hiệu đảo."
            return meta

    if cau_type == "CẦU HỖN LOẠN":
        if recent_gap >= 0.08:
            meta = pack(recent_best, 60, "Cầu hỗn loạn: chọn tín hiệu gần hơn.", cau_type)
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = "Nhiễu cao, chọn tín hiệu gần."
            return meta
        if last_label in (LOW_LABEL, HIGH_LABEL):
            meta = pack(last_label, 56, "Cầu hỗn loạn: bám nhịp mới nhất.", cau_type)
            meta["decision_hint"] = "TRUNG_LẬP"
            meta["decision_note"] = "Nhiễu mạnh, chốt dè chừng."
            return meta

    if cau_type == "CẦU ỔN ĐỊNH":
        if last_label in (LOW_LABEL, HIGH_LABEL) and streak >= 3:
            meta = pack(last_label, 75, "Cầu ổn định: bám nhịp đang chạy.", cau_type)
            meta["decision_hint"] = "THEO"
            meta["decision_note"] = "Chuỗi ổn định, ưu tiên bám chuỗi."
            return meta

    if last_label in (LOW_LABEL, HIGH_LABEL) and recent_gap < 0.04:
        meta = pack(last_label, 58, "Cầu chưa rõ: bám ván gần nhất.", "CHƯA RÕ CẦU")
        meta["decision_hint"] = "TRUNG_LẬP"
        meta["decision_note"] = "Chưa đủ chênh lệch để bẻ."
        return meta

    if recent_best in (LOW_LABEL, HIGH_LABEL):
        meta = pack(recent_best, 60, "Cầu chưa rõ: ưu tiên đuôi gần nhất.", "CHƯA RÕ CẦU")
        meta["decision_hint"] = "TRUNG_LẬP"
        meta["decision_note"] = "Ưu tiên đuôi gần nhất."
        return meta

    meta = pack(LOW_LABEL, 50, "Cầu chưa rõ.", "CHƯA RÕ CẦU")
    meta["decision_hint"] = "TRUNG_LẬP"
    meta["decision_note"] = "Không đủ tín hiệu."
    return meta


def final_decision(report: Dict[str, Any], d: Dict[str, Any]) -> Dict[str, Any]:
    meta = ai_level2_decision(d, report)

    recent_labels = d.get("labels", [])[-max(8, min(RECENT_WINDOW, len(d.get("labels", [])))):]
    raw_probs = meta.get("recent_probs") or ensemble_recent_probs(recent_labels)
    smoothed = smooth_prediction_probs(d, raw_probs)

    final_label = max(smoothed, key=smoothed.get)
    if meta.get("final_label") in (LOW_LABEL, HIGH_LABEL):
        gap = abs(smoothed[LOW_LABEL] - smoothed[HIGH_LABEL])
        if meta["final_label"] == final_label or gap < 0.08:
            final_label = meta["final_label"]

    gap_pct = abs(smoothed[LOW_LABEL] - smoothed[HIGH_LABEL]) * 100.0
    conf = int(48 + gap_pct * 0.95)

    if report.get("white_type") in ("CẦU TRẮNG KHÁNG ÁM", "CẦU CHUYỂN PHA"):
        conf += 4
    if report.get("ghost_score", 0.0) >= 70:
        conf += 2
    if report.get("volatility", 0.0) >= 0.75:
        conf -= 2
    if report.get("entropy", 0.0) >= 0.80:
        conf -= 2
    if report.get("freshness", 0.0) >= 0.60:
        conf += 2

    prev_conf = int(d.get("last_prediction_conf", 0) or 0)
    if prev_conf > 0:
        conf = int(round(prev_conf * 0.35 + conf * 0.65))
        conf = max(prev_conf - 8, min(prev_conf + 8, conf))

    # lớp 2: bẻ cầu / chống nhiễu
    meta = merge_two_layers(report, meta)

    # nếu lớp 2 ép TRUNG LẬP thì giữ % an toàn hơn
    if meta.get("decision_hint") == "TRUNG_LẬP":
        conf = min(conf, max(32, int(conf * 0.92)))
    elif meta.get("decision_hint") == "BẺ":
        conf = min(100, conf + 2)
    elif meta.get("decision_hint") == "THEO":
        conf = min(100, conf + 1)

    conf = max(0, min(100, conf))

    meta["final_label"] = final_label
    meta["confidence"] = conf
    meta["final_probs"] = smoothed
    meta["other_label"] = HIGH_LABEL if final_label == LOW_LABEL else LOW_LABEL

    d["last_prediction_label"] = final_label
    d["last_prediction_conf"] = conf
    d["pred_ema_probs"] = smoothed
    d["last_decision_hint"] = meta.get("decision_hint", "TRUNG_LẬP")
    d["last_decision_note"] = meta.get("decision_note", "")
    d["fast_break_score"] = float(meta.get("fast_break_score", 0.0) or 0.0)
    d["noise_score"] = float(meta.get("noise_score", 0.0) or 0.0)
    return meta


def anti_ghost_cleanup(d: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    ghost_score = float(report.get("ghost_score", 0.0))
    d["last_clean_score"] = ghost_score
    result = {"cleaned": False, "level": "none", "message": ""}

    if ghost_score >= GHOST_HARD_CLEAN:
        d["ghost_mode"] = True
        d["monitor_log"].append("hard_clean")
        d["pattern_memory"].clear()
        d["labels"] = _safe_tail(d["labels"], max(180, RECENT_WINDOW * 2))
        d["values"] = _safe_tail(d["values"], max(180, RECENT_WINDOW * 2))
        result.update({"cleaned": True, "level": "hard", "message": f"🧹 Đã dọn mạnh lịch sử cũ ({ghost_score:.0f}%)."})
        return result

    if ghost_score >= GHOST_SOFT_CLEAN:
        d["ghost_mode"] = True
        d["monitor_log"].append("soft_clean")
        for k in list(d["pattern_memory"].keys()):
            d["pattern_memory"][k] *= 0.40
            if d["pattern_memory"][k] < 0.05:
                del d["pattern_memory"][k]
        result.update({"cleaned": True, "level": "soft", "message": f"🧹 Đã giảm ảnh hưởng lịch sử cũ ({ghost_score:.0f}%)."})
        return result

    if ghost_score >= GHOST_WARN:
        d["ghost_mode"] = True
        d["monitor_log"].append("warn_clean")
        for k in list(d["pattern_memory"].keys()):
            d["pattern_memory"][k] *= 0.70
            if d["pattern_memory"][k] < 0.05:
                del d["pattern_memory"][k]
        result.update({"cleaned": True, "level": "warn", "message": f"⚠️ Bot vừa làm nhẹ dữ liệu cũ ({ghost_score:.0f}%)."})
        return result

    d["ghost_mode"] = False
    d["monitor_log"].append("stable")
    return result


def monitor_ai(d: Dict[str, Any], report: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    ghost_score = float(report.get("ghost_score", 0.0))
    shift_score = float(report.get("shift_score", 0.0))
    freshness = float(report.get("freshness", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))

    stability = 100.0 - ghost_score * 0.55 - shift_score * 35.0 - vol * 12.0 - ent * 8.0 + freshness * 10.0
    if d.get("ghost_mode", False):
        stability -= 5.0
    stability = max(0.0, min(100.0, stability))

    d["stability_score"] = stability
    if stability < 45:
        severity = "HIGH"
    elif stability < 70:
        severity = "MEDIUM"
    else:
        severity = "LOW"

    return {
        "stability": stability,
        "severity": severity,
        "ghost_mode": bool(d.get("ghost_mode", False)),
    }


# =========================
# RENDER
# =========================

def build_stat_chart(labels: List[str]) -> str:
    total = len(labels)
    if total <= 0:
        return (
            "║ 📈 BIỂU ĐỒ THỐNG KÊ\n"
            f"║ {LOW_LABEL:<5}: {'░'*12} 0.0%\n"
            f"║ {HIGH_LABEL:<5}: {'░'*12} 0.0%"
        )
    recent, mid, old = split_layers(labels)
    segs = [("GẦN", recent), ("GIỮA", mid), ("XA", old)]
    lines = ["║ 📈 BIỂU ĐỒ THỐNG KÊ"]
    for name, seg in segs:
        ratio = layer_ratio(seg)
        low_pct = ratio.get(LOW_LABEL, 0.5) * 100.0
        high_pct = ratio.get(HIGH_LABEL, 0.5) * 100.0
        spread = abs(low_pct - high_pct)
        winner = LOW_LABEL if low_pct >= high_pct else HIGH_LABEL
        lines.append(
            f"║ {name:<4} {LOW_LABEL[:1]} {make_bar(low_pct):<12} {low_pct:5.1f}% | {HIGH_LABEL[:1]} {make_bar(high_pct):<12} {high_pct:5.1f}% | Lệch {spread:4.1f}% | {winner}"
        )
    overall = layer_ratio(labels)
    overall_low = overall.get(LOW_LABEL, 0.5) * 100.0
    overall_high = overall.get(HIGH_LABEL, 0.5) * 100.0
    lines.append(
        f"║ TỔNG {LOW_LABEL[:1]} {make_bar(overall_low):<12} {overall_low:5.1f}% | {HIGH_LABEL[:1]} {make_bar(overall_high):<12} {overall_high:5.1f}%"
    )
    return "\n".join(lines)


def build_stats_message(report: Dict[str, Any], d: Dict[str, Any]) -> str:
    patterns = format_pattern_lines(report.get("patterns", []))
    hist = format_history(d.get("labels", []))
    total = report["total"]
    low_p = safe_div(report["low"] * 100.0, total) if total else 0.0
    high_p = safe_div(report["high"] * 100.0, total) if total else 0.0
    labels = d.get("labels", [])
    rec = layer_ratio(labels[-RECENT_WINDOW:])
    mid = layer_ratio(labels[max(0, len(labels) - RECENT_WINDOW - MID_WINDOW): max(0, len(labels) - RECENT_WINDOW)]) if len(labels) > RECENT_WINDOW else {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    old = layer_ratio(labels[:-RECENT_WINDOW] if len(labels) > RECENT_WINDOW else [])
    chart = build_stat_chart(labels)

    decision_hint = d.get("last_decision_hint", report.get("decision_hint", "TRUNG_LẬP"))
    decision_note = d.get("last_decision_note", report.get("decision_note", ""))
    fast_score = float(d.get("fast_break_score", 0.0) or 0.0)
    noise_score = float(d.get("noise_score", 0.0) or 0.0)
    final_label = d.get("last_prediction_label", "-")
    final_conf = int(d.get("last_prediction_conf", 0) or 0)

    return (
        "╔════════════════════════════╗\n"
        "║      ✅ BẢNG THỐNG KÊ      ║\n"
        "╠════════════════════════════╣\n"
        f"║ {LOW_LABEL}: {report['low']} ({low_p:.1f}%)\n"
        f"║ {HIGH_LABEL}: {report['high']} ({high_p:.1f}%)\n"
        f"║ Tổng: {report['total']} | Mode: {report['mode']}\n"
        f"║ Ghost: {'ON' if d.get('ghost_mode') else 'OFF'} | Clean: {d.get('last_clean_score', 0.0):.1f}\n"
        f"║ Stable: {d.get('stability_score', 100.0):.1f} | Errors: {d.get('error_count', 0)}\n"
        "╠════════════════════════════╣\n"
        f"║ Hướng    : {decision_hint}\n"
        f"║ Ý kiến   : {decision_note if decision_note else '-'}\n"
        f"║ Bẻ score : {fast_score:.1f}\n"
        f"║ Nhiễu    : {noise_score:.1f}\n"
        f"║ Chốt     : {final_label} | {final_conf}%\n"
        f"║ Cầu chính: {report.get('cau_type', '-')}\n"
        f"║ Chi tiết  : {report.get('cau_detail', '-')}\n"
        f"║ Cầu trắng : {report.get('white_type', '-')} | {report.get('white_score', 0):.0f}\n"
        f"║ Ghost     : {report.get('ghost_score', 0.0):.0f} | Drift {report.get('drift_js', 0.0):.2f}\n"
        f"║ Ám cũ     : {report.get('shift_score', 0.0):.2f}\n"
        f"║ Turn sớm  : {report.get('turn_score', 0.0):.2f}\n"
        f"║ Fresh     : {report.get('freshness', 0.0):.2f}\n"
        f"║ Recent    : {rec.get(LOW_LABEL, 0.5)*100:.1f}% / {rec.get(HIGH_LABEL, 0.5)*100:.1f}%\n"
        f"║ Mid       : {mid.get(LOW_LABEL, 0.5)*100:.1f}% / {mid.get(HIGH_LABEL, 0.5)*100:.1f}%\n"
        f"║ Old       : {old.get(LOW_LABEL, 0.5)*100:.1f}% / {old.get(HIGH_LABEL, 0.5)*100:.1f}%\n"
        f"║ Xen kẽ    : {'Có' if report['alternating'] else 'Không'} | {report['alt_ratio']:.2f}\n"
        f"║ Bệt       : {report['last_label'] if report['last_label'] else '—'} x{report['streak']}\n"
        f"║ Lặp       : {report['cycle_len'] if report['cycle_len'] else '—'} | {report['cycle_score']:.0f}\n"
        f"║ Rung      : {report['volatility']:.2f}\n"
        f"║ Ổn định   : {report['entropy']:.2f}\n"
        f"║ Pattern   : {patterns}\n"
        f"║ Lịch sử   : {hist}\n"
        f"║\n{chart}\n"
        "╚════════════════════════════╝"
    )
def build_analysis_message(report: Dict[str, Any], meta: Dict[str, Any], monitor: Dict[str, Any]) -> str:
    probs = meta.get("final_probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})
    return (
        "╔════════════════════════════╗\n"
        "║       🔍 PHÂN TÍCH         ║\n"
        "╠════════════════════════════╣\n"
        f"║ Dựa trên: {report.get('cau_type', '-')}\n"
        f"║ {report.get('cau_detail', '-')}\n"
        f"║ Trắng   : {report.get('white_type', '-')} | {report.get('white_score', 0):.0f}\n"
        f"║ Ghost   : {report.get('ghost_score', 0.0):.0f}\n"
        f"║ Turn sớm: {report.get('turn_score', 0.0):.2f}\n"
        f"║ Drift   : {report.get('drift_js', 0.0):.2f}\n"
        f"║ Giám sát: {monitor.get('severity', '-') } | {monitor.get('stability', 0):.1f}\n"
        f"║ Hướng   : {meta.get('decision_hint', '-')}\n"
        f"║ Ý kiến  : {meta.get('decision_note', '-')}\n"
        f"║ Bẻ score: {meta.get('fast_break_score', 0.0):.1f}\n"
        f"║ Nhiễu   : {meta.get('noise_score', 0.0):.1f}\n"
        f"║ Chốt    : {meta.get('final_label', '-')} | {meta.get('confidence', 0)}%\n"
        f"║ Xác suất: {format_prob_inline(probs)}\n"
        f"║ Cầu     : {meta.get('cau_type', '-')}\n"
        "╚════════════════════════════╝"
    )
def build_final_message(meta: Dict[str, Any]) -> str:
    return (
        f"DỰ ĐOÁN: {meta.get('final_label', '-')}\n"
        f"TỶ LỆ: {meta.get('confidence', 0)}%"
    )

def build_stage_message(step: int) -> str:
    if step == 1:
        return "✅ Bước 1: Đã cập nhật bảng thống kê."
    if step == 2:
        return "🔍 Bước 2: Đã phân tích bảng thống kê."
    return "🧠 Bước 3: Đang chốt dự đoán."


# =========================
# PIPELINE
# =========================

async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, nums: Optional[List[int]] = None) -> None:
    if not update.message:
        return

    chat_id = get_key(update)
    async with STATE_LOCK:
        d = ensure_state(await load_user(chat_id))

        entries: List[Tuple[int, str]] = []
        if nums:
            for n in nums:
                label = map_value(n)
                d["values"].append(n)
                d["labels"].append(label)
                d["history"].append({"value": n, "label": label, "source": "real", "conf": 1.0})
                entries.append((n, label))
                d["monitor_log"].append("input_append")
                update_pattern_memory_in_memory(d)

            rebuild_counters(d)
            trim_state_memory(d)

        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)
        report = analyze_sequence(d)
        meta = final_decision(report, d)
        monitor = monitor_ai(d, report, meta)

        await persist_snapshot(chat_id, d, entries)

    await update.message.reply_text(build_stage_message(1))
    await update.message.reply_text(build_stats_message(report, d))
    await update.message.reply_text(build_stage_message(2))
    await update.message.reply_text(build_analysis_message(report, meta, monitor))
    if cleanup.get("cleaned"):
        await update.message.reply_text(cleanup.get("message", "🧹 Đã dọn dữ liệu cũ."))
    await update.message.reply_text(build_final_message(meta))


# =========================
# COMMANDS
# =========================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        async with DB_LOCK:
            def _work():
                with db_connect() as conn:
                    conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
                    conn.execute("DELETE FROM patterns WHERE chat_id = ?", (chat_id,))
                    conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
                    conn.commit()
            await run_db_work(_work)
        users.pop(chat_id, None)
        await update.message.reply_text("🔄 Đã reset chat hiện tại.")
    except Exception as e:
        logger.exception("reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi reset")


async def factory_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        async with DB_LOCK:
            def _work():
                with db_connect() as conn:
                    conn.execute("DELETE FROM history")
                    conn.execute("DELETE FROM patterns")
                    conn.execute("DELETE FROM users")
                    conn.commit()
            await run_db_work(_work)
        users.clear()
        await update.message.reply_text("🧼 Đã xóa sạch toàn bộ dữ liệu.")
    except Exception as e:
        logger.exception("factory_reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi factory reset")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = ensure_state(await load_user(chat_id))
        report = analyze_sequence(d)
        await update.message.reply_text(build_stats_message(report, d))
    except Exception as e:
        logger.exception("stats failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xem thống kê")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = ensure_state(await load_user(chat_id))
        report = analyze_sequence(d)
        meta = final_decision(report, d)
        monitor = monitor_ai(d, report, meta)
        await update.message.reply_text(
            "🔧 DEBUG\n"
            f"ghost_mode: {d.get('ghost_mode')}\n"
            f"stability: {monitor.get('stability', 0):.1f}\n"
            f"severity: {monitor.get('severity')}\n"
            f"mode: {report.get('mode')}\n"
            f"cau: {report.get('cau_type')}\n"
            f"white: {report.get('white_type')}\n"
            f"streak: {report.get('streak')}\n"
            f"cycle: {report.get('cycle_len')}\n"
            f"last_clean: {d.get('last_clean_score', 0.0):.1f}\n"
            f"errors: {d.get('error_count', 0)}"
        )
    except Exception as e:
        logger.exception("debug failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi debug")


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return
        chat_id = get_key(update)
        d = ensure_state(await load_user(chat_id))
        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)
        await persist_snapshot(chat_id, d, [])
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
            f"Quy đổi: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}.\n"
            "Bot chốt theo cầu + nhịp gần nhất, ưu tiên nhận diện sớm để giảm trễ."
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
        d = ensure_state(await load_user(chat_id))
        repair_state(d)

        report = analyze_sequence(d)
        cleanup = anti_ghost_cleanup(d, report)
        repair_state(d)
        report = analyze_sequence(d)
        meta = final_decision(report, d)
        monitor = monitor_ai(d, report, meta)

        await update.message.reply_text(build_stage_message(1))
        await update.message.reply_text(build_stats_message(report, d))
        await update.message.reply_text(build_stage_message(2))
        await update.message.reply_text(build_analysis_message(report, meta, monitor))
        if cleanup.get("cleaned"):
            await update.message.reply_text(cleanup.get("message", "🧹 Đã dọn dữ liệu cũ."))
        await update.message.reply_text(build_final_message(meta))
    except Exception as e:
        logger.exception("ai_cmd failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi AI kết luận")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ai_cmd(update, context)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return
        nums = parse_input(update.message.text)
        if not nums:
            return
        await process_chat(update, context, nums)
    except Exception as e:
        logger.exception("handle failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xử lý dữ liệu")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Global handler caught error: %s", context.error)
    err = context.error
    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after + 1)
    elif isinstance(err, (TimedOut, NetworkError, TelegramError)):
        await asyncio.sleep(1.0)
    try:
        if getattr(update, "message", None):
            await update.message.reply_text("⚠️ Có lỗi tạm thời, bot đã tự giữ an toàn.")
    except Exception:
        pass


# =========================
# MAIN
# =========================

def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_error_handler(global_error_handler)
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


def run_bot_forever():
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.exception("Bot crashed, restarting in 5s: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot_forever()
