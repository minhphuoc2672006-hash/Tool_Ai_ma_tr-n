import os
import re
import json
import math
import time
import sqlite3
import asyncio
import logging
from io import BytesIO
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from PIL import Image, ImageEnhance, ImageDraw
    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageEnhance = None
    ImageDraw = None
    PIL_AVAILABLE = False

from telegram import Update
from telegram.error import Conflict, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters


# ===================== CONFIG =====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tai_xiu_bot_deep")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "ai_state.db")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

THRESHOLD = int(os.getenv("THRESHOLD", "11"))
LOW_LABEL = os.getenv("LOW_LABEL", "Xá»u")
HIGH_LABEL = os.getenv("HIGH_LABEL", "TÃ i")

RECENT_CACHE = int(os.getenv("RECENT_CACHE", "500"))
HISTORY_ANALYSIS_LIMIT = int(os.getenv("HISTORY_ANALYSIS_LIMIT", "0"))
MAX_KEEP_HISTORY = int(os.getenv("MAX_KEEP_HISTORY", "0"))
MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))
MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "6"))

ANALYSIS_WINDOW_MIN = int(os.getenv("ANALYSIS_WINDOW_MIN", "15"))
ANALYSIS_WINDOW_MAX = int(os.getenv("ANALYSIS_WINDOW_MAX", "35"))

MIN_PREDICTION_DATA = int(os.getenv("MIN_PREDICTION_DATA", "6"))
MIN_FINAL_CONFIDENCE = int(os.getenv("MIN_FINAL_CONFIDENCE", "55"))

ROBOT_ANALYZE_DELAY = float(os.getenv("ROBOT_ANALYZE_DELAY", "1.2"))
ROBOT_SOURCE_PATH = os.getenv("ROBOT_SOURCE_PATH", "robot.jpg")
ROBOT_IMAGE_PATH = os.getenv("ROBOT_IMAGE_PATH", "robot.jpg")
ROBOT_ANIM_PATH = os.getenv("ROBOT_ANIM_PATH", "robot_anim.gif")

ai_enabled_default = os.getenv("AI_ENABLED_DEFAULT", "1") != "0"

if not TOKEN:
    raise RuntimeError("Thiáº¿u TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
users: Dict[int, Dict[str, Any]] = {}


# ===================== BASIC HELPERS =====================
def is_admin(update: Update) -> bool:
    user = getattr(update, "effective_user", None)
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)

async def admin_only(update: Update) -> bool:
    if is_admin(update):
        return True
    if getattr(update, "message", None):
        await update.message.reply_text("â Bot nÃ y chá» dÃ nh cho admin.")
    return False

def get_key(update: Update) -> int:
    return update.effective_chat.id

def map_value(n: int) -> str:
    return HIGH_LABEL if n >= THRESHOLD else LOW_LABEL

def opposite_label(label: str) -> str:
    return HIGH_LABEL if label == LOW_LABEL else LOW_LABEL

def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0

def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)

def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    return list(seq[-limit:]) if limit > 0 and len(seq) > limit else list(seq)

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

def run_length_encode(labels: List[str]) -> List[Tuple[str, int]]:
    if not labels:
        return []
    out: List[Tuple[str, int]] = []
    cur = labels[0]
    count = 1
    for x in labels[1:]:
        if x == cur:
            count += 1
        else:
            out.append((cur, count))
            cur = x
            count = 1
    out.append((cur, count))
    return out

def recent_ratio(labels: List[str], window: int) -> Dict[str, float]:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if not tail:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(tail)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: c[LOW_LABEL] / total, HIGH_LABEL: c[HIGH_LABEL] / total}

def alternating_tail(labels: List[str], window: int = 6) -> Tuple[bool, float]:
    tail = labels[-window:] if len(labels) >= window else labels[:]
    if len(tail) < 4:
        return False, 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    ratio = changes / (len(tail) - 1)
    return all(tail[i] != tail[i - 1] for i in range(1, len(tail))), ratio

def entropy_score(labels: List[str], window: int = 20) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    c = Counter(tail)
    total = len(tail)
    ent = 0.0
    for v in c.values():
        p = v / total
        ent -= p * math.log2(p)
    return ent

def volatility_score(labels: List[str], window: int = 12) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    return changes / (len(tail) - 1)

def rolling_mean(values: List[float], window: int) -> List[float]:
    if not values:
        return []
    out = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        seg = values[start:i + 1]
        out.append(sum(seg) / len(seg))
    return out


# ===================== DB =====================
def rotate_broken_db() -> None:
    if not os.path.exists(DB_FILE):
        return
    broken_name = f"{DB_FILE}.broken.{int(time.time())}"
    try:
        os.replace(DB_FILE, broken_name)
        logger.warning("DB há»ng, ÄÃ£ Äá»i tÃªn sang %s", broken_name)
    except Exception:
        try:
            os.remove(DB_FILE)
        except Exception:
            pass

def db_connect() -> sqlite3.Connection:
    last_err: Optional[Exception] = None
    for attempt in range(2):
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.DatabaseError as e:
            last_err = e
            logger.exception("DB connect failed: %s", e)
            if attempt == 0:
                rotate_broken_db()
                continue
    raise RuntimeError(f"KhÃ´ng thá» má» DB: {last_err}")

async def run_db_work(fn):
    return await asyncio.to_thread(fn)

