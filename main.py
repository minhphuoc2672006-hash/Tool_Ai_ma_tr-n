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
MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))
MAX_PATTERN_MEMORY = int(os.getenv("MAX_PATTERN_MEMORY", "2500"))
PATTERN_DECAY = float(os.getenv("PATTERN_DECAY", "0.995"))
MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "15"))
MAX_SUFFIX_SCAN = int(os.getenv("MAX_SUFFIX_SCAN", "5000"))
MAX_CYCLE_SCAN = int(os.getenv("MAX_CYCLE_SCAN", "5000"))
MIN_CONFIDENCE = int(os.getenv("MIN_CONFIDENCE", "62"))

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()

# =========================
# STATE
# =========================
def new_user() -> Dict[str, Any]:
    return {
        "history": [],
        "values": [],
        "labels": [],
        "low_count": 0,
        "high_count": 0,
        "mode": "WARMUP",
        "updates": 0,
        "health_log": [],
        "pattern_memory": defaultdict(float),
    }


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


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                low_count INTEGER NOT NULL DEFAULT 0,
                high_count INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'WARMUP',
                updates INTEGER NOT NULL DEFAULT 0,
                health_log_json TEXT NOT NULL DEFAULT '[]'
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


async def wipe_all_state() -> None:
    async with DB_LOCK:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)


async def delete_chat_state(chat_id: int) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM pattern_memory WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))


def _deserialize_float_map(src: Dict[str, float]) -> defaultdict:
    out = defaultdict(float)
    for k, v in (src or {}).items():
        out[k] = float(v)
    return out


