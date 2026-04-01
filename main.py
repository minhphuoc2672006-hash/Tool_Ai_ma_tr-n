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
MAX_SUFFIX_SCAN = int(os.getenv("MAX_SUFFIX_SCAN", "5000"))
MAX_CYCLE_SCAN = int(os.getenv("MAX_CYCLE_SCAN", "5000"))

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


def format_history(labels: List[str], tail: int = 30) -> str:
    out = []
    for lb in labels[-tail:]:
        if lb == HIGH_LABEL:
            out.append("⬛")
        elif lb == LOW_LABEL:
            out.append("⬜")
    return " ".join(out) if out else "(trống)"


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
        "patterns": summarize_patterns(d, top_n=3),
    }

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
📊 THỐNG KÊ

• {LOW_LABEL}: {d['low_count']}
• {HIGH_LABEL}: {d['high_count']}
• Độ cân bằng tổng: {balance}%
• Tỷ lệ ổn định {recent_total} lượt gần nhất: {recent_stable}%
• Mode hiện tại: {d.get('mode', 'WARMUP')}
• Số lượt đã xử lý: {d.get('updates', 0)}
• Tổng lịch sử đã lưu: {hist_len}
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
📘 LỆNH HỖ TRỢ

/stats - xem thống kê
/reset - xóa dữ liệu của chat hiện tại
/factory_reset - xóa sạch toàn bộ bot, như mới tạo

Gửi các số để hệ thống tự học và phân tích chuỗi 2 trạng thái.
Quy đổi hiện tại: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}.
            """.strip()
        )
    except Exception as e:
        logger.exception("help failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi mở trợ giúp")

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
        hist = format_history(d.get("labels", []))
        patterns = report.get("patterns", [])

        pattern_text = "(không có)"
        if patterns:
            pattern_text = "\n".join([f"• {p}  |  {w:.1f}" for p, w in patterns])

        await msg.edit_text(
            f"""
╔══ 📈 CHUỖI PHÂN TÍCH ══╗
{report['note']}

📊 {hist}

• Tổng: {report['total']}
• {LOW_LABEL}: {report['low']}
• {HIGH_LABEL}: {report['high']}
• Chuỗi hiện tại: {report['last_label']} x{report['streak'] if report['last_label'] else 0}
• Xen kẽ: {'Có' if report['alternating'] else 'Không'} | {report['alt_ratio']:.2f}
• Nhiễu: {report['volatility']:.2f}
• Entropy: {report['entropy']:.2f}
• Vòng lặp: {report['cycle_len'] if report['cycle_len'] else '-'} | {report['cycle_score']:.0f}

🧩 Top pattern
{pattern_text}
╚═══════════════════════╝
            """.strip()
        )
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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("factory_reset", factory_reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 SEQUENCE ANALYZER RUNNING...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