def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS chat_state (
        chat_id INTEGER PRIMARY KEY,
        state_json TEXT NOT NULL,
        updated_at INTEGER NOT NULL DEFAULT (unixepoch())
    );

    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        raw_value INTEGER NOT NULL,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (unixepoch())
    );

    CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);
    """
    try:
        with db_connect() as conn:
            conn.executescript(schema)
            conn.commit()
    except sqlite3.DatabaseError as e:
        logger.exception("init_db failed, recreating DB: %s", e)
        rotate_broken_db()
        with db_connect() as conn:
            conn.executescript(schema)
            conn.commit()

def prune_history(conn: sqlite3.Connection, chat_id: int, keep_limit: int) -> None:
    if keep_limit <= 0:
        return
    row = conn.execute(
        "SELECT id FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (chat_id, max(0, keep_limit - 1)),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM history WHERE chat_id = ? AND id < ?", (chat_id, int(row["id"])))

async def append_history(chat_id: int, items: List[Tuple[int, str]]) -> None:
    if not items:
        return
    def _work():
        try:
            with db_connect() as conn:
                for raw_value, label in items:
                    conn.execute(
                        "INSERT INTO history (chat_id, raw_value, label) VALUES (?, ?, ?)",
                        (chat_id, int(raw_value), str(label)),
                    )
                prune_history(conn, chat_id, MAX_KEEP_HISTORY)
                conn.commit()
        except Exception as e:
            logger.exception("append_history failed: %s", e)
    async with DB_LOCK:
        await run_db_work(_work)

async def load_history_rows(chat_id: int, limit: int = HISTORY_ANALYSIS_LIMIT) -> List[Tuple[int, str]]:
    def _work():
        try:
            with db_connect() as conn:
                if limit and limit > 0:
                    rows = conn.execute(
                        "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                        (chat_id, limit),
                    ).fetchall()
                    rows = list(reversed(rows))
                else:
                    rows = conn.execute(
                        "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id ASC",
                        (chat_id,),
                    ).fetchall()
                return [(int(r["raw_value"]), str(r["label"])) for r in rows]
        except Exception as e:
            logger.exception("load_history_rows failed: %s", e)
            return []
    async with DB_LOCK:
        return await run_db_work(_work)


# ===================== STATE =====================
def new_state() -> Dict[str, Any]:
    return {
        "values": [],
        "labels": [],
        "dice_rolls": [],
        "total": 0,
        "low_count": 0,
        "high_count": 0,
        "last_prediction_label": None,
        "last_prediction_conf": 0,
        "last_prediction_result": "CHÆ¯A RÃ",
        "prediction_total": 0,
        "prediction_hits": 0,
        "prediction_misses": 0,
        "current_correct_streak": 0,
        "current_wrong_streak": 0,
        "max_correct_streak": 0,
        "max_wrong_streak": 0,
        "model_accuracy": {"pattern": 50, "markov": 50, "chart": 50, "structure": 50},
        "last_note": "",
        "last_structure": "CHÆ¯A Äá»¦ Dá»® LIá»U",
        "last_mode": "NORMAL",
        "last_model_predictions": {},
        "last_model_predictions_raw": {},
        "last_filter_notes": "",
        "last_gate_status": "CHá»",
        "last_gate_reason": "ChÆ°a kiá»m tra",
        "last_detected_pattern": "",
        "last_detected_hint": None,
        "last_chart_label": "",
        "last_chart_conf": 0,
        "last_dice_label": "",
        "last_dice_conf": 0,
        "pattern_confirm_sig": "",
        "pattern_confirm_count": 0,
        "last_analysis_window": 0,
        "last_analysis_total": 0,
        "recent_outcomes": [],
        "last_analysis_trace": "",
        "ai_enabled": ai_enabled_default,
        "hash_enabled": True,
        "hash_waiting": False,
        "hash_last_prediction": None,
        "hash_last_conf": 0,
        "hash_last_input": "",
        "last_real_value": None,
        "hash_total": 0,
        "hash_win": 0,
        "hash_lose": 0,
        "last_dice_summary": "",
        "dice_deep_summary": "",
        "pattern_bank": {},
        "last_long_pattern": {},
        "last_short_pattern": {},
        "last_signal_clear": False,
        "last_signal_reason": "",
    }

def rebuild_counters_from_labels(d: Dict[str, Any], labels: List[str]) -> None:
    d["low_count"] = labels.count(LOW_LABEL)
    d["high_count"] = labels.count(HIGH_LABEL)
    d["total"] = len(labels)

def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), RECENT_CACHE)
    d["labels"] = _safe_tail(d.get("labels", []), RECENT_CACHE)
    d["dice_rolls"] = _safe_tail(d.get("dice_rolls", []), RECENT_CACHE)

def repair_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        d = new_state()
    for k in ("values", "labels", "recent_outcomes", "dice_rolls"):
        if not isinstance(d.get(k), list):
            d[k] = []
    if not isinstance(d.get("model_accuracy"), dict):
        d["model_accuracy"] = {"pattern": 50, "markov": 50, "chart": 50, "structure": 50}
    defaults = new_state()
    for k, v in defaults.items():
        d.setdefault(k, v)
    n = min(len(d["values"]), len(d["labels"]))
    d["values"] = d["values"][-n:] if n else []
    d["labels"] = d["labels"][-n:] if n else []
    trim_state_memory(d)
    rebuild_counters_from_labels(d, d.get("labels", []))
    d["ai_enabled"] = bool(d.get("ai_enabled", True))
    d["hash_enabled"] = bool(d.get("hash_enabled", True))
    d["hash_waiting"] = bool(d.get("hash_waiting", False))
    d["hash_last_prediction"] = d.get("hash_last_prediction")
    d["hash_last_conf"] = int(d.get("hash_last_conf", 0))
    d["hash_last_input"] = str(d.get("hash_last_input", ""))
    d["last_real_value"] = d.get("last_real_value")
    d["hash_total"] = int(d.get("hash_total", 0))
    d["hash_win"] = int(d.get("hash_win", 0))
    d["hash_lose"] = int(d.get("hash_lose", 0))
    return d

async def load_state(chat_id: int, force_reload: bool = False) -> Dict[str, Any]:
    if not force_reload and chat_id in users:
        return repair_state(users[chat_id])

    def _work():
        try:
            with db_connect() as conn:
                return conn.execute("SELECT state_json FROM chat_state WHERE chat_id = ?", (chat_id,)).fetchone()
        except Exception as e:
            logger.exception("load_state DB failed: %s", e)
            return None

    async with DB_LOCK:
        row = await run_db_work(_work)

    state = new_state()
    if row:
        try:
            loaded = json.loads(row["state_json"])
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.exception("load_state JSON failed: %s", e)

    state = repair_state(state)
    users[chat_id] = state
    trim_cache()
    return state

async def save_state(chat_id: int, state: Dict[str, Any]) -> None:
    state = repair_state(state)
    def _work():
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_state (chat_id, state_json, updated_at)
                    VALUES (?, ?, unixepoch())
                    ON CONFLICT(chat_id) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (chat_id, json.dumps(state, ensure_ascii=False, default=str)),
                )
                prune_history(conn, chat_id, MAX_KEEP_HISTORY)
                conn.commit()
        except Exception as e:
            logger.exception("save_state failed: %s", e)
    async with DB_LOCK:
        await run_db_work(_work)


# ===================== INPUT PARSER =====================
def parse_input(text: str) -> Tuple[List[int], List[List[int]]]:
    text = (text or "").strip()
    if not text:
        return [], []
    tokens = re.findall(r"\d+", text)
    if not tokens:
        return [], []

    if len(tokens) == 1:
        tok = tokens[0]
        if len(tok) == 3 and all(ch in "123456" for ch in tok):
            faces = [int(ch) for ch in tok]
            return [sum(faces)], [faces]
        try:
            n = int(tok)
            return ([n] if n >= 0 else []), []
        except Exception:
            return [], []

    if all(1 <= int(x) <= 6 for x in tokens):
        totals: List[int] = []
        dice_rolls: List[List[int]] = []
        i = 0
        while i + 3 <= len(tokens):
            faces = [int(v) for v in tokens[i:i + 3]]
            totals.append(sum(faces))
            dice_rolls.append(faces)
            i += 3
        while i < len(tokens):
            n = int(tokens[i])
            if n >= 0:
                totals.append(n)
            i += 1
        return totals[:MAX_INPUT_NUMS], dice_rolls[:MAX_INPUT_NUMS]

    out: List[int] = []
    for x in tokens:
        try:
            n = int(x)
            if n >= 0:
                out.append(n)
        except Exception:
            continue
    return out[:MAX_INPUT_NUMS], []

HEX_HASH_RE = re.compile(r"(?=.*[a-fA-F])[0-9a-fA-F]{16,}$")
def is_hash_like(text: str) -> bool:
    return bool(HEX_HASH_RE.fullmatch((text or "").strip()))


# ===================== ANALYTICS =====================
def choose_analysis_window(labels_full: List[str]) -> int:
    n = len(labels_full)
    if n <= ANALYSIS_WINDOW_MIN:
        return n
    probe_size = min(n, 24)
    probe = labels_full[-probe_size:]
    probe_vol = volatility_score(probe, window=min(12, len(probe)))
    probe_ent = entropy_score(probe, window=min(20, len(probe)))
    _, streak = current_streak(probe)

    if probe_vol >= 0.70 or probe_ent >= 0.98 or streak <= 1:
        win = 30
    elif probe_vol >= 0.55 or probe_ent >= 0.90:
        win = 24
    elif probe_vol <= 0.32 and probe_ent <= 0.78 and streak >= 4:
        win = 15
    else:
        win = 20
    win = max(ANALYSIS_WINDOW_MIN, min(ANALYSIS_WINDOW_MAX, win))
    return min(win, n)

def detect_repeat_block(labels: List[str], max_block: int = 3) -> Optional[Dict[str, Any]]:
    tail = labels[-12:] if len(labels) > 12 else labels[:]
    for block in range(1, max_block + 1):
        if len(tail) >= block * 2:
            a = tail[-(block * 2):-block]
            b = tail[-block:]
            if a == b:
                return {"name": f"Láº¶P KHá»I {block}", "detail": f"Khá»i {block} máº«u gáº§n nháº¥t Äang láº·p", "score": min(78 + block * 4, 92), "hint": a[0] if a else None}
    return None

def detect_motif_repeat(labels: List[str], max_motif: int = 8) -> Optional[Dict[str, Any]]:
    tail = labels[-40:] if len(labels) > 40 else labels[:]
    if len(tail) < 4:
        return None
    best: Optional[Dict[str, Any]] = None
    for m in range(1, min(max_motif, len(tail) // 2) + 1):
        motif = tail[-m:]
        reps = 1
        idx = len(tail) - m
        while idx - m >= 0 and tail[idx - m:idx] == motif:
            reps += 1
            idx -= m
        if reps >= 2:
            if m == 1:
                name = "Bá»T"
                hint = motif[0]
                score = min(70 + reps * 6, 95)
            elif m == 2 and motif[0] != motif[1]:
                name = "XEN Káº¼"
                hint = opposite_label(tail[-1])
                score = min(76 + reps * 4, 94)
            else:
                name = f"Láº¶P MáºªU {m}"
                hint = motif[0]
                score = min(72 + reps * 5, 94)
            cand = {"name": name, "detail": f"Máº«u {m} láº·p {reps} láº§n", "score": score, "hint": hint}
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best

def detect_explicit_pair_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if len(labels) < 4:
        return out
    a, b, c, d = labels[-4], labels[-3], labels[-2], labels[-1]
    if a != b and a == c and b == d:
        out.append({"name": "1-1", "detail": "Máº«u luÃ¢n phiÃªn 1-1", "score": 90, "hint": a})
    if a == b and c == d and a != c:
        out.append({"name": "2-2", "detail": "Máº«u chia cáº·p 2-2", "score": 86, "hint": a})
    if len(labels) >= 6:
        t = labels[-6:]
        if t[0] == t[2] == t[4] and t[1] == t[3] == t[5] and t[0] != t[1]:
            out.append({"name": "XEN Káº¼ SÃU", "detail": "LuÃ¢n phiÃªn Äá»u trong 6 máº«u gáº§n nháº¥t", "score": 94, "hint": t[0]})
    return out

def detect_run_cycle(labels: List[str]) -> Optional[Dict[str, Any]]:
    runs = run_length_encode(labels)
    if len(runs) < 4:
        return None
    a1, l1 = runs[-4]
    b1, m1 = runs[-3]
    a2, l2 = runs[-2]
    b2, m2 = runs[-1]
    if a1 == a2 and b1 == b2 and l1 == l2 and m1 == m2 and a1 != b1:
        return {"name": f"Cáº¦U {l1}-{m1}", "detail": f"Chu ká»³ 2 khá»i láº·p: {l1}-{m1}", "score": min(82 + (l1 + m1) * 2, 95), "hint": a2}
    return None

def detect_bias(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 12:
        return None
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)
    gap6 = abs(r6[LOW_LABEL] - r6[HIGH_LABEL])
    gap12 = abs(r12[LOW_LABEL] - r12[HIGH_LABEL])
    gap24 = abs(r24[LOW_LABEL] - r24[HIGH_LABEL])

    if gap6 >= 0.50:
        winner = LOW_LABEL if r6[LOW_LABEL] > r6[HIGH_LABEL] else HIGH_LABEL
        return {"name": "NGHIÃNG NHáº¸", "detail": f"ÄuÃ´i 6 nghiÃªng vá» {winner}", "score": 72, "hint": winner}
    if gap12 >= 0.35:
        winner = LOW_LABEL if r12[LOW_LABEL] > r12[HIGH_LABEL] else HIGH_LABEL
        return {"name": "NGHIÃNG", "detail": f"ÄuÃ´i 12 nghiÃªng vá» {winner}", "score": 78, "hint": winner}
    if gap24 >= 0.25:
        winner = LOW_LABEL if r24[LOW_LABEL] > r24[HIGH_LABEL] else HIGH_LABEL
        return {"name": "XU HÆ¯á»NG", "detail": f"24 máº«u gáº§n ÄÃ¢y nghiÃªng vá» {winner}", "score": 80, "hint": winner}
    if gap24 < 0.10:
        return {"name": "CÃN Báº°NG", "detail": "Hai phÃ­a gáº§n nhÆ° ngang nhau", "score": 65, "hint": None}
    return None

def detect_reversal(labels: List[str]) -> Optional[Dict[str, Any]]:
    tail = labels[-18:] if len(labels) > 18 else labels[:]
    if len(tail) < 8:
        return None
    recent = tail[-6:]
    prev = tail[-12:-6] if len(tail) >= 12 else tail[:-6]
    if len(prev) < 4:
        return None

    recent_high = recent.count(HIGH_LABEL) / len(recent)
    prev_high = prev.count(HIGH_LABEL) / len(prev)
    recent_changes = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i - 1])
    prev_changes = sum(1 for i in range(1, len(prev)) if prev[i] != prev[i - 1])
    recent_vol = recent_changes / max(1, len(recent) - 1)
    prev_vol = prev_changes / max(1, len(prev) - 1)
    diff = abs(recent_high - prev_high)

    if diff >= 0.50:
        hint = HIGH_LABEL if recent_high > prev_high else LOW_LABEL
        return {"name": "Äáº¢O CHIá»U", "detail": f"6 máº«u gáº§n nháº¥t nghiÃªng máº¡nh vá» {hint}", "score": 86, "hint": hint}
    if diff >= 0.30 and abs(recent_vol - prev_vol) >= 0.20:
        hint = HIGH_LABEL if recent_high > prev_high else LOW_LABEL
        return {"name": "Äáº¢O CHIá»U", "detail": f"Nhá»p gáº§n ÄÃ¢y Äá»i pha vá» {hint}", "score": 80, "hint": hint}
    return None

def detect_wave(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 8:
        return None
    tail = labels[-8:]
    s = "".join("T" if x == HIGH_LABEL else "X" for x in tail)
    if s in {"TTXXTTXX", "XXTTXXTT"}:
        return {"name": "SÃNG Äá»U", "detail": f"Nhá»p sÃ³ng {s}", "score": 90, "hint": tail[-1]}
    if s in {"TXXTTXXT", "XTTXXTTX"}:
        return {"name": "SÃNG Äáº¢O", "detail": f"Nhá»p sÃ³ng Äáº£o {s}", "score": 86, "hint": tail[-1]}
    return None

def detect_transition_phase(labels: List[str]) -> Optional[Dict[str, Any]]:
    tail = labels[-18:] if len(labels) > 18 else labels[:]
    if len(tail) < 10:
        return None
    prev = tail[-16:-8] if len(tail) >= 16 else tail[:-8]
    recent = tail[-8:]
    if len(prev) < 4 or len(recent) < 4:
        return None
    prev_ratio = safe_div(prev.count(HIGH_LABEL), len(prev))
    recent_ratio = safe_div(recent.count(HIGH_LABEL), len(recent))
    _, prev_streak = current_streak(prev)
    _, recent_streak = current_streak(recent)
    diff = abs(recent_ratio - prev_ratio)
    if diff >= 0.45:
        winner = HIGH_LABEL if recent_ratio > prev_ratio else LOW_LABEL
        return {"name": "CHUYá»N PHA", "detail": f"Äoáº¡n gáº§n nháº¥t nghiÃªng máº¡nh vá» {winner} vÃ  lá»ch rÃµ so vá»i Äoáº¡n trÆ°á»c", "score": 88, "hint": winner}
    if diff >= 0.30 and abs(recent_streak - prev_streak) >= 2:
        winner = HIGH_LABEL if recent_ratio >= prev_ratio else LOW_LABEL
        return {"name": "CHUYá»N PHA", "detail": f"Nhá»p gáº§n ÄÃ¢y Äá»i pha sang {winner}", "score": 82, "hint": winner}
    return None

def detect_complex_cycle(labels: List[str]) -> Optional[Dict[str, Any]]:
    tail = labels[-30:] if len(labels) > 30 else labels[:]
    if len(tail) < 10:
        return None
    runs = run_length_encode(tail)
    if len(runs) < 4:
        return None
    if len(runs) >= 4:
        r = [c for _, c in runs[-4:]]
        lbls = [lab for lab, _ in runs[-4:]]
        if r[0] == r[2] and r[1] == r[3] and r[0] != r[1] and lbls[0] != lbls[1] and lbls[2] != lbls[3]:
            return {"name": "Cáº¦U PHá»¨C Há»¢P", "detail": f"Run-length {r[0]}-{r[1]} láº·p theo cáº·p", "score": min(92, 86 + r[0] + r[1]), "hint": tail[-1]}
    if len(runs) >= 6:
        r = [c for _, c in runs[-6:]]
        if r[0] == r[3] and r[1] == r[4] and r[2] == r[5] and len({r[0], r[1], r[2]}) >= 2:
            return {"name": "CHU Ká»² Má» Rá»NG", "detail": f"Nhá»p {r[0]}-{r[1]}-{r[2]} láº·p", "score": min(94, 84 + sum(r[-3:])), "hint": tail[-1]}
    return None

def detect_nested_zigzag(labels: List[str]) -> Optional[Dict[str, Any]]:
    tail = labels[-14:] if len(labels) > 14 else labels[:]
    if len(tail) < 8:
        return None
    s = "".join("T" if x == HIGH_LABEL else "X" for x in tail)
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    changes_ratio = changes / max(1, len(tail) - 1)
    if changes_ratio >= 0.72 and ("TT" in s or "XX" in s):
        return {"name": "XEN Káº¼ BIáº¾N THá»", "detail": f"Chuá»i Äá»i liÃªn tá»¥c nhÆ°ng cÃ³ nhá»p gÃ£y nháº¹: {s}", "score": 84, "hint": opposite_label(tail[-1])}
    return None

def detect_false_trap(labels: List[str]) -> Optional[Dict[str, Any]]:
    tail = labels[-12:] if len(labels) > 12 else labels[:]
    if len(tail) < 6:
        return None
    for side in (HIGH_LABEL, LOW_LABEL):
        other = opposite_label(side)
        if tail[-6:-3] == [side, side, side] and tail[-3] == other and tail[-2:] == [side, side]:
            return {"name": "Cáº¦U BáºªY", "detail": f"Cá»¥m {side} x3 bá» chá»c 1 nhá»p rá»i há»i láº¡i", "score": 89, "hint": None}
    return None

def detect_cycle_repeat(labels: List[str], max_window: int = 12) -> Optional[Dict[str, Any]]:
    runs = run_length_encode(labels)
    if len(runs) < 4:
        return None
    tail_runs = runs[-max_window:] if len(runs) > max_window else runs[:]
    counts = [count for _, count in tail_runs]
    if len(counts) < 4:
        return None
    best: Optional[Dict[str, Any]] = None
    upper = min(4, len(counts) // 2)
    for size in range(2, upper + 1):
        motif = counts[-size:]
        reps = 1
        idx = len(counts) - size
        while idx - size >= 0 and counts[idx - size:idx] == motif:
            reps += 1
            idx -= size
        if reps >= 2:
            label = "Cáº¦U Láº¶P"
            if size == 2:
                label = "Cáº¦U 2 NHá»P"
            elif size == 3:
                label = "Cáº¦U 3 NHá»P"
            score = min(95, 76 + reps * 6 + size * 2)
            cand = {"name": label, "detail": f"Nhá»p run-length {'-'.join(map(str, motif))} láº·p {reps} láº§n", "score": score, "hint": tail_runs[-1][0], "pattern_counts": motif, "signature": "-".join(map(str, motif))}
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best

def detect_dynamic_patterns(labels: List[str], max_len: int = 6) -> List[Dict[str, Any]]:
    runs = [c for _, c in run_length_encode(labels)]
    patterns: List[Dict[str, Any]] = []
    if len(runs) < 4:
        return patterns
    for size in range(3, min(max_len, len(runs)) + 1):
        recent = tuple(runs[-size:])
        match = 0
        total = 0
        for i in range(len(runs) - size):
            if tuple(runs[i:i + size]) == recent:
                total += 1
                if i + size < len(runs):
                    match += 1
        if total >= 2:
            score = int((match / total) * 100)
            patterns.append({
                "name": f"Cáº¦U SÃU {recent}",
                "detail": f"Pattern Äá»ng {recent} xuáº¥t hiá»n {total} láº§n",
                "score": min(95, score + 10),
                "hint": labels[-1] if labels else None,
                "type": "dynamic",
                "signature": "-".join(map(str, recent)),
                "pattern_counts": list(recent),
            })
    return patterns

def _dedupe_patterns(patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for p in sorted(patterns, key=lambda x: x.get("score", 0), reverse=True):
        sig = (p.get("name"), p.get("hint"), p.get("detail"), p.get("signature"))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(p)
    return out

def detect_all_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    patterns: List[Dict[str, Any]] = []
    patterns.extend(detect_dynamic_patterns(labels))
    for item in (
        *detect_explicit_pair_patterns(labels),
        detect_cycle_repeat(labels),
        detect_motif_repeat(labels, 8),
        detect_complex_cycle(labels),
        detect_transition_phase(labels),
        detect_nested_zigzag(labels),
        detect_false_trap(labels),
        detect_run_cycle(labels),
        detect_repeat_block(labels, 3),
        detect_reversal(labels),
        detect_bias(labels),
        detect_wave(labels),
    ):
        if item:
            patterns.append(item)

    last, streak = current_streak(labels)
    if last in (LOW_LABEL, HIGH_LABEL) and streak >= 3:
        patterns.append({"name": "Bá»T", "detail": f"{last} x{streak}", "score": min(68 + streak * 6, 95), "hint": last, "signature": f"BET-{last}-{streak}"})

    alt, alt_ratio = alternating_tail(labels, 6)
    if alt and alt_ratio >= 0.80 and len(labels) >= 6:
        patterns.append({"name": "XEN Káº¼", "detail": "Chuá»i Äá»i liÃªn tá»¥c", "score": 88, "hint": opposite_label(labels[-1]), "signature": "ALT-6"})

    return _dedupe_patterns(patterns)[:20]

def deep_structure_analysis(labels: List[str]) -> Dict[str, Any]:
    runs = run_length_encode(labels)
    counts = [count for _, count in runs]
    if not labels:
        return {"cycle_signature": "-", "repeat_signature": "-", "repeat_count": 0, "chaos_score": 0, "stability_score": 50, "structure_hint": "CHÆ¯A Äá»¦ Dá»® LIá»U", "repeat_note": "ChÆ°a cÃ³ dá»¯ liá»u"}

    changes = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    change_rate = safe_div(changes, max(1, len(labels) - 1))
    ent = entropy_score(labels, min(20, len(labels)))
    vol = volatility_score(labels, min(12, len(labels)))

    if counts:
        mean_len = sum(counts) / len(counts)
        variance = sum((c - mean_len) ** 2 for c in counts) / len(counts)
        irregularity = math.sqrt(variance) / mean_len if mean_len else 0.0
        cycle_signature = "-".join(map(str, counts[-8:]))
    else:
        irregularity = 0.0
        cycle_signature = "-"

    repeat_signature = "-"
    repeat_count = 0
    if len(counts) >= 4:
        for size in range(2, min(4, len(counts) // 2) + 1):
            motif = counts[-size:]
            reps = 1
            idx = len(counts) - size
            while idx - size >= 0 and counts[idx - size:idx] == motif:
                reps += 1
                idx -= size
            if reps >= 2:
                repeat_signature = "-".join(map(str, motif))
                repeat_count = reps
                break

    chaos_raw = (change_rate * 45.0) + (ent * 18.0) + (vol * 20.0) + (irregularity * 25.0)
    chaos_score = max(0, min(100, int(round(chaos_raw))))
    stability_score = max(0, min(100, 100 - chaos_score))

    if chaos_score >= 72:
        structure_hint = "Cáº¦U LOáº N"
        repeat_note = "Biáº¿n Äá»ng cao, Äá»i nhá»p liÃªn tá»¥c"
    elif repeat_count >= 2:
        structure_hint = "Cáº¦U Láº¶P"
        repeat_note = f"CÃ³ nhá»p láº·p {repeat_signature}"
    elif change_rate <= 0.25 and counts and max(counts) >= 4:
        structure_hint = "Cáº¦U Bá»T"
        repeat_note = "Xu hÆ°á»ng bá»t rÃµ"
    elif change_rate >= 0.65:
        structure_hint = "XEN Káº¼"
        repeat_note = "Äá»i tráº¡ng thÃ¡i nhanh"
    else:
        structure_hint = "Cáº¦U Há»N Há»¢P"
        repeat_note = "ChÆ°a tháº¥y nhá»p láº·p Äá»§ rÃµ"

    return {
        "cycle_signature": cycle_signature,
        "repeat_signature": repeat_signature,
        "repeat_count": repeat_count,
        "chaos_score": chaos_score,
        "stability_score": stability_score,
        "structure_hint": structure_hint,
        "repeat_note": repeat_note,
    }

def build_report(labels: List[str]) -> Dict[str, Any]:
    c = Counter(labels)
    last, streak = current_streak(labels)
    alt, alt_ratio = alternating_tail(labels, 6)
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)
    ent = entropy_score(labels, 20)
    vol = volatility_score(labels, 12)
    patterns = detect_all_patterns(labels)
    deep = deep_structure_analysis(labels)

    if len(labels) < 4:
        structure = "CHÆ¯A Äá»¦ Dá»® LIá»U"
        detail = "Cáº§n thÃªm káº¿t quáº£"
    elif patterns:
        structure = patterns[0]["name"]
        detail = patterns[0]["detail"]
    else:
        if deep.get("structure_hint") == "Cáº¦U LOáº N":
            structure = "Cáº¦U LOáº N"
            detail = deep.get("repeat_note", "Biáº¿n Äá»ng cao")
        elif deep.get("repeat_count", 0) >= 2:
            structure = deep.get("structure_hint", "Cáº¦U Láº¶P")
            detail = deep.get("repeat_note", "CÃ³ nhá»p láº·p")
        elif alt and alt_ratio >= 0.80:
            structure = "XEN Káº¼"
            detail = "Chuá»i Äá»i liÃªn tá»¥c"
        elif streak >= 4 and last in (LOW_LABEL, HIGH_LABEL):
            structure = "Bá»T"
            detail = f"{last} x{streak}"
        elif vol >= 0.65:
            structure = "CHUYá»N PHA"
            detail = "Nhá»p Äang Äá»i nhanh"
        elif ent <= 0.85:
            structure = "á»N Äá»NH"
            detail = "Máº«u gáº§n ÄÃ¢y khÃ¡ Äá»u"
        else:
            structure = "TRUNG TÃNH"
            detail = "ChÆ°a cÃ³ tÃ­n hiá»u quÃ¡ rÃµ"

    return {
        "total": len(labels),
        "low": c.get(LOW_LABEL, 0),
        "high": c.get(HIGH_LABEL, 0),
        "labels": labels,
        "last_label": last,
        "streak": streak,
        "alternating": alt,
        "alt_ratio": alt_ratio,
        "structure": structure,
        "detail": detail,
        "recent_6": r6,
        "recent_12": r12,
        "recent_24": r24,
        "entropy": ent,
        "volatility": vol,
        "patterns": patterns,
        "cycle_signature": deep.get("cycle_signature", "-"),
        "repeat_signature": deep.get("repeat_signature", "-"),
        "repeat_count": deep.get("repeat_count", 0),
        "chaos_score": deep.get("chaos_score", 0),
        "stability_score": deep.get("stability_score", 50),
        "structure_hint": deep.get("structure_hint", "TRUNG TÃNH"),
        "repeat_note": deep.get("repeat_note", "-"),
    }

def advanced_metrics(labels: List[str]) -> Dict[str, Any]:
    if not labels:
        return {"max_streak": 0, "r10_high": 0.5, "r20_high": 0.5, "momentum": 0.0, "noise": 0.0, "reversal": 0.0}
    max_streak = 1
    cur = 1
    for i in range(1, len(labels)):
        if labels[i] == labels[i - 1]:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1
    last10 = labels[-10:]
    last20 = labels[-20:]
    r10 = safe_div(last10.count(HIGH_LABEL), len(last10)) if last10 else 0.5
    r20 = safe_div(last20.count(HIGH_LABEL), len(last20)) if last20 else 0.5
    momentum = r10 - r20
    changes = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    noise = safe_div(changes, len(labels))
    reversal = abs(momentum) * 100
    return {"max_streak": max_streak, "r10_high": r10, "r20_high": r20, "momentum": momentum, "noise": noise, "reversal": reversal}

def extract_chart_features(labels: List[str]) -> Dict[str, Any]:
    if not labels:
        return {"trend": 0.0, "trend_label": "TRUNG TÃNH", "reversal_rate": 0.0, "smoothness": 0.0, "max_streak": 0, "last_value": None, "last_5_high": 0.5, "prev_5_high": 0.5}
    ys = [1 if x == HIGH_LABEL else 0 for x in labels]
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = 0.0
    if denom:
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    trend = slope * 100.0
    trend_label = f"NGHIÃNG {HIGH_LABEL}" if trend > 0.08 else f"NGHIÃNG {LOW_LABEL}" if trend < -0.08 else "TRUNG TÃNH"
    reversals = sum(1 for i in range(1, n) if ys[i] != ys[i - 1])
    reversal_rate = reversals / max(1, n - 1)
    smoothness = 1.0 - reversal_rate
    max_streak = 1
    cur = 1
    for i in range(1, n):
        if ys[i] == ys[i - 1]:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1
    last_5 = ys[-5:] if n >= 5 else ys[:]
    prev_5 = ys[-10:-5] if n >= 10 else ys[:-5] if n > 5 else []
    return {
        "trend": trend,
        "trend_label": trend_label,
        "reversal_rate": reversal_rate,
        "smoothness": smoothness,
        "max_streak": max_streak,
        "last_value": ys[-1],
        "last_5_high": safe_div(sum(last_5), len(last_5)) if last_5 else 0.5,
        "prev_5_high": safe_div(sum(prev_5), len(prev_5)) if prev_5 else 0.5,
    }

def analyze_dice_deep(dice_rolls: List[List[int]]) -> Dict[str, Any]:
    if not dice_rolls:
        return {
            "total_rolls": 0,
            "face_counts": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
            "die_counts": [{}, {}, {}],
            "avg_total": 0.0,
            "avg_faces": [0.0, 0.0, 0.0],
            "std_faces": [0.0, 0.0, 0.0],
            "recent_delta": [0.0, 0.0, 0.0],
            "recent_volatility": [0.0, 0.0, 0.0],
            "dominant_die": None,
            "dominant_face": None,
            "pattern": "CHÆ¯A Äá»¦ Dá»® LIá»U",
            "detail": "ChÆ°a cÃ³ dá»¯ liá»u xÃ­ ngáº§u",
        }

    valid = [r for r in dice_rolls if isinstance(r, list) and len(r) == 3]
    if not valid:
        return {
            "total_rolls": 0,
            "face_counts": {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0},
            "die_counts": [{}, {}, {}],
            "avg_total": 0.0,
            "avg_faces": [0.0, 0.0, 0.0],
            "std_faces": [0.0, 0.0, 0.0],
            "recent_delta": [0.0, 0.0, 0.0],
            "recent_volatility": [0.0, 0.0, 0.0],
            "dominant_die": None,
            "dominant_face": None,
            "pattern": "CHÆ¯A Äá»¦ Dá»® LIá»U",
            "detail": "ChÆ°a cÃ³ dá»¯ liá»u xÃ­ ngáº§u",
        }

    face_counts = Counter()
    die_counts = [Counter(), Counter(), Counter()]
    totals = []
    for roll in valid:
        totals.append(sum(roll))
        for idx, face in enumerate(roll[:3]):
            face_counts[face] += 1
            die_counts[idx][face] += 1

    total_rolls = len(valid)
    avg_total = safe_div(sum(totals), total_rolls)
    avg_faces = []
    std_faces = []
    recent_delta = []
    recent_volatility = []

    for idx in range(3):
        vals = [r[idx] for r in valid]
        mean = safe_div(sum(vals), len(vals))
        avg_faces.append(mean)
        variance = safe_div(sum((v - mean) ** 2 for v in vals), len(vals))
        std_faces.append(math.sqrt(variance))
        recent_delta.append(vals[-1] - vals[-2] if len(vals) >= 2 else 0.0)
        recent_slice = vals[-10:] if len(vals) > 10 else vals[:]
        if len(recent_slice) >= 3:
            diff = sum(abs(recent_slice[i] - recent_slice[i - 1]) for i in range(1, len(recent_slice))) / (len(recent_slice) - 1)
        else:
            diff = 0.0
        recent_volatility.append(diff)

    hot_faces = sorted(face_counts.items(), key=lambda x: (x[1], x[0]), reverse=True)[:3]
    dominant_die = None
    dominant_face = None
    best_gap = -1.0
    for idx in range(3):
        cnt = die_counts[idx]
        if not cnt:
            continue
        most_face, most_count = cnt.most_common(1)[0]
        p = safe_div(most_count, sum(cnt.values()))
        if p > best_gap:
            best_gap = p
            dominant_die = idx + 1
            dominant_face = most_face

    if avg_total >= 11.2:
        pattern = "Tá»NG CAO"
        detail = "Tá»ng 3 viÃªn thiÃªn vá» TÃ i"
    elif avg_total <= 10.8:
        pattern = "Tá»NG THáº¤P"
        detail = "Tá»ng 3 viÃªn thiÃªn vá» Xá»u"
    else:
        pattern = "TRUNG TÃNH"
        detail = "Tá»ng 3 viÃªn khÃ¡ cÃ¢n báº±ng"

    return {
        "total_rolls": total_rolls,
        "face_counts": dict(sorted(face_counts.items())),
        "die_counts": [dict(sorted(c.items())) for c in die_counts],
        "avg_total": avg_total,
        "avg_faces": avg_faces,
        "std_faces": std_faces,
        "recent_delta": recent_delta,
        "recent_volatility": recent_volatility,
        "hot_faces": hot_faces,
        "dominant_die": dominant_die,
        "dominant_face": dominant_face,
        "pattern": pattern,
        "detail": detail,
    }

def pattern_signature(pattern: Dict[str, Any]) -> str:
    return f"{pattern.get('name','')}|{pattern.get('hint','')}|{int(pattern.get('score',0))}"

def update_pattern_bank(state: Dict[str, Any], patterns: List[Dict[str, Any]]) -> None:
    bank = state.setdefault("pattern_bank", {})
    if not isinstance(bank, dict):
        bank = {}
        state["pattern_bank"] = bank
    for p in patterns:
        sig = str(p.get("signature") or pattern_signature(p))
        entry = bank.get(sig, {"seen": 0, "best_score": 0, "last_score": 0, "last_name": "", "last_hint": None})
        entry["seen"] = int(entry.get("seen", 0)) + 1
        entry["best_score"] = max(int(entry.get("best_score", 0)), int(p.get("score", 0)))
        entry["last_score"] = int(p.get("score", 0))
        entry["last_name"] = str(p.get("name", ""))
        entry["last_hint"] = p.get("hint")
        bank[sig] = entry
    if len(bank) > 300:
        keys = list(bank.keys())[-300:]
        state["pattern_bank"] = {k: bank[k] for k in keys}

def update_confirm_state(state: Dict[str, Any], report: Dict[str, Any]) -> Tuple[bool, str]:
    patterns = report.get("patterns", [])
    if not patterns:
        state["pattern_confirm_sig"] = ""
        state["pattern_confirm_count"] = 0
        return False, "ChÆ°a cÃ³ cáº§u rÃµ rÃ ng"
    top = patterns[0]
    sig = pattern_signature(top)
    if state.get("pattern_confirm_sig") == sig:
        state["pattern_confirm_count"] = int(state.get("pattern_confirm_count", 0)) + 1
    else:
        state["pattern_confirm_sig"] = sig
        state["pattern_confirm_count"] = 1
    confirmed = int(top.get("score", 0)) >= 76 or int(state["pattern_confirm_count"]) >= 2
    return confirmed, f"{top.get('name', '-')} ({int(top.get('score', 0))}%) | confirm={state['pattern_confirm_count']}"

def prediction_gate(labels: List[str], report: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    patterns = report.get("patterns", [])
    total = int(report.get("total", 0))
    if state is not None:
        clear_signal, reason = update_confirm_state(state, report)
        state["last_gate_status"] = "Cáº¦U RÃ" if clear_signal else "THEO DÃI"
        state["last_gate_reason"] = reason
    else:
        clear_signal, reason = False, "ChÆ°a kiá»m tra"
    if not patterns:
        return False, "ChÆ°a tháº¥y cáº§u rÃµ"
    top = patterns[0]
    top_score = int(top.get("score", 0))
    chaos = int(report.get("chaos_score", 0))
    structure_hint = str(report.get("structure_hint", ""))
    if top_score >= 70 and chaos <= 72 and structure_hint != "Cáº¦U LOáº N" and total >= MIN_PREDICTION_DATA:
        return True, f"Cáº§u rÃµ: {top.get('name', '-')}"
    if top_score >= 58 and chaos <= 82:
        return False, f"Äang theo dÃµi: {top.get('name', '-')} ({top_score}%)"
    return False, f"Cáº§u cÃ²n yáº¿u: {top.get('name', '-')} ({top_score}%)"

def predict_pattern(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        primary = patterns[0]
        hint = primary.get("hint")
        name = primary.get("name", "")
        if hint in (LOW_LABEL, HIGH_LABEL):
            return {"label": hint, "confidence": min(90, int(primary.get("score", 60)) + 2), "source": f"pattern:{name}"}
    if len(labels) < 4:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "pattern"}
    if labels[-1] == labels[-2] == labels[-3]:
        return {"label": labels[-1], "confidence": 66, "source": "pattern"}
    if labels[-1] != labels[-2]:
        return {"label": labels[-2], "confidence": 56, "source": "pattern"}
    return {"label": labels[-1], "confidence": 54, "source": "pattern"}

def predict_markov(labels: List[str]) -> Dict[str, Any]:
    if len(labels) < 3:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "markov"}
    trans = {LOW_LABEL: Counter(), HIGH_LABEL: Counter()}
    for a, b in zip(labels[:-1], labels[1:]):
        if a in trans and b in (LOW_LABEL, HIGH_LABEL):
            trans[a][b] += 1
    last = labels[-1]
    if last in trans and trans[last]:
        label = trans[last].most_common(1)[0][0]
        total = sum(trans[last].values())
        confidence = 55 + int(25 * (trans[last][label] / total))
        return {"label": label, "confidence": min(90, confidence), "source": "markov"}
    if len(labels) >= 4:
        key = tuple(labels[-2:])
        bigram_map: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
        for i in range(len(labels) - 2):
            k = (labels[i], labels[i + 1])
            nxt = labels[i + 2]
            if nxt in (LOW_LABEL, HIGH_LABEL):
                bigram_map[k][nxt] += 1
        if key in bigram_map and bigram_map[key]:
            label = bigram_map[key].most_common(1)[0][0]
            total = sum(bigram_map[key].values())
            confidence = 53 + int(27 * (bigram_map[key][label] / total))
            return {"label": label, "confidence": min(88, confidence), "source": "markov"}
    return {"label": labels[-1], "confidence": 52, "source": "markov"}

def predict_structure(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        top = patterns[0]
        hint = top.get("hint")
        name = top.get("name", "")
        strong_same = {"Bá»T", "Bá»T Sá»M", "Láº¶P MáºªU 1", "Láº¶P KHá»I 1", "XEN Káº¼ Sá»M", "XEN Káº¼", "XEN Káº¼ SÃU", "1-1", "2-2", "SÃNG Äá»U", "Cáº¦U PHá»¨C Há»¢P", "CHU Ká»² Má» Rá»NG"}
        if hint in (LOW_LABEL, HIGH_LABEL) and name in strong_same:
            return {"label": hint, "confidence": min(90, int(top.get("score", 60)) + 1), "source": f"structure:{name}"}
        if name.startswith("Cáº¦U ") or name.startswith("Láº¶P MáºªU") or name.startswith("Láº¶P KHá»I") or name.startswith("SÃNG") or name.startswith("CHU Ká»²") or name.startswith("CHUYá»N PHA") or name.startswith("XEN Káº¼"):
            return {"label": hint if hint in (LOW_LABEL, HIGH_LABEL) else (labels[-1] if labels else LOW_LABEL), "confidence": min(90, int(top.get("score", 60)) + 1), "source": f"structure:{name}"}
        if name == "CÃN Báº°NG":
            return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 52, "source": "structure:balance"}
    if len(labels) < 2:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "structure"}
    return {"label": labels[-1], "confidence": 54, "source": "structure"}

def predict_chart(labels: List[str], report: Dict[str, Any], adv: Dict[str, Any]) -> Dict[str, Any]:
    if not labels:
        return {"label": LOW_LABEL, "confidence": 50, "source": "chart"}
    trend = float(adv.get("trend", 0.0))
    trend_label = str(adv.get("trend_label", "TRUNG TÃNH"))
    smoothness = float(adv.get("smoothness", 0.0))
    reversal_rate = float(adv.get("reversal_rate", 0.0))
    last_5_high = float(adv.get("last_5_high", 0.5))
    prev_5_high = float(adv.get("prev_5_high", 0.5))
    momentum = float(adv.get("momentum", 0.0))
    last_label = labels[-1]
    if trend > 0.10 or (last_5_high - prev_5_high) >= 0.15 or momentum > 0.08:
        label = HIGH_LABEL
        base = 62
        if trend > 0.20:
            base += 10
        if last_5_high > 0.60:
            base += 8
        if smoothness >= 0.60:
            base += 4
        if reversal_rate < 0.35:
            base += 4
    elif trend < -0.10 or (prev_5_high - last_5_high) >= 0.15 or momentum < -0.08:
        label = LOW_LABEL
        base = 62
        if trend < -0.20:
            base += 10
        if last_5_high < 0.40:
            base += 8
        if smoothness >= 0.60:
            base += 4
        if reversal_rate < 0.35:
            base += 4
    else:
        label = last_label
        base = 52
        if trend_label != "TRUNG TÃNH":
            base += 3
        if 0.40 <= last_5_high <= 0.60:
            base += 2
    if trend_label == f"NGHIÃNG {HIGH_LABEL}" and label == HIGH_LABEL:
        base += 4
    if trend_label == f"NGHIÃNG {LOW_LABEL}" and label == LOW_LABEL:
        base += 4
    confidence = max(50, min(90, int(base)))
    return {"label": label, "confidence": confidence, "source": "chart"}

def predict_dice(dice_rolls: List[List[int]], summary: Dict[str, Any]) -> Dict[str, Any]:
    if not dice_rolls:
        return {"label": LOW_LABEL, "confidence": 50, "source": "dice"}
    total_rolls = int(summary.get("total_rolls", 0))
    avg_total = float(summary.get("avg_total", 0.0))
    avg_faces = summary.get("avg_faces", [0.0, 0.0, 0.0])
    vol = summary.get("recent_volatility", [0.0, 0.0, 0.0])
    if avg_total >= 11.6:
        label = HIGH_LABEL
    elif avg_total <= 10.4:
        label = LOW_LABEL
    else:
        label = HIGH_LABEL if sum(avg_faces) / 3.0 >= 3.5 else LOW_LABEL
    confidence = 54
    if total_rolls >= 8:
        confidence += 6
    if abs(avg_total - 10.5) >= 1.0:
        confidence += 6
    if any(v >= 4.0 for v in avg_faces):
        confidence += 4
    if any(v >= 1.2 for v in vol):
        confidence += 4
    return {"label": label, "confidence": min(90, confidence), "source": "dice"}

def update_model_accuracy(state: Dict[str, Any], predictions: Dict[str, Dict[str, Any]], actual: str) -> None:
    state.setdefault("model_accuracy", {"pattern": 50, "markov": 50, "chart": 50, "structure": 50})
    for name, pred in predictions.items():
        old = int(state["model_accuracy"].get(name, 50))
        old = old + 1 if pred.get("label") == actual else old - 1
        state["model_accuracy"][name] = max(1, min(99, old))

def update_prediction_feedback(state: Dict[str, Any], actual_label: str) -> None:
    pred = state.get("last_prediction_label")
    if pred not in (LOW_LABEL, HIGH_LABEL):
        return
    state["prediction_total"] = int(state.get("prediction_total", 0)) + 1
    recent_outcomes = state.setdefault("recent_outcomes", [])
    if pred == actual_label:
        state["prediction_hits"] = int(state.get("prediction_hits", 0)) + 1
        state["last_prediction_result"] = "ÄÃNG"
        state["current_correct_streak"] = int(state.get("current_correct_streak", 0)) + 1
        state["current_wrong_streak"] = 0
        state["max_correct_streak"] = max(int(state.get("max_correct_streak", 0)), int(state["current_correct_streak"]))
        recent_outcomes.append("WIN")
    else:
        state["prediction_misses"] = int(state.get("prediction_misses", 0)) + 1
        state["last_prediction_result"] = "SAI"
        state["current_wrong_streak"] = int(state.get("current_wrong_streak", 0)) + 1
        state["current_correct_streak"] = 0
        state["max_wrong_streak"] = max(int(state.get("max_wrong_streak", 0)), int(state["current_wrong_streak"]))
        recent_outcomes.append("LOSE")
    if len(recent_outcomes) > 200:
        del recent_outcomes[:-200]

def filter_predictions_for_decision(predictions: Dict[str, Dict[str, Any]], state: Dict[str, Any], report: Dict[str, Any], adv: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    filtered: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    chart = predictions.get("chart", {})
    structure = predictions.get("structure", {})
    chart_label = chart.get("label")
    structure_label = structure.get("label")
    top_pattern = report.get("patterns", [{}])[0] if report.get("patterns") else {}
    pattern_label = top_pattern.get("hint")
    pattern_score = int(top_pattern.get("score", 0))
    thresholds = {"chart": 56, "dice": 58, "pattern": 54, "markov": 55, "structure": 56}

    for name, pred in predictions.items():
        label = pred.get("label")
        conf = float(pred.get("confidence", 50))
        keep = False
        if name == "chart":
            if conf >= 60 and float(adv.get("smoothness", 0.0)) >= 0.40:
                keep = True
            elif conf >= thresholds[name]:
                keep = True
        elif name == "dice":
            avg_total = float(report.get("avg_total", 0.0)) if isinstance(report, dict) else 0.0
            if conf >= 60 and (avg_total >= 11.0 or avg_total <= 10.0 or max(adv.get("recent_volatility", [0.0, 0.0, 0.0])) >= 0.9):
                keep = True
            elif conf >= thresholds[name]:
                keep = True
        else:
            if conf >= thresholds.get(name, 55):
                keep = True
        if not keep and label in (chart_label, structure_label, pattern_label):
            if float(chart.get("confidence", 0)) >= 62 or float(structure.get("confidence", 0)) >= 62 or pattern_score >= 70:
                keep = True
        if not keep and chart_label in (LOW_LABEL, HIGH_LABEL) and label == chart_label:
            if name in {"dice", "pattern", "markov"} and float(chart.get("confidence", 0)) >= 68:
                keep = True
        if keep:
            filtered[name] = pred
        else:
            notes.append(f"drop:{name}({int(conf)}%)")
    if not filtered and predictions:
        best_name = max(predictions, key=lambda k: float(predictions[k].get("confidence", 0)))
        filtered[best_name] = predictions[best_name]
        notes.append(f"fallback:{best_name}")
    notes.append(f"kept={len(filtered)}/{len(predictions)}")
    return filtered, notes

def meta_decision(predictions: Dict[str, Dict[str, Any]], state: Dict[str, Any], report: Dict[str, Any], adv: Dict[str, Any], clear_signal: bool) -> Dict[str, Any]:
    filtered_predictions, filter_notes = filter_predictions_for_decision(predictions, state, report, adv)
    model_acc = state.get("model_accuracy", {})
    vote: Dict[str, float] = defaultdict(float)
    model_scores: Dict[str, float] = {}
    volatility = float(report.get("volatility", 0.0))
    smoothness = float(adv.get("smoothness", 0.0))
    reversal_rate = float(adv.get("reversal_rate", 0.0))
    entropy = float(report.get("entropy", 0.0))
    trend = float(adv.get("trend", 0.0))
    last_5_high = float(adv.get("last_5_high", 0.5))
    prev_5_high = float(adv.get("prev_5_high", 0.5))

    for name, pred in filtered_predictions.items():
        label = pred.get("label")
        conf = float(pred.get("confidence", 50))
        acc = float(model_acc.get(name, 50))
        score = conf * (0.88 + acc / 130.0)
        if name == "chart":
            score *= 1.22
            if abs(trend) >= 0.18:
                score *= 1.12
            elif abs(trend) >= 0.10:
                score *= 1.06
            if abs(last_5_high - prev_5_high) >= 0.15:
                score *= 1.08
            if smoothness >= 0.60:
                score *= 1.04
        elif name == "dice":
            score *= 1.14
            avg_faces = adv.get("avg_faces", [0.0, 0.0, 0.0])
            if abs(float(sum(avg_faces)) / 3.0 - 3.5) >= 0.3:
                score *= 1.06
            if max(adv.get("recent_volatility", [0.0, 0.0, 0.0])) >= 1.1:
                score *= 1.05
        elif name == "structure":
            score *= 1.08
        elif name == "pattern":
            score *= 1.05
        else:
            score *= 1.02
        if smoothness >= 0.70:
            score *= 1.03
        elif reversal_rate >= 0.45:
            score *= 0.94
        if volatility > 0.80:
            score *= 0.92
        elif entropy < 1.0:
            score *= 1.02
        model_scores[name] = score
        vote[label] += score

    if not vote:
        fallback = predictions.get("chart") or predictions.get("dice") or next(iter(predictions.values()), None)
        if not fallback:
            return {"model": "none", "final_label": LOW_LABEL, "confidence": 50, "scores": {}, "filtered_predictions": {}, "filter_notes": filter_notes, "filtered_count": 0, "raw_count": len(predictions)}
        return {"model": "fallback", "final_label": fallback.get("label", LOW_LABEL), "confidence": max(50, int(fallback.get("confidence", 50)) - 2), "scores": {fallback.get("label", LOW_LABEL): float(fallback.get("confidence", 50))}, "filtered_predictions": filtered_predictions, "filter_notes": filter_notes, "filtered_count": len(filtered_predictions), "raw_count": len(predictions)}

    best_label = max(vote, key=vote.get)
    total = sum(vote.values())
    top = vote[best_label]
    top_ratio = top / total if total else 0.5
    agreement = sum(1 for p in filtered_predictions.values() if p.get("label") == best_label)
    strongest_conf = max((int(p.get("confidence", 50)) for p in filtered_predictions.values()), default=50)
    best_model = max(model_scores, key=model_scores.get)
    confidence = int(50 + top_ratio * 36 + (agreement - 1) * 5 + (strongest_conf - 50) * 0.16)
    if filtered_predictions.get("chart", {}).get("label") == best_label and float(filtered_predictions.get("chart", {}).get("confidence", 0)) >= 66:
        confidence += 5
    if report.get("structure") in {"CÃN Báº°NG", "TRUNG TÃNH", "CHÆ¯A Äá»¦ Dá»® LIá»U"}:
        confidence -= 2
    if abs(trend) >= 0.18:
        confidence += 2
    confidence = max(0, min(confidence, 90 if clear_signal else 65))
    return {
        "model": best_model,
        "final_label": best_label,
        "confidence": confidence,
        "scores": dict(vote),
        "agreement": agreement,
        "top_ratio": top_ratio,
        "filtered_predictions": filtered_predictions,
        "filter_notes": filter_notes,
        "filtered_count": len(filtered_predictions),
        "raw_count": len(predictions),
    }

def analyze_state_from_labels(state: Dict[str, Any], labels_full: List[str], dice_rolls_full: Optional[List[List[int]]] = None) -> Dict[str, Any]:
    window = choose_analysis_window(labels_full)
    short_labels = labels_full[-window:] if len(labels_full) > window else labels_full[:]
    long_cut = min(len(labels_full), max(window * 3, 60))
    long_labels = labels_full[-long_cut:] if len(labels_full) > long_cut else labels_full[:]

    short_report = build_report(short_labels)
    long_report = build_report(long_labels)
    short_adv = advanced_metrics(short_labels)
    short_adv.update(extract_chart_features(short_labels))
    long_adv = advanced_metrics(long_labels)
    dice_rolls = dice_rolls_full[-window:] if dice_rolls_full and len(dice_rolls_full) > window else (dice_rolls_full[:] if dice_rolls_full else [])
    dice_summary = analyze_dice_deep(dice_rolls)

    merged_patterns = _dedupe_patterns((long_report.get("patterns", [])[:10] or []) + (short_report.get("patterns", [])[:10] or []))
    report = dict(short_report)
    report["patterns"] = merged_patterns
    report["long_structure"] = long_report.get("structure", "-")
    report["long_detail"] = long_report.get("detail", "-")
    report["short_structure"] = short_report.get("structure", "-")
    report["short_detail"] = short_report.get("detail", "-")
    report["history_total"] = len(labels_full)

    state["dice_deep_summary"] = (
        f"D1:{dice_summary['avg_faces'][0]:.2f}Â±{dice_summary['std_faces'][0]:.2f}, "
        f"D2:{dice_summary['avg_faces'][1]:.2f}Â±{dice_summary['std_faces'][1]:.2f}, "
        f"D3:{dice_summary['avg_faces'][2]:.2f}Â±{dice_summary['std_faces'][2]:.2f}"
        if dice_summary.get("total_rolls", 0) else "ChÆ°a cÃ³ dá»¯ liá»u"
    )
    state["last_analysis_window"] = window
    state["last_analysis_total"] = len(labels_full)
    state["last_dice_summary"] = f"{dice_summary.get('pattern', '-')}: {dice_summary.get('detail', '-')}"
    state["last_dice_rolls"] = len(dice_rolls)

    clear_signal, reason = prediction_gate(short_labels, report, state)
    state["last_signal_clear"] = bool(clear_signal)
    state["last_signal_reason"] = reason

    top_pattern = report["patterns"][0] if report.get("patterns") else {}
    state["last_detected_pattern"] = top_pattern.get("name", "")
    state["last_detected_hint"] = top_pattern.get("hint")
    state["last_structure"] = report.get("short_structure", report.get("structure", "CHÆ¯A Äá»¦ Dá»® LIá»U"))

    predictions = {
        "pattern": predict_pattern(short_labels, report),
        "markov": predict_markov(short_labels),
        "structure": predict_structure(short_labels, report),
        "chart": predict_chart(short_labels, report, short_adv),
        "dice": predict_dice(dice_rolls, dice_summary),
    }

    state["last_mined_patterns"] = merged_patterns[:10]
    update_pattern_bank(state, merged_patterns)

    if clear_signal:
        meta = meta_decision(predictions, state, report, short_adv, clear_signal=True)
        meta["confidence"] = cap_confidence(meta.get("confidence", 55), clear_signal=True)
        state["last_analysis_trace"] = (
            f"vote={meta.get('model', 'none')} | "
            f"agree={meta.get('agreement', 0)}/{len(predictions)} | "
            f"top={meta.get('top_ratio', 0):.2f} | "
            f"scores={meta.get('scores', {})} | "
            f"gate={state.get('last_gate_status', 'CHá»')}: {state.get('last_gate_reason', '-')}"
        )
        if meta["confidence"] < MIN_FINAL_CONFIDENCE:
            state["last_prediction_label"] = None
            state["last_prediction_conf"] = 0
            state["last_prediction_result"] = "THEO DÃI"
            state["last_note"] = f"{reason} | chÆ°a Äá»§ cháº¯c Äá» chá»t"
        else:
            state["last_prediction_label"] = meta["final_label"]
            state["last_prediction_conf"] = meta["confidence"]
            state["last_prediction_result"] = "CHá» Káº¾T QUáº¢"
            state["last_note"] = f"Model: {meta['model']} | Consensus: {meta['agreement']}/{len(predictions)}"
    else:
        meta = {
            "model": "watch",
            "final_label": "CHÆ¯A RÃ",
            "confidence": cap_confidence(56 + int(report.get("stability_score", 50) * 0.1), clear_signal=False),
            "scores": {},
            "agreement": 0,
            "top_ratio": 0.0,
            "filtered_predictions": predictions,
            "filter_notes": ["watch-mode"],
            "filtered_count": len(predictions),
            "raw_count": len(predictions),
        }
        state["last_analysis_trace"] = f"watch-mode | gate={reason}"
        state["last_prediction_label"] = None
        state["last_prediction_conf"] = 0
        state["last_prediction_result"] = "THEO DÃI"
        state["last_note"] = reason

    state["last_chart_label"] = predictions.get("chart", {}).get("label", "")
    state["last_chart_conf"] = int(predictions.get("chart", {}).get("confidence", 0))
    state["last_dice_label"] = predictions.get("dice", {}).get("label", "")
    state["last_dice_conf"] = int(predictions.get("dice", {}).get("confidence", 0))
    state["last_mode"] = "READY" if len(short_labels) >= MIN_ANALYSIS_LEN else "NORMAL"
    state["last_model_predictions"] = predictions

    return {
        "report": report,
        "adv": short_adv,
        "long_adv": long_adv,
        "chart_features": extract_chart_features(short_labels),
        "dice_summary": dice_summary,
        "predictions": predictions,
        "meta": meta,
        "allowed": True,
        "reason": reason,
        "window": window,
        "labels_used": short_labels,
        "dice_used": dice_rolls,
        "clear_signal": clear_signal,
    }


# ===================== HASH MODEL =====================
def analyze_hash_model(hash_str: str) -> Dict[str, Any]:
    s = (hash_str or "").strip().lower()
    last3 = s[-3:]
    value = int(last3, 16)
    is_odd = (value % 2 == 1)
    label = HIGH_LABEL if is_odd else LOW_LABEL
    confidence = 68
    return {"label": label, "confidence": confidence, "text": f"ð Dá»± ÄoÃ¡n: {label}"}

def build_hash_stats_message(state: Dict[str, Any]) -> str:
    total = int(state.get("hash_total", 0))
    win = int(state.get("hash_win", 0))
    lose = int(state.get("hash_lose", 0))
    wr = (win / total * 100.0) if total else 0.0
    waiting = "CÃ" if state.get("hash_waiting") else "KHÃNG"
    pred = state.get("hash_last_prediction") or "-"
    conf = int(state.get("hash_last_conf", 0))
    return (
        "ð Thá»ng kÃª:\n"
        f"â¢ Tá»ng: {total}\n"
        f"â¢ Tháº¯ng: {win}\n"
        f"â¢ Thua: {lose}\n"
        f"â¢ Winrate: {wr:.1f}%\n"
        f"â¢ Chá» xÃ¡c nháº­n: {waiting}\n"
        f"â¢ Dá»± ÄoÃ¡n gáº§n nháº¥t: {pred} ({conf}%)\n"
    )

def build_hash_confirmation_message(state: Dict[str, Any], actual_value: int, actual_label: str) -> str:
    pred = state.get("hash_last_prediction") or "-"
    ok = pred in (LOW_LABEL, HIGH_LABEL) and pred == actual_label
    status = "ÄÃNG" if ok else "SAI"
    return (
        "ð¸ Káº¿t quáº£ vÃ²ng trÆ°á»c:\n"
        f"â¢ Dá»± ÄoÃ¡n: {pred}\n"
        f"â¢ Thá»±c táº¿: {actual_value} => {actual_label}\n"
        f"ð {status}\n"
    )


# ===================== CHARTS =====================
def build_chart_summary(report: Dict[str, Any], adv: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> str:
    return (
        f"Tá»ng: {report.get('total',0)} | {LOW_LABEL}: {report.get('low',0)} | {HIGH_LABEL}: {report.get('high',0)}\n"
        f"Cáº¥u trÃºc: {report.get('structure','-')}\n"
        f"Window: {state.get('last_analysis_window', '-') if state else '-'}\n"
        f"Chi tiáº¿t: {report.get('detail','-')}\n"
        f"Bá»t max: {adv.get('max_streak',0)} | 10 gáº§n: {adv.get('r10_high',0.5)*100:.1f}% {HIGH_LABEL} | "
        f"20 gáº§n: {adv.get('r20_high',0.5)*100:.1f}% {HIGH_LABEL}\n"
        f"Momentum: {adv.get('momentum',0.0):.2f} | Trend: {adv.get('trend_label','TRUNG TÃNH')} ({adv.get('trend',0.0):.2f})\n"
        f"MÆ°á»£t: {adv.get('smoothness',0.0):.2f} | Äáº£o chiá»u: {adv.get('reversal',0.0):.1f}%\n"
        f"Entropy: {report.get('entropy',0.0):.2f} | Volatility: {report.get('volatility',0.0):.2f}\n"
        f"Cycle: {report.get('cycle_signature', '-')}\n"
        f"Repeat: {report.get('repeat_signature', '-')} x{report.get('repeat_count', 0)}\n"
        f"Loáº¡n/Stab: {report.get('chaos_score', 0)}/{report.get('stability_score', 50)}"
    )

def build_dice_chart_summary(summary: Dict[str, Any]) -> str:
    if not summary or int(summary.get("total_rolls", 0)) <= 0:
        return "ChÆ°a cÃ³ dá»¯ liá»u xÃ­ ngáº§u."
    avg_faces = summary.get("avg_faces", [0.0, 0.0, 0.0])
    std_faces = summary.get("std_faces", [0.0, 0.0, 0.0])
    recent_delta = summary.get("recent_delta", [0.0, 0.0, 0.0])
    recent_vol = summary.get("recent_volatility", [0.0, 0.0, 0.0])
    hot_faces = summary.get("hot_faces", [])
    hot_text = " / ".join(f"{face}:{count}" for face, count in hot_faces[:3]) if hot_faces else "-"
    return (
        f"Rolls: {summary.get('total_rolls', 0)} | "
        f"Avg total: {summary.get('avg_total', 0.0):.2f}\n"
        f"Avg die1/2/3: {avg_faces[0]:.2f} / {avg_faces[1]:.2f} / {avg_faces[2]:.2f}\n"
        f"Std die1/2/3: {std_faces[0]:.2f} / {std_faces[1]:.2f} / {std_faces[2]:.2f}\n"
        f"Î gáº§n nháº¥t: {recent_delta[0]:+.0f} / {recent_delta[1]:+.0f} / {recent_delta[2]:+.0f}\n"
        f"Biáº¿n Äá»ng ngáº¯n: {recent_vol[0]:.2f} / {recent_vol[1]:.2f} / {recent_vol[2]:.2f}\n"
        f"Hot faces: {hot_text}\n"
        f"Pattern: {summary.get('pattern', '-')}\n"
        f"Detail: {summary.get('detail', '-')}"
    )

def _point_color(label: str) -> str:
    return "gold" if label == HIGH_LABEL else "white"

def _point_edge(label: str) -> str:
    return "#3a2b00" if label == HIGH_LABEL else "#444444"

def build_bridge_chart_image(labels: List[str], report: Dict[str, Any], adv: Dict[str, Any], state: Optional[Dict[str, Any]] = None, limit: int = 0) -> Optional[BytesIO]:
    tail = labels[-limit:] if limit and len(labels) > limit else labels[:]
    if not tail:
        return None
    xs = list(range(1, len(tail) + 1))
    ys = [1 if x == HIGH_LABEL else 0 for x in tail]
    fig_w = max(14.0, min(38.0, 0.25 * len(tail) + 10.0))
    fig_h = 7.8 if len(tail) < 120 else 8.4
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=180)
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#11151c")

    for i in range(1, len(xs) + 1):
        ax.axvline(i, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhline(0, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhline(0.5, color="white", alpha=0.22, linewidth=1.2, linestyle="--", zorder=0)
    ax.axhline(1, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhspan(-0.02, 0.5, alpha=0.05, color="#ffffff", zorder=0)
    ax.axhspan(0.5, 1.02, alpha=0.05, color="#ffd700", zorder=0)

    ax.step(xs, ys, where="mid", linewidth=4.5, alpha=0.10, color="white", zorder=2)
    ax.step(xs, ys, where="mid", linewidth=2.8, alpha=0.18, color="#7db7ff", zorder=3)
    ax.step(xs, ys, where="mid", linewidth=2.0, alpha=0.95, color="#cfd8dc", zorder=4)
    ax.scatter(xs, ys, s=180, c=[_point_color(t) for t in tail], edgecolors=[_point_edge(t) for t in tail], linewidths=1.4, zorder=5)
    ax.scatter(xs, ys, s=80, c="none", edgecolors="black", linewidths=0.4, alpha=0.45, zorder=4)
    for x, y, t in zip(xs, ys, tail):
        txt_color = "#111111" if t == HIGH_LABEL else "#222222"
        ax.text(x, y, t, ha="center", va="center", fontsize=9, fontweight="bold", color=txt_color, zorder=6)
    if xs:
        ax.scatter([xs[-1]], [ys[-1]], s=320, c=["#ff4d4d"], edgecolors="white", linewidths=1.8, zorder=7)
        ax.text(xs[-1], ys[-1] + (0.11 if ys[-1] == 1 else -0.11), tail[-1], ha="center", va="center", fontsize=11, fontweight="bold", color="white", zorder=8)
    if len(ys) >= 3:
        ma_blue = rolling_mean(ys, 5)
        ma_yellow = rolling_mean(ys, 12)
        ax.plot(xs, ma_blue, linewidth=2.6, alpha=0.95, color="#4aa3ff", zorder=6)
        ax.plot(xs, ma_yellow, linewidth=2.4, alpha=0.95, color="#ffd84d", zorder=6)
        ax.fill_between(xs, ma_blue, ma_yellow, where=[a >= b for a, b in zip(ma_blue, ma_yellow)], alpha=0.06, color="#4aa3ff")
        ax.fill_between(xs, ma_blue, ma_yellow, where=[a < b for a, b in zip(ma_blue, ma_yellow)], alpha=0.05, color="#ffd84d")
    ax.set_ylim(-0.15, 1.15)
    ax.set_yticks([0, 1])
    ax.set_yticklabels([LOW_LABEL, HIGH_LABEL], fontsize=11, color="white")
    ax.set_xlabel("Máº«u gáº§n nháº¥t", fontsize=10, color="white")
    ax.set_ylabel("Tráº¡ng thÃ¡i", fontsize=10, color="white")
    ax.set_title("BIá»U Äá» Cáº¦U PHÃN TÃCH", fontsize=13, fontweight="bold", color="white")
    if len(xs) <= 14:
        ax.set_xticks(xs)
        ax.set_xticklabels([str(i) for i in xs], fontsize=9, color="white")
    else:
        step = max(1, len(xs) // 12)
        ticks = xs[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([str(i) for i in ticks], fontsize=9, color="white")
    for spine in ax.spines.values():
        spine.set_color("#6b7280")
        spine.set_alpha(0.35)
    ax.tick_params(colors="white")
    ax.grid(True, axis="y", alpha=0.14, linestyle="-")
    ax.text(0.02, 0.98, build_chart_summary(report, adv, state), transform=ax.transAxes, va="top", ha="left", fontsize=9, color="white", bbox=dict(boxstyle="round,pad=0.55", facecolor="#121826", alpha=0.92, edgecolor="#44506a"))
    top_patterns = report.get("patterns", [])[:4]
    quick = " / ".join(p.get("name", "") for p in top_patterns) if top_patterns else "TRUNG TÃNH"
    ax.text(0.98, 0.02, f"Cáº§u: {quick}", transform=ax.transAxes, va="bottom", ha="right", fontsize=9, color="white", bbox=dict(boxstyle="round,pad=0.35", facecolor="#1f2937", alpha=0.9, edgecolor="#6b7280"))
    ax.text(0.02, 0.02, "SÃ³ng xanh = ngáº¯n háº¡n | SÃ³ng vÃ ng = trung háº¡n", transform=ax.transAxes, va="bottom", ha="left", fontsize=8.5, color="white", bbox=dict(boxstyle="round,pad=0.30", facecolor="#1f2937", alpha=0.82, edgecolor="#6b7280"))
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf

def build_dice_chart_image(dice_rolls: List[List[int]], summary: Dict[str, Any]) -> Optional[BytesIO]:
    if not dice_rolls:
        return None
    valid = [r for r in dice_rolls if isinstance(r, list) and len(r) == 3]
    if not valid:
        return None
    n = len(valid)
    xs = list(range(1, n + 1))
    die1 = [r[0] for r in valid]
    die2 = [r[1] for r in valid]
    die3 = [r[2] for r in valid]
    totals = [sum(r) for r in valid]
    avg_total = rolling_mean(totals, 5)
    fig_w = max(14.0, min(38.0, 0.28 * n + 9.0))
    fig_h = 8.0
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=180)
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#11151c")
    ax.axhline(3.5, color="white", alpha=0.18, linestyle="--", linewidth=1.2)
    ax.axhline(10.5, color="white", alpha=0.22, linestyle="--", linewidth=1.5)
    ax.axhline(18.5, color="white", alpha=0.12, linestyle="--", linewidth=1.0)
    ax.plot(xs, die1, marker="o", linewidth=1.8, label="XÃ­ ngáº§u 1")
    ax.plot(xs, die2, marker="o", linewidth=1.8, label="XÃ­ ngáº§u 2")
    ax.plot(xs, die3, marker="o", linewidth=1.8, label="XÃ­ ngáº§u 3")
    ax.plot(xs, totals, marker="s", linewidth=2.2, label="Tá»ng 3 viÃªn")
    ax.plot(xs, avg_total, linewidth=2.5, linestyle="--", label="TB tá»ng 5 vÃ¡n")
    for x, r in zip(xs, valid):
        ax.text(x, max(r) + 0.12, f"{r[0]}|{r[1]}|{r[2]}", ha="center", va="bottom", fontsize=8)
    ax.set_ylim(0.5, 19.5)
    ax.set_yticks([1, 2, 3, 4, 5, 6, 10, 11, 12, 15, 18])
    ax.set_xlabel("LÆ°á»£t xÃ­ ngáº§u", fontsize=10, color="white")
    ax.set_ylabel("GiÃ¡ trá»", fontsize=10, color="white")
    ax.set_title("BIá»U Äá» XÃ NGáº¦U - Tá»ªNG VIÃN", fontsize=13, fontweight="bold", color="white")
    if n <= 18:
        ax.set_xticks(xs)
        ax.set_xticklabels([str(i) for i in xs], fontsize=9, color="white")
    else:
        step = max(1, n // 12)
        ticks = xs[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([str(i) for i in ticks], fontsize=9, color="white")
    for spine in ax.spines.values():
        spine.set_color("#6b7280")
        spine.set_alpha(0.35)
    ax.tick_params(colors="white")
    ax.grid(True, alpha=0.14, linestyle="-")
    ax.legend(loc="upper left", fontsize=8, framealpha=0.25)
    dice_text = build_dice_chart_summary(summary)
    ax.text(0.02, 0.98, dice_text, transform=ax.transAxes, va="top", ha="left", fontsize=9, color="white", bbox=dict(boxstyle="round,pad=0.55", facecolor="#121826", alpha=0.92, edgecolor="#44506a"))
    ax.text(0.98, 0.02, "Má»i ÄÆ°á»ng lÃ  má»t viÃªn riÃªng", transform=ax.transAxes, va="bottom", ha="right", fontsize=9, color="white", bbox=dict(boxstyle="round,pad=0.35", facecolor="#1f2937", alpha=0.9, edgecolor="#6b7280"))
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf

def send_chart_summary_text(report: Dict[str, Any], adv: Dict[str, Any]) -> str:
    return (
        f"ð Tá»ng: {report.get('total',0)} | {LOW_LABEL}: {report.get('low',0)} | {HIGH_LABEL}: {report.get('high',0)}\n"
        f"ð§© Cáº¥u trÃºc: {report.get('structure','-')} | {report.get('detail','-')}\n"
        f"ð Bá»t max: {adv.get('max_streak',0)} | Momentum: {adv.get('momentum',0.0):.2f}\n"
        f"ð Trend: {adv.get('trend_label','TRUNG TÃNH')} ({adv.get('trend',0.0):.2f}) | "
        f"MÆ°á»£t: {adv.get('smoothness',0.0):.2f} | Nhiá»u: {adv.get('noise',0.0):.2f}"
    )

async def send_bridge_chart(update: Update, labels: List[str], report: Dict[str, Any], adv: Dict[str, Any], title: str, state: Optional[Dict[str, Any]] = None) -> None:
    if not update.message:
        return
    chart = build_bridge_chart_image(labels, report, adv, state=state)
    if chart is None:
        await update.message.reply_text("ð ChÆ°a Äá»§ dá»¯ liá»u Äá» váº½ biá»u Äá» cáº§u.")
        return
    try:
        chart.seek(0)
        await update.message.reply_photo(photo=chart, caption=title)
    except Exception as e:
        logger.exception("send_bridge_chart failed: %s", e)
        await update.message.reply_text("ð KhÃ´ng thá» gá»­i biá»u Äá» lÃºc nÃ y.")

async def send_dice_chart(update: Update, dice_rolls: List[List[int]], summary: Dict[str, Any], title: str) -> None:
    if not update.message:
        return
    chart = build_dice_chart_image(dice_rolls, summary)
    if chart is None:
        await update.message.reply_text("ð² ChÆ°a Äá»§ dá»¯ liá»u Äá» váº½ biá»u Äá» xÃ­ ngáº§u.")
        return
    try:
        chart.seek(0)
        await update.message.reply_photo(photo=chart, caption=title)
    except Exception as e:
        logger.exception("send_dice_chart failed: %s", e)
        await update.message.reply_text("ð² KhÃ´ng thá» gá»­i biá»u Äá» xÃ­ ngáº§u lÃºc nÃ y.")


# ===================== ROBOT ASSETS =====================
def ensure_robot_asset() -> str:
    if os.path.exists(ROBOT_IMAGE_PATH):
        return ROBOT_IMAGE_PATH
    try:
        fig, ax = plt.subplots(figsize=(5, 3), dpi=150)
        fig.patch.set_facecolor("#0f1117")
        ax.set_facecolor("#11151c")
        ax.axis("off")
        ax.text(0.5, 0.60, "ROBOT", ha="center", va="center", fontsize=28, fontweight="bold", color="white")
        ax.text(0.5, 0.35, "ÄANG PHÃN TÃCH...", ha="center", va="center", fontsize=14, color="white")
        fig.savefig(ROBOT_IMAGE_PATH, format="jpg", bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
    except Exception as e:
        logger.exception("KhÃ´ng táº¡o ÄÆ°á»£c file robot: %s", e)
    return ROBOT_IMAGE_PATH

def _load_robot_source() -> str:
    if os.path.exists(ROBOT_SOURCE_PATH):
        return ROBOT_SOURCE_PATH
    if os.path.exists(ROBOT_IMAGE_PATH):
        return ROBOT_IMAGE_PATH
    return ensure_robot_asset()

def ensure_robot_animation() -> str:
    if os.path.exists(ROBOT_ANIM_PATH):
        return ROBOT_ANIM_PATH
    if not PIL_AVAILABLE:
        return ensure_robot_asset()
    base_path = _load_robot_source()
    try:
        src = Image.open(base_path).convert("RGBA")
        w, h = src.size
        frames = []
        for i in range(24):
            phase = i / 24.0
            frame = src.copy()
            scale = 1.0 + 0.015 * math.sin(2 * math.pi * phase)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            scaled = frame.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", (w, h), (255, 255, 255, 255))
            x = (w - nw) // 2
            y = (h - nh) // 2 + int(5 * math.sin(2 * math.pi * phase))
            canvas.alpha_composite(scaled, (x, y))
            draw = ImageDraw.Draw(canvas)
            cx, cy = int(w * 0.48), int(h * 0.64)
            orbit_r = int(min(w, h) * 0.17)
            for j, off in enumerate([0.0, 2.2, 4.1]):
                ang = 2 * math.pi * phase + off
                px = cx + int(orbit_r * math.cos(ang))
                py = cy + int(orbit_r * 0.6 * math.sin(ang))
                rad = 4 + j
                alpha = int(70 + 90 * (0.5 + 0.5 * math.sin(2 * math.pi * phase + off)))
                draw.ellipse((px - rad, py - rad, px + rad, py + rad), fill=(110, 210, 255, alpha))
            blink_wave = max(0.08, (math.sin(2 * math.pi * phase * 2) + 1) / 2)
            eye_h = int(92 * (0.18 + 0.82 * blink_wave))
            eye_w = 28
            eye_y = int(h * 0.58)
            eye1_x = int(w * 0.41)
            eye2_x = int(w * 0.51)
            for ex in (eye1_x, eye2_x):
                top = eye_y + (92 - eye_h) // 2
                draw.rounded_rectangle((ex - eye_w // 2, top, ex + eye_w // 2, top + eye_h), radius=12, fill=(52, 225, 255, 220))
            arm_y = int(h * 0.71)
            swing = int(12 * math.sin(2 * math.pi * phase + math.pi / 2))
            draw.line((int(w * 0.34), arm_y, int(w * 0.30) + swing, arm_y + 50), fill=(110, 210, 255, 90), width=10)
            draw.line((int(w * 0.62), arm_y, int(w * 0.66) - swing, arm_y + 50), fill=(110, 210, 255, 90), width=10)
            ring_y = int(h * 0.90)
            glow_w = int(w * 0.08 + 10 * math.sin(2 * math.pi * phase))
            glow_h = int(h * 0.01 + 3 * math.sin(2 * math.pi * phase))
            alpha = int(120 + 60 * (0.5 + 0.5 * math.sin(2 * math.pi * phase)))
            draw.ellipse((int(w * 0.43) - glow_w, ring_y - glow_h, int(w * 0.43) + glow_w, ring_y + glow_h), fill=(65, 220, 255, alpha))
            if ImageEnhance is not None:
                canvas = ImageEnhance.Brightness(canvas).enhance(0.98 + 0.05 * (0.5 + 0.5 * math.sin(2 * math.pi * phase)))
            frames.append(canvas.convert("P", palette=Image.ADAPTIVE))
        frames[0].save(ROBOT_ANIM_PATH, save_all=True, append_images=frames[1:], duration=90, loop=0, optimize=False)
    except Exception as e:
        logger.exception("KhÃ´ng táº¡o ÄÆ°á»£c robot animation: %s", e)
        return ensure_robot_asset()
    return ROBOT_ANIM_PATH

def build_final_message(meta: Dict[str, Any], state: Dict[str, Any]) -> str:
    return (
        "â ROBOT ÄÃ PHÃN TÃCH XONG\n"
        f"ð CHá»T CUá»I: {meta.get('final_label', '-')}\n"
        f"ð Tá»· lá»: {meta.get('confidence', 0)}%\n"
        f"ð Win / Lose: {state.get('prediction_hits', 0)} / {state.get('prediction_misses', 0)}\n"
        f"ð WR tá»ng: {overall_winrate(state):.1f}% | WR gáº§n20: {recent_winrate(state, 20):.1f}%\n"
        f"ð§© Nhá»p: {state.get('last_structure', '-')} | {state.get('last_detected_pattern') or '-'}\n"
        f"ð Ghi chÃº: {state.get('last_note') or '-'}\n"
        f"ð² XÃ­ ngáº§u: {state.get('last_dice_summary') or '-'}\n"
        f"ð² Dice deep: {state.get('dice_deep_summary') or '-'}\n"
        f"ð¤ Tráº¡ng thÃ¡i: ÄÃ PHÃN TÃCH XONG"
    )

async def send_robot_status(update: Update, caption: str) -> Optional[Any]:
    if not update.message:
        return None
    anim_path = ensure_robot_animation()
    try:
        with open(anim_path, "rb") as f:
            return await update.message.reply_animation(animation=f, caption=caption)
    except Exception as e:
        logger.exception("send_robot_status failed: %s", e)
        try:
            with open(ensure_robot_asset(), "rb") as f:
                return await update.message.reply_photo(photo=f, caption=caption)
        except Exception:
            await update.message.reply_text(caption)
            return None

async def send_robot_analysis_sequence(update: Update, meta: Dict[str, Any], state: Dict[str, Any]) -> None:
    msg = await send_robot_status(update, "ð¤ ÄANG PHÃN TÃCH...")
    await asyncio.sleep(ROBOT_ANALYZE_DELAY)
    final_caption = build_final_message(meta, state)
    if msg:
        try:
            await msg.edit_caption(caption=final_caption)
        except Exception:
            try:
                await update.message.reply_text(final_caption)
            except Exception:
                pass
    else:
        try:
            await update.message.reply_text(final_caption)
        except Exception:
            pass


# ===================== UTILS =====================
def overall_winrate(state: Dict[str, Any]) -> float:
    return safe_div(int(state.get("prediction_hits", 0)) * 100.0, int(state.get("prediction_total", 0)))

def recent_winrate(state: Dict[str, Any], window: int = 20) -> float:
    outcomes = state.get("recent_outcomes", [])
    if not isinstance(outcomes, list):
        return 0.0
    tail = outcomes[-window:] if len(outcomes) > window else outcomes[:]
    if not tail:
        return 0.0
    wins = sum(1 for x in tail if x == "WIN")
    return safe_div(wins * 100.0, len(tail))

def build_trace_message(state: Dict[str, Any]) -> str:
    recent = recent_winrate(state, 20)
    overall = overall_winrate(state)
    trace = state.get("last_analysis_trace") or "-"
    model_preds = state.get("last_model_predictions", {})
    if isinstance(model_preds, dict) and model_preds:
        model_text = " | ".join(
            f"{k}:{v.get('label', '-')}"
            f"({int(v.get('confidence', 0))}%)"
            for k, v in model_preds.items()
        )
    else:
        model_text = "-"
    return (
        "ð§¾ TRACE PHÃN TÃCH Gáº¦N NHáº¤T\n"
        f"â¢ Window: {state.get('last_analysis_window', '-')}\n"
        f"â¢ AI model: {'Báº¬T' if state.get('ai_enabled', True) else 'Táº®T'}\n"
        f"â¢ HASH model: {'Báº¬T' if state.get('hash_enabled', True) else 'Táº®T'}\n"
        f"â¢ Cá»­a gÃ¡c: {state.get('last_gate_status', 'CHá»')}\n"
        f"â¢ LÃ½ do: {state.get('last_gate_reason', '-')}\n"
        f"â¢ Cáº§u hiá»n táº¡i: {state.get('last_detected_pattern') or '-'}\n"
        f"â¢ HÆ°á»ng: {state.get('last_detected_hint') or '-'}\n"
        f"â¢ Prediction: {state.get('last_prediction_label') or '-'}\n"
        f"â¢ Confidence: {state.get('last_prediction_conf', 0)}%\n"
        f"â¢ Win / Lose: {state.get('prediction_hits', 0)} / {state.get('prediction_misses', 0)}\n"
        f"â¢ WR tá»ng: {overall:.1f}%\n"
        f"â¢ WR gáº§n 20: {recent:.1f}%\n"
        f"â¢ Cáº§u sÃ¢u: {state.get('last_structure', '-')} | {state.get('last_note', '-')}\n"
        f"â¢ Trace: {trace}\n"
        f"â¢ Models: {model_text}\n"
        f"â¢ XÃ­ ngáº§u: {state.get('last_dice_summary', '-')}\n"
        f"â¢ Dice deep: {state.get('dice_deep_summary', '-')}"
    )


# ===================== COMMANDS =====================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    await update.message.reply_text(
        "ð TRá»¢ GIÃP\n"
        "/stats - xem báº£ng thá»ng kÃª\n"
        "/ai - phÃ¢n tÃ­ch cáº§u\n"
        "/next - giá»ng /ai\n"
        "/why - xem lÃ½ do chá»t/skip gáº§n nháº¥t\n"
        "/aimodel - báº­t/táº¯t hiá»n thá» AI\n"
        "/hashmodel - báº­t/táº¯t model hash/MD5\n"
        "/reset - xÃ³a dá»¯ liá»u chat hiá»n táº¡i\n"
        "/factory_reset - xÃ³a sáº¡ch toÃ n bá» bot\n\n"
        f"Quy Äá»i: sá» >= {THRESHOLD} -> {HIGH_LABEL}, sá» < {THRESHOLD} -> {LOW_LABEL}.\n"
        "Luá»ng hoáº¡t Äá»ng: cáº­p nháº­t thá»ng kÃª â biá»u Äá» cáº§u â biá»u Äá» xÃ­ ngáº§u â robot phÃ¢n tÃ­ch â chá»t cuá»i."
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
    labels = [r[1] for r in rows]
    state["labels"] = _safe_tail(labels, RECENT_CACHE)
    rebuild_counters_from_labels(state, labels)
    dice_rolls = state.get("dice_rolls", [])
    dice_summary = analyze_dice_deep(dice_rolls)
    window = choose_analysis_window(labels)
    labels_used = labels[-window:] if len(labels) > window else labels[:]
    state["last_analysis_window"] = window
    state["last_analysis_total"] = len(labels)
    report_full = build_report(labels)
    adv_full = advanced_metrics(labels)
    report_win = build_report(labels_used)
    adv_win = advanced_metrics(labels_used)
    adv_win.update(extract_chart_features(labels_used))
    await send_bridge_chart(update, labels_used, report_win, adv_win, "ð BIá»U Äá» Cáº¦U - THá»NG KÃ Má»I NHáº¤T", state)
    await send_dice_chart(update, dice_rolls, dice_summary, "ð² BIá»U Äá» XÃ NGáº¦U - THá»NG KÃ Má»I NHáº¤T")
    total = report_full["total"]
    low_p = safe_div(report_full["low"] * 100.0, total)
    high_p = safe_div(report_full["high"] * 100.0, total)
    await update.message.reply_text(
        "ââââââââââââââââââââââââââââââ\n"
        "â      â Báº¢NG THá»NG KÃ      â\n"
        "â âââââââââââââââââââââââââââââ£\n"
        f"â Tá»ng    : {total}\n"
        f"â {LOW_LABEL:<6}: {report_full['low']} ({low_p:.1f}%)\n"
        f"â {HIGH_LABEL:<6}: {report_full['high']} ({high_p:.1f}%)\n"
        f"â Cáº¥u trÃºc: {report_full['structure']}\n"
        f"â Window  : {state.get('last_analysis_window', '-')}\n"
        f"â Chi tiáº¿t : {report_full['detail']}\n"
        f"â Bá»t max  : {adv_full.get('max_streak', 0)}\n"
        f"â 10 gáº§n   : {adv_full.get('r10_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"â 20 gáº§n   : {adv_full.get('r20_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"â Momentum : {adv_full.get('momentum', 0.0):.2f}\n"
        f"â Trend    : {adv_full.get('trend_label', 'TRUNG TÃNH')} ({adv_full.get('trend', 0.0):.2f})\n"
        f"â MÆ°á»£t     : {adv_full.get('smoothness', 0.0):.2f}\n"
        f"â Nhiá»u    : {adv_full.get('noise', 0.0):.2f}\n"
        f"â Äáº£o chiá»u: {adv_full.get('reversal', 0.0):.1f}%\n"
        f"â Entropy  : {report_full.get('entropy', 0.0):.2f}\n"
        f"â Volatility: {report_full.get('volatility', 0.0):.2f}\n"
        f"â CycleSig : {report_full.get('cycle_signature', '-')}\n"
        f"â Repeat   : {report_full.get('repeat_signature', '-')} x{report_full.get('repeat_count', 0)}\n"
        f"â Loáº¡n/Stab: {report_full.get('chaos_score', 0)}/{report_full.get('stability_score', 50)}\n"
        f"â Ghi chÃº  : {report_full.get('repeat_note', '-')}\n"
        f"â Win/Lose : {state.get('prediction_hits', 0)}/{state.get('prediction_misses', 0)}\n"
        f"â WR tá»ng  : {overall_winrate(state):.1f}%\n"
        f"â WR gáº§n20 : {recent_winrate(state, 20):.1f}%\n"
        f"â Cá»­a gÃ¡c  : {state.get('last_gate_status', 'CHá»')}\n"
        f"â LÃ½ do    : {state.get('last_gate_reason', 'ChÆ°a kiá»m tra')}\n"
        "ââââââââââââââââââââââââââââââ"
    )

async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id, force_reload=True))
        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        labels = [r[1] for r in rows]
        state["labels"] = _safe_tail(labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, labels)
        result = analyze_state_from_labels(state, labels, state.get("dice_rolls", []))
        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()
    report = result["report"]
    adv = result["adv"]
    labels_used = result.get("labels_used", labels)
    dice_used = result.get("dice_used", state.get("dice_rolls", []))
    dice_summary = result.get("dice_summary", {})
    await send_bridge_chart(update, labels_used, report, adv, "ð BIá»U Äá» Cáº¦U - DÃNG CHO PHÃN TÃCH", state)
    await send_dice_chart(update, dice_used, dice_summary, "ð² BIá»U Äá» XÃ NGáº¦U - DÃNG CHO PHÃN TÃCH")
    if not result.get("allowed", False):
        await send_robot_status(update, f"â¸ BOT Táº M Dá»ªNG\nLÃ½ do: {result.get('reason', 'Cáº§u chÆ°a rÃµ')}")
        return
    meta = result["meta"]
    await send_robot_analysis_sequence(update, meta, state)

async def why_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    await update.message.reply_text(build_trace_message(state))

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    await ai_cmd(update, context)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            conn.commit()
    async with DB_LOCK:
        await run_db_work(_work)
    users.pop(chat_id, None)
    await update.message.reply_text("ð ÄÃ£ reset chat hiá»n táº¡i.")

async def factory_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history")
            conn.execute("DELETE FROM chat_state")
            conn.commit()
    async with DB_LOCK:
        await run_db_work(_work)
    users.clear()
    await update.message.reply_text("ð§¼ ÄÃ£ xÃ³a sáº¡ch toÃ n bá» dá»¯ liá»u.")

async def aimodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    state["ai_enabled"] = not state.get("ai_enabled", True)
    await save_state(chat_id, state)
    users[chat_id] = state
    trim_cache()
    await update.message.reply_text(f"ð¤ AI model: {'Báº¬T' if state['ai_enabled'] else 'Táº®T'}")

async def hashmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    state["hash_enabled"] = not state.get("hash_enabled", True)
    if not state["hash_enabled"]:
        state["hash_waiting"] = False
    await save_state(chat_id, state)
    users[chat_id] = state
    trim_cache()
    await update.message.reply_text(f"ð· HASH model: {'Báº¬T' if state['hash_enabled'] else 'Táº®T'}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not await admin_only(update):
            return
        if not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        if is_hash_like(text):
            await process_chat(update, context, nums=None)
            return
        nums, dice_rolls = parse_input(text)
        if not nums and not dice_rolls:
            return
        await process_chat(update, context, nums, dice_rolls)
    except Exception as e:
        logger.exception("handle_text failed: %s", e)
        if update.message:
            await update.message.reply_text("â Lá»i khi xá»­ lÃ½ dá»¯ liá»u")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.exception("Global error: %s", err)
    if isinstance(err, Conflict):
        logger.error("Bot Äang bá» cháº¡y trÃ¹ng instance. HÃ£y táº¯t instance khÃ¡c Äang dÃ¹ng cÃ¹ng token.")
        return
    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after + 1)
    elif isinstance(err, (TimedOut, NetworkError, TelegramError)):
        await asyncio.sleep(1.0)
    try:
        if getattr(update, "message", None):
            await update.message.reply_text("â ï¸ CÃ³ lá»i táº¡m thá»i, bot ÄÃ£ tá»± giá»¯ an toÃ n.")
    except Exception:
        pass


# ===================== PROCESS =====================
async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, nums: Optional[List[int]] = None, dice_rolls: Optional[List[List[int]]] = None) -> None:
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    text = (update.message.text or "").strip()
    hash_input = is_hash_like(text)

    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id, force_reload=True))
        ai_enabled = bool(state.get("ai_enabled", True))
        hash_enabled = bool(state.get("hash_enabled", True))

        if hash_input:
            if not hash_enabled:
                return
            msg_parts: List[str] = []
            if state.get("hash_waiting") and state.get("last_real_value") is not None:
                actual_value = int(state["last_real_value"])
                actual_label = map_value(actual_value)
                pred = state.get("hash_last_prediction")
                if pred in (LOW_LABEL, HIGH_LABEL):
                    is_win = pred == actual_label
                    state["hash_total"] = int(state.get("hash_total", 0)) + 1
                    if is_win:
                        state["hash_win"] = int(state.get("hash_win", 0)) + 1
                    else:
                        state["hash_lose"] = int(state.get("hash_lose", 0)) + 1
                    msg_parts.append(build_hash_confirmation_message(state, actual_value, actual_label))
                state["hash_waiting"] = False
            msg_parts.append(build_hash_stats_message(state))
            analysis = analyze_hash_model(text)
            state["hash_last_prediction"] = analysis["label"]
            state["hash_last_conf"] = int(analysis["confidence"])
            state["hash_last_input"] = text
            state["hash_waiting"] = True
            state["last_note"] = f"HASH dá»± ÄoÃ¡n: {analysis['label']}"
            msg_parts.append("ð· PhÃ¢n tÃ­ch má»i:\n" + analysis["text"] + "\n")
            await save_state(chat_id, state)
            users[chat_id] = state
            trim_cache()
            await update.message.reply_text("ð HASH MODEL\n\n" + "\n".join(msg_parts))
            return

        entries: List[Tuple[int, str]] = []
        if nums:
            for n in nums:
                entries.append((n, map_value(n)))

        if entries:
            await append_history(chat_id, entries)
            if dice_rolls:
                valid_rolls = [r for r in dice_rolls if isinstance(r, list) and len(r) == 3]
                if valid_rolls:
                    state.setdefault("dice_rolls", [])
                    state["dice_rolls"].extend(valid_rolls)
                    state["dice_rolls"] = _safe_tail(state["dice_rolls"], RECENT_CACHE)

        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        full_values = [r[0] for r in rows]
        full_labels = [r[1] for r in rows]
        state["values"] = _safe_tail(full_values, RECENT_CACHE)
        state["labels"] = _safe_tail(full_labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, full_labels)

        if entries:
            latest_actual_value = entries[-1][0]
            latest_actual_label = entries[-1][1]
            state["last_real_value"] = latest_actual_value
            update_prediction_feedback(state, latest_actual_label)
            prev_predictions = state.get("last_model_predictions", {})
            if isinstance(prev_predictions, dict) and prev_predictions:
                update_model_accuracy(state, prev_predictions, latest_actual_label)

        result = analyze_state_from_labels(state, full_labels, state.get("dice_rolls", []))
        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()

    report = result["report"]
    adv = result["adv"]
    labels_used = result.get("labels_used", full_labels)
    dice_used = result.get("dice_used", state.get("dice_rolls", []))
    dice_summary = result.get("dice_summary", {})

    if ai_enabled:
        await send_bridge_chart(update, labels_used, report, adv, "ð BIá»U Äá» Cáº¦U - Cáº¬P NHáº¬T Má»I", state)
        await send_dice_chart(update, dice_used, dice_summary, "ð² BIá»U Äá» XÃ NGáº¦U - Tá»ªNG VIÃN")
        if not result.get("allowed", False):
            await send_robot_status(update, f"â¸ BOT Táº M Dá»ªNG\nLÃ½ do: {result.get('reason', 'Cáº§u chÆ°a rÃµ')}")
            return
        meta = result["meta"]
        await send_robot_analysis_sequence(update, meta, state)


# ===================== MAIN =====================
def main():
    init_db()
    ensure_robot_asset()
    ensure_robot_animation()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("why", why_cmd))
    app.add_handler(CommandHandler("aimodel", aimodel_cmd))
    app.add_handler(CommandHandler("hashmodel", hashmodel_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("factory_reset", factory_reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("ð¥ BOT THá»NG KÃ - PHÃN TÃCH Cáº¦U ÄANG CHáº Y...")
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