async def load_user(chat_id: int) -> Dict[str, Any]:
    if chat_id in users:
        return users[chat_id]

    state = new_user()

    async with DB_LOCK:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT low_count, high_count, mode, updates, health_log_json FROM users WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if row:
                state["low_count"] = int(row["low_count"])
                state["high_count"] = int(row["high_count"])
                state["mode"] = row["mode"] or "WARMUP"
                state["updates"] = int(row["updates"])
                try:
                    state["health_log"] = list(json.loads(row["health_log_json"] or "[]"))
                except Exception:
                    state["health_log"] = []

            hist_rows = conn.execute(
                "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()

            for r in hist_rows:
                raw_value = int(r["raw_value"])
                label = r["label"]
                if label not in (LOW_LABEL, HIGH_LABEL):
                    continue
                state["values"].append(raw_value)
                state["labels"].append(label)

            pat_rows = conn.execute(
                "SELECT pattern, weight FROM pattern_memory WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()

            pat_map = {r["pattern"]: float(r["weight"]) for r in pat_rows}
            state["pattern_memory"] = _deserialize_float_map(pat_map)

    state["history"] = [
        {"value": v, "label": l, "source": "real", "conf": 1.0}
        for v, l in list(zip(state["values"], state["labels"]))[-50:]
    ]
    users[chat_id] = state
    trim_cache()
    return state


async def save_user_meta(chat_id: int, d: Dict[str, Any]) -> None:
    async with DB_LOCK:
        health_json = json.dumps(d.get("health_log", []), ensure_ascii=False)
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO users (chat_id, low_count, high_count, mode, updates, health_log_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    low_count=excluded.low_count,
                    high_count=excluded.high_count,
                    mode=excluded.mode,
                    updates=excluded.updates,
                    health_log_json=excluded.health_log_json
                """,
                (
                    chat_id,
                    int(d.get("low_count", 0)),
                    int(d.get("high_count", 0)),
                    d.get("mode", "WARMUP"),
                    int(d.get("updates", 0)),
                    health_json,
                ),
            )


async def persist_batch(
    chat_id: int,
    d: Dict[str, Any],
    entries: List[Tuple[int, str, str, float]],
    patterns: List[Tuple[str, float]],
) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            for raw_value, label, source, conf in entries:
                conn.execute(
                    "INSERT INTO history (chat_id, raw_value, label, source, conf) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, int(raw_value), label, source, float(conf)),
                )

            for pattern, inc in patterns:
                conn.execute(
                    """
                    INSERT INTO pattern_memory (chat_id, pattern, weight)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, pattern)
                    DO UPDATE SET weight = weight + excluded.weight
                    """,
                    (chat_id, pattern, float(inc)),
                )

            health_json = json.dumps(d.get("health_log", []), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO users (chat_id, low_count, high_count, mode, updates, health_log_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    low_count=excluded.low_count,
                    high_count=excluded.high_count,
                    mode=excluded.mode,
                    updates=excluded.updates,
                    health_log_json=excluded.health_log_json
                """,
                (
                    chat_id,
                    int(d.get("low_count", 0)),
                    int(d.get("high_count", 0)),
                    d.get("mode", "WARMUP"),
                    int(d.get("updates", 0)),
                    health_json,
                ),
            )


# =========================
# UTIL
# =========================
def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    users.clear()


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


def normalize_user_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if "history" not in d:
        d["history"] = []
    if "values" not in d:
        d["values"] = []
    if "labels" not in d:
        d["labels"] = []
    if "low_count" not in d:
        d["low_count"] = 0
    if "high_count" not in d:
        d["high_count"] = 0
    if "mode" not in d:
        d["mode"] = "WARMUP"
    if "updates" not in d:
        d["updates"] = 0
    if "health_log" not in d:
        d["health_log"] = []
    if "pattern_memory" not in d:
        d["pattern_memory"] = defaultdict(float)
    return d


def decay_pattern_memory(d: Dict[str, Any]) -> None:
    pm = d.get("pattern_memory", {})
    if not pm:
        return

    for k in list(pm.keys()):
        pm[k] *= PATTERN_DECAY
        if pm[k] < 0.08:
            del pm[k]

    if len(pm) > MAX_PATTERN_MEMORY:
        items = sorted(pm.items(), key=lambda x: x[1])
        drop_n = len(pm) - MAX_PATTERN_MEMORY
        for k, _ in items[:drop_n]:
            pm.pop(k, None)


def format_history(labels: List[str], tail: int = 30) -> str:
    out = []
    for lb in labels[-tail:]:
        if lb == HIGH_LABEL:
            out.append("⬛")
        elif lb == LOW_LABEL:
            out.append("⬜")
    return " ".join(out) if out else "(trống)"


def bar(percent: float, width: int = 12) -> str:
    p = max(0.0, min(100.0, percent))
    filled = int(round(width * p / 100.0))
    empty = width - filled
    return "█" * filled + "░" * empty


def format_pattern_lines(patterns: List[Tuple[str, float]]) -> str:
    if not patterns:
        return "• (không có)"
    lines = []
    for p, w in patterns:
        lines.append(f"• {p}\n  ↳ trọng số: {w:.1f}")
    return "\n".join(lines)


def format_prob_block(probs: Dict[str, float]) -> str:
    low_p = probs.get(LOW_LABEL, 0.5) * 100
    high_p = probs.get(HIGH_LABEL, 0.5) * 100
    return (
        f"• {LOW_LABEL:<4}: {low_p:>5.1f}%  {bar(low_p)}\n"
        f"• {HIGH_LABEL:<4}: {high_p:>5.1f}%  {bar(high_p)}"
    )


# =========================
# PATTERN UPDATE
# =========================
def update_pattern_memory_in_memory(d: Dict[str, Any]) -> List[Tuple[str, float]]:
    labels = d.get("labels", [])
    ops: List[Tuple[str, float]] = []

    if len(labels) >= 4:
        p4 = "|".join(labels[-4:])
        d["pattern_memory"][p4] += 1.0
        ops.append((p4, 1.0))

    if len(labels) >= 5:
        p5 = "|".join(labels[-5:])
        d["pattern_memory"][p5] += 0.7
        ops.append((p5, 0.7))

    if len(labels) >= 6:
        p6 = "|".join(labels[-6:])
        d["pattern_memory"][p6] += 0.5
        ops.append((p6, 0.5))

    decay_pattern_memory(d)
    return ops


# =========================
# ANALYSIS
# =========================
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
    max_ent = math.log2(2)
    return ent / max_ent if max_ent else 0.0


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

    h = labels[-MAX_CYCLE_SCAN:]
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
            score = min((cnt / occur) * 100, 85.0)
            if score > best_score:
                best_score = score
                best_cycle = size

    return best_cycle, best_score


def summarize_patterns(d: Dict[str, Any], top_n: int = 3) -> List[Tuple[str, float]]:
    items = list(d.get("pattern_memory", {}).items())
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:top_n]


def master_controller(d: Dict[str, Any]) -> Tuple[str, str]:
    labels = d.get("labels", [])
    if len(labels) < 10:
        return "WARMUP", "📊 Đang học"

    vol = volatility_score(labels)
    ent = entropy_score(labels)
    alt, alt_ratio = alternating_analysis(labels)
    last_label, streak = current_streak(labels)
    cycle_len, cycle_score = detect_repeat_cycle(labels)
    low = labels.count(LOW_LABEL)
    high = labels.count(HIGH_LABEL)
    gap = abs(low - high)

    if vol >= 0.85 and ent >= 0.85:
        return "NOISY", "🚫 Nhiễu cao"
    if alt and alt_ratio >= 1.0:
        return "ALT", "🔁 Xen kẽ mạnh"
    if streak >= 6:
        return "STREAK", f"📌 Chuỗi dài ({last_label} x{streak})"
    if cycle_score >= 70 and cycle_len:
        return "CYCLE", f"♻️ Vòng lặp rõ (size {cycle_len})"
    if gap >= 20:
        return "BIAS_STRONG", "⚖️ Lệch tổng mạnh"
    if gap >= 12:
        return "BIAS_MEDIUM", "⚖️ Lệch tổng vừa"
    if len(labels) >= 20 and abs(low - high) <= 2:
        return "BALANCED", "✅ Cân bằng"
    return "NORMAL", "✅ Ổn định"


def analyze_sequence(d: Dict[str, Any]) -> Dict[str, Any]:
    labels = d.get("labels", [])
    total = len(labels)
    low = labels.count(LOW_LABEL)
    high = labels.count(HIGH_LABEL)

    last_label, streak = current_streak(labels)
    alt, alt_ratio = alternating_analysis(labels)
    vol = volatility_score(labels)
    ent = entropy_score(labels)
    cycle_len, cycle_score = detect_repeat_cycle(labels)
    mode, note = master_controller(d)
    long_mode, long_score = long_bias(labels)
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
        "long_mode": long_mode,
        "long_score": long_score,
        "patterns": summarize_patterns(d, top_n=5),
    }


def weighted_label_probs(labels: List[str], window: int = 40, decay: float = 0.94) -> Dict[str, float]:
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
    if not labels:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(labels)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {
        LOW_LABEL: c[LOW_LABEL] / total,
        HIGH_LABEL: c[HIGH_LABEL] / total,
    }


def markov_next_probs(labels: List[str]) -> Dict[str, float]:
    if len(labels) < 2:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    trans = {
        LOW_LABEL: Counter(),
        HIGH_LABEL: Counter(),
    }

    for a, b in zip(labels[:-1], labels[1:]):
        if a in trans and b in trans:
            trans[a][b] += 1

    last = labels[-1]
    if last not in trans or not trans[last]:
        return global_label_probs(labels)

    total = sum(trans[last].values())
    if total <= 0:
        return global_label_probs(labels)

    return {
        LOW_LABEL: trans[last][LOW_LABEL] / total,
        HIGH_LABEL: trans[last][HIGH_LABEL] / total,
    }


def normalize_probs(scores: Dict[str, float]) -> Dict[str, float]:
    for k in (LOW_LABEL, HIGH_LABEL):
        scores.setdefault(k, 0.0)

    for k in scores:
        if scores[k] < 0:
            scores[k] = 0.0

    total = scores[LOW_LABEL] + scores[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}

    return {
        LOW_LABEL: scores[LOW_LABEL] / total,
        HIGH_LABEL: scores[HIGH_LABEL] / total,
    }


def forecast_next(report: Dict[str, Any], labels: List[str]) -> Dict[str, Any]:
    total = int(report.get("total", 0))
    mode = report.get("mode", "WARMUP")
    last_label = report.get("last_label")
    streak = int(report.get("streak", 0))
    alt = bool(report.get("alternating", False))
    alt_ratio = float(report.get("alt_ratio", 0.0))
    vol = float(report.get("volatility", 0.0))
    ent = float(report.get("entropy", 0.0))
    cycle_score = float(report.get("cycle_score", 0.0))
    cycle_len = report.get("cycle_len")
    low = int(report.get("low", 0))
    high = int(report.get("high", 0))

    recent_probs = weighted_label_probs(labels, window=40, decay=0.92)
    global_probs = global_label_probs(labels)
    markov_probs = markov_next_probs(labels)

    scores = {
        LOW_LABEL: (
            recent_probs[LOW_LABEL] * 0.45
            + global_probs[LOW_LABEL] * 0.25
            + markov_probs[LOW_LABEL] * 0.30
        ),
        HIGH_LABEL: (
            recent_probs[HIGH_LABEL] * 0.45
            + global_probs[HIGH_LABEL] * 0.25
            + markov_probs[HIGH_LABEL] * 0.30
        ),
    }

    if mode == "NOISY" or (vol >= 0.85 and ent >= 0.85):
        scores[LOW_LABEL] = scores[LOW_LABEL] * 0.55 + 0.225
        scores[HIGH_LABEL] = scores[HIGH_LABEL] * 0.55 + 0.225

    if streak >= 5 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[last_label] *= 0.90
        scores[other] *= 1.10

    if alt and alt_ratio >= 0.90 and last_label in (LOW_LABEL, HIGH_LABEL):
        other = HIGH_LABEL if last_label == LOW_LABEL else LOW_LABEL
        scores[other] *= 1.08

    if cycle_score >= 70 and cycle_len:
        scores[LOW_LABEL] *= 1.03
        scores[HIGH_LABEL] *= 1.03

    gap = abs(low - high)
    if gap >= 12:
        dominant = LOW_LABEL if low > high else HIGH_LABEL
        scores[dominant] *= 1.06

    probs = normalize_probs(scores)

    best_label = max(probs, key=probs.get)
    other_label = HIGH_LABEL if best_label == LOW_LABEL else LOW_LABEL
    delta = abs(probs[LOW_LABEL] - probs[HIGH_LABEL])

    quality = 0
    if total >= 30:
        quality += 8
    elif total >= 15:
        quality += 4

    if mode in ("STREAK", "CYCLE"):
        quality += 7
    elif mode in ("BALANCED", "NORMAL"):
        quality += 2
    elif mode == "NOISY":
        quality -= 10

    confidence = int(max(0, min(100, 48 + delta * 45 + quality)))

    if total < MIN_ANALYSIS_LEN:
        return {
            "status": "WARMUP",
            "confidence": 0,
            "message": "Chưa đủ dữ liệu để ước tính.",
            "best_label": None,
            "other_label": None,
            "probs": {LOW_LABEL: 0.5, HIGH_LABEL: 0.5},
        }

    if confidence < MIN_CONFIDENCE:
        return {
            "status": "UNCERTAIN",
            "confidence": confidence,
            "message": "Tín hiệu còn yếu, ước tính chưa rõ.",
            "best_label": best_label,
            "other_label": other_label,
            "probs": probs,
        }

    if delta < 0.08:
        status = "BALANCED"
        message = "Hai phía khá cân bằng, khó nghiêng rõ."
    else:
        status = "TRENDING"
        if best_label == LOW_LABEL:
            message = f"Xu hướng hiện tại nghiêng về {LOW_LABEL}."
        else:
            message = f"Xu hướng hiện tại nghiêng về {HIGH_LABEL}."

    if streak >= 6:
        status = "STREAK"
        message = f"Chuỗi hiện tại rất mạnh: {last_label} x{streak}."

    if cycle_score >= 70 and cycle_len:
        status = "CYCLE"
        message = f"Phát hiện vòng lặp khá rõ (size {cycle_len})."

    return {
        "status": status,
        "confidence": confidence,
        "message": message,
        "best_label": best_label,
        "other_label": other_label,
        "probs": probs,
    }


# =========================
# BEAUTY / RENDER
# =========================
def build_output(report: Dict[str, Any], ai: Dict[str, Any], hist: str, pattern_text: str) -> str:
    probs = ai.get("probs", {LOW_LABEL: 0.5, HIGH_LABEL: 0.5})
    low_p = int(round(probs.get(LOW_LABEL, 0.5) * 100))
    high_p = int(round(probs.get(HIGH_LABEL, 0.5) * 100))
    best = ai.get("best_label") or "-"

    streak_text = f"{report['last_label']} x{report['streak']}" if report["last_label"] else "—"
    cycle_text = f"{report['cycle_len']}" if report["cycle_len"] else "—"

    return f"""
╔══════════════════════════════════╗
║        📊 BẢNG PHÂN TÍCH         ║
╠══════════════════════════════════╣
║ {report['note']:<32} ║
╚══════════════════════════════════╝

🤖 AI KẾT LUẬN
┌──────────────────────────────────┐
│ Trạng thái : {ai['status']:<23}│
│ Tin cậy    : {ai['confidence']:>3}%                      │
│ Nhận xét   :                         │
│ {ai['message'][:30]:<30}│
└──────────────────────────────────┘

🔮 DỰ PHÓNG NHÃN KẾ TIẾP
{format_prob_block(probs)}
• Ước tính nghiêng: {best}

📈 BIỂU DIỄN LỊCH SỬ
{hist}

📋 THỐNG KÊ NHANH
• Tổng phiên  : {report['total']}
• {LOW_LABEL:<10}: {report['low']}
• {HIGH_LABEL:<10}: {report['high']}
• Chuỗi hiện tại: {streak_text}
• Xen kẽ       : {'Có' if report['alternating'] else 'Không'} | {report['alt_ratio']:.2f}
• Nhiễu        : {report['volatility']:.2f}
• Entropy      : {report['entropy']:.2f}
• Vòng lặp     : {cycle_text} | {report['cycle_score']:.0f}
• Lệch tổng    : {report['long_mode']} | {report['long_score']}

🧩 TOP PATTERN
{pattern_text}
""".strip()


# =========================
# COMMANDS
# =========================
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        users.pop(chat_id, None)
        await delete_chat_state(chat_id)
        if update.message:
            await update.message.reply_text("🔄 RESET CHAT HIỆN TẠI XONG")
    except Exception as e:
        logger.exception("reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi reset")


async def factory_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        users.clear()
        await wipe_all_state()
        init_db()
        if update.message:
            await update.message.reply_text("🧼 FACTORY RESET XONG: xóa sạch toàn bộ dữ liệu và file lưu")
    except Exception as e:
        logger.exception("factory_reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi factory reset")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        d = await load_user(chat_id)
        total = d["low_count"] + d["high_count"]

        recent = d["health_log"][-RECENT_WINDOW:]
        recent_total = len(recent)
        recent_stable = int((sum(recent) / recent_total) * 100) if recent_total else 0
        balance = int((min(d["low_count"], d["high_count"]) / total) * 100) if total else 0
        hist_len = len(d.get("labels", []))

        await update.message.reply_text(
            f"""
╔══════════════📊 THỐNG KÊ══════════════╗
• {LOW_LABEL}: {d['low_count']}
• {HIGH_LABEL}: {d['high_count']}
• Độ cân bằng tổng: {balance}%
• Ổn định {recent_total} lượt gần nhất: {recent_stable}%
• Mode hiện tại: {d.get('mode', 'WARMUP')}
• Số lượt đã xử lý: {d.get('updates', 0)}
• Tổng lịch sử đã lưu: {hist_len}
╚══════════════════════════════════════╝
            """.strip()
        )
    except Exception as e:
        logger.exception("stats failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xem thống kê")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            f"""
╔══════════════📘 TRỢ GIÚP══════════════╗
/stats - xem thống kê
/ai - xem kết luận AI hiện tại
/next - xem dự phóng nhãn kế tiếp
/reset - xóa dữ liệu của chat hiện tại
/factory_reset - xóa sạch toàn bộ bot

Gửi số để hệ thống tự học và phân tích chuỗi.
Quy đổi hiện tại: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}.
╚══════════════════════════════════════╝
            """.strip()
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
        report = analyze_sequence(d)
        ai = forecast_next(report, d.get("labels", []))

        patterns = report.get("patterns", [])
        pattern_text = format_pattern_lines(patterns)
        hist = format_history(d.get("labels", []))

        await update.message.reply_text(build_output(report, ai, hist, pattern_text))
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
            d = normalize_user_state(await load_user(chat_id))

            nums = parse_input(update.message.text)
            if not nums:
                return

            entries: List[Tuple[int, str, str, float]] = []
            patterns: List[Tuple[str, float]] = []

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
                d["history"] = d["history"][-50:]

                entries.append((n, label, "real", 1.0))
                d["health_log"].append(1)

                patterns.extend(update_pattern_memory_in_memory(d))

            d["health_log"] = d["health_log"][-RECENT_WINDOW:]
            await persist_batch(chat_id, d, entries, patterns)
            await save_user_meta(chat_id, d)

        msg = await update.message.reply_text("🧠 Đang phân tích chuỗi...")
        await asyncio.sleep(0.06)

        d = await load_user(chat_id)
        report = analyze_sequence(d)
        ai = forecast_next(report, d.get("labels", []))

        hist = format_history(d.get("labels", []))
        pattern_text = format_pattern_lines(report.get("patterns", []))

        await msg.edit_text(build_output(report, ai, hist, pattern_text))
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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("factory_reset", factory_reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 SEQUENCE ANALYZER RUNNING...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
