import os
import re
import json
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
DB_FILE = "ai_state.db"

RECENT_EVAL_WINDOW = 80
MAX_SUFFIX_SCAN = 5000
MAX_CYCLE_SCAN = 5000

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()

# =========================
# STATE
# =========================
def new_transition_bank() -> Dict[str, defaultdict]:
    return {str(order): defaultdict(Counter) for order in range(1, 6)}


def new_user() -> Dict[str, Any]:
    return {
        "history": [],          # optional preview log, not used for analysis
        "tx_history": [],       # full in-memory tx list for active chat
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "transitions": new_transition_bank(),
        "pattern_memory": defaultdict(float),
        "ai_mode": "NORMAL",
        "eval_log": [],
        "updates": 0,
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
                win INTEGER NOT NULL DEFAULT 0,
                lose INTEGER NOT NULL DEFAULT 0,
                last_pred TEXT,
                ai_mode TEXT NOT NULL DEFAULT 'NORMAL',
                eval_log_json TEXT NOT NULL DEFAULT '[]',
                updates INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                tx TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'real',
                conf REAL NOT NULL DEFAULT 1.0,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                FOREIGN KEY(chat_id) REFERENCES users(chat_id)
            );

            CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);

            CREATE TABLE IF NOT EXISTS transitions (
                chat_id INTEGER NOT NULL,
                order_n INTEGER NOT NULL,
                context TEXT NOT NULL,
                next_tx TEXT NOT NULL,
                count REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, order_n, context, next_tx)
            );

            CREATE INDEX IF NOT EXISTS idx_transitions_chat_order_context
                ON transitions(chat_id, order_n, context);

            CREATE TABLE IF NOT EXISTS pattern_memory (
                chat_id INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, pattern)
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_chat_id ON pattern_memory(chat_id);
            """
        )


def _serialize_transition_bank(bank: Dict[str, defaultdict]) -> Dict[str, Dict[str, Dict[str, float]]]:
    data: Dict[str, Dict[str, Dict[str, float]]] = {}
    for order, model in bank.items():
        data[order] = {}
        for key, counter in model.items():
            data[order][key] = {k: float(v) for k, v in counter.items()}
    return data


def _deserialize_transition_bank(raw: Dict[str, Dict[str, Dict[str, float]]]) -> Dict[str, defaultdict]:
    bank = new_transition_bank()
    for order, model in (raw or {}).items():
        dd = defaultdict(Counter)
        for key, counter in (model or {}).items():
            dd[key] = Counter({k: float(v) for k, v in counter.items()})
        bank[str(order)] = dd
    return bank


def _serialize_float_map(src: Dict[str, float]) -> Dict[str, float]:
    return {k: float(v) for k, v in src.items()}


def _deserialize_float_map(src: Dict[str, float]) -> defaultdict:
    out = defaultdict(float)
    for k, v in (src or {}).items():
        out[k] = float(v)
    return out


async def save_user_meta(chat_id: int, d: Dict[str, Any]) -> None:
    async with DB_LOCK:
        eval_json = json.dumps(d.get("eval_log", []), ensure_ascii=False)
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO users (chat_id, win, lose, last_pred, ai_mode, eval_log_json, updates)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    win=excluded.win,
                    lose=excluded.lose,
                    last_pred=excluded.last_pred,
                    ai_mode=excluded.ai_mode,
                    eval_log_json=excluded.eval_log_json,
                    updates=excluded.updates
                """,
                (
                    chat_id,
                    int(d.get("win", 0)),
                    int(d.get("lose", 0)),
                    d.get("last_pred"),
                    d.get("ai_mode", "NORMAL"),
                    eval_json,
                    int(d.get("updates", 0)),
                ),
            )


async def append_history(chat_id: int, tx: str, source: str = "real", conf: float = 1.0) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO history (chat_id, tx, source, conf) VALUES (?, ?, ?, ?)",
                (chat_id, tx, source, float(conf)),
            )


async def upsert_transition(chat_id: int, order_n: int, context: str, next_tx: str, inc: float = 1.0) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO transitions (chat_id, order_n, context, next_tx, count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, order_n, context, next_tx)
                DO UPDATE SET count = count + excluded.count
                """,
                (chat_id, order_n, context, next_tx, float(inc)),
            )


async def upsert_pattern(chat_id: int, pattern: str, inc: float) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO pattern_memory (chat_id, pattern, weight)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, pattern)
                DO UPDATE SET weight = weight + excluded.weight
                """,
                (chat_id, pattern, float(inc)),
            )


async def delete_chat_state(chat_id: int) -> None:
    async with DB_LOCK:
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM transitions WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM pattern_memory WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))


async def wipe_all_state() -> None:
    async with DB_LOCK:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)


async def load_user(chat_id: int) -> Dict[str, Any]:
    if chat_id in users:
        return users[chat_id]

    state = new_user()

    async with DB_LOCK:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT win, lose, last_pred, ai_mode, eval_log_json, updates FROM users WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if row:
                state["win"] = int(row["win"])
                state["lose"] = int(row["lose"])
                state["last_pred"] = row["last_pred"]
                state["ai_mode"] = row["ai_mode"] or "NORMAL"
                state["eval_log"] = list(json.loads(row["eval_log_json"] or "[]"))
                state["updates"] = int(row["updates"])

            hist_rows = conn.execute(
                "SELECT tx FROM history WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
            state["tx_history"] = [r["tx"] for r in hist_rows if r["tx"] in ("Tài", "Xỉu")]

            trans_rows = conn.execute(
                "SELECT order_n, context, next_tx, count FROM transitions WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()

            trans_raw: Dict[str, Dict[str, Dict[str, float]]] = {}
            for r in trans_rows:
                order_n = str(int(r["order_n"]))
                trans_raw.setdefault(order_n, {})
                trans_raw[order_n].setdefault(r["context"], {})
                trans_raw[order_n][r["context"]][r["next_tx"]] = float(r["count"])
            state["transitions"] = _deserialize_transition_bank(trans_raw)

            pat_rows = conn.execute(
                "SELECT pattern, weight FROM pattern_memory WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
            state["pattern_memory"] = _deserialize_float_map({r["pattern"]: float(r["weight"]) for r in pat_rows})

    # preview history only (not all raw dicts)
    state["history"] = [{"tx": tx, "source": "real", "conf": 1.0} for tx in state["tx_history"][-50:]]
    users[chat_id] = state
    return state


def get_key(update: Update) -> int:
    return update.effective_chat.id


def to_tx(n: int) -> str:
    return "Tài" if n >= 11 else "Xỉu"


def parse_input(text: str) -> List[int]:
    nums = []
    for x in re.findall(r"\d+", text or ""):
        try:
            n = int(x)
            if n > 0:
                nums.append(n)
        except ValueError:
            pass
    return nums


def format_history(txs: List[str], tail: int = 30) -> str:
    out = []
    for tx in txs[-tail:]:
        if tx == "Tài":
            out.append("⚫")
        elif tx == "Xỉu":
            out.append("⚪")
    return " ".join(out) if out else "(trống)"


def normalize_user_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if "history" not in d:
        d["history"] = []
    if "tx_history" not in d:
        d["tx_history"] = []
    if "transitions" not in d:
        d["transitions"] = new_transition_bank()
    if "pattern_memory" not in d:
        d["pattern_memory"] = defaultdict(float)
    if "eval_log" not in d:
        d["eval_log"] = []
    if "win" not in d:
        d["win"] = 0
    if "lose" not in d:
        d["lose"] = 0
    if "last_pred" not in d:
        d["last_pred"] = None
    if "ai_mode" not in d:
        d["ai_mode"] = "NORMAL"
    if "updates" not in d:
        d["updates"] = 0
    return d


# =========================
# MODEL UPDATE
# =========================
def update_transitions_in_memory(d: Dict[str, Any], chat_id: int, tx: str) -> List[Tuple[int, str, str]]:
    """
    Return the transition updates to persist.
    """
    txs = d.get("tx_history", [])
    ops: List[Tuple[int, str, str]] = []
    for order in range(1, 6):
        if len(txs) >= order + 1:
            context = "|".join(txs[-(order + 1):-1])
            d["transitions"][str(order)][context][tx] += 1.0
            ops.append((order, context, tx))
    return ops


def update_pattern_memory_in_memory(d: Dict[str, Any]) -> List[Tuple[str, float]]:
    txs = d.get("tx_history", [])
    ops: List[Tuple[str, float]] = []
    if len(txs) >= 4:
        p4 = "|".join(txs[-4:])
        d["pattern_memory"][p4] += 1.0
        ops.append((p4, 1.0))
    if len(txs) >= 5:
        p5 = "|".join(txs[-5:])
        d["pattern_memory"][p5] += 0.7
        ops.append((p5, 0.7))
    return ops


# =========================
# ANALYSIS
# =========================
def transition_predict(d: Dict[str, Any], order: int) -> Optional[Tuple[str, float]]:
    txs = d.get("tx_history", [])
    if len(txs) < order + 1:
        return None

    key = "|".join(txs[-order:])
    model = d["transitions"].get(str(order), {})
    counter = model.get(key)
    if not counter:
        return None

    total = sum(counter.values())
    if total < max(3, order):
        return None

    best = max(counter, key=counter.get)
    ratio = counter[best] / total
    base_weight = {1: 12.0, 2: 16.0, 3: 22.0, 4: 28.0, 5: 34.0}[order]
    score = (ratio * base_weight) + min(total, 12.0)
    return best, score


def recent_suffix_predict(txs: List[str], order: int) -> Optional[Tuple[str, float]]:
    if len(txs) < order + 2:
        return None

    suffix = tuple(txs[-order:])
    next_counts = Counter()
    occur = 0

    start = 0
    if len(txs) > MAX_SUFFIX_SCAN:
        start = len(txs) - MAX_SUFFIX_SCAN

    for i in range(start, len(txs) - order):
        if tuple(txs[i:i + order]) == suffix and i + order < len(txs):
            age = len(txs) - (i + order)
            weight = 1.0 / (1.0 + age / 8.0)
            next_counts[txs[i + order]] += weight
            occur += 1

    if occur < 2 or not next_counts:
        return None

    best = max(next_counts, key=next_counts.get)
    total = sum(next_counts.values())
    score = (next_counts[best] / total) * (10.0 + order * 4.0)
    return best, score


def detect_cycle(d: Dict[str, Any]) -> Tuple[Optional[str], int]:
    h = d.get("tx_history", [])
    if len(h) < 10:
        return None, 0

    best_pred = None
    best_score = 0

    start = 0
    if len(h) > MAX_CYCLE_SCAN:
        start = len(h) - MAX_CYCLE_SCAN
    h2 = h[start:]

    max_size = min(10, len(h2) // 2)
    for size in range(2, max_size + 1):
        suffix = tuple(h2[-size:])
        next_counts = Counter()
        occur = 0

        for i in range(len(h2) - size):
            if tuple(h2[i:i + size]) == suffix and i + size < len(h2):
                occur += 1
                next_counts[h2[i + size]] += 1

        if occur >= 2 and next_counts:
            pred, cnt = next_counts.most_common(1)[0]
            score = min(int((cnt / occur) * 100), 85)
            if score > best_score:
                best_score = score
                best_pred = pred

    return best_pred, best_score


def streak_analysis(txs: List[str]) -> Tuple[Optional[str], int]:
    if len(txs) < 3:
        return None, 0

    last = txs[-1]
    streak = 1
    for i in range(len(txs) - 2, -1, -1):
        if txs[i] == last:
            streak += 1
        else:
            break

    if streak >= 6:
        return last, 82
    if streak == 5:
        return last, 78
    if streak == 4:
        return last, 70
    if streak == 3:
        return last, 60
    return None, 0


def alternating_analysis(txs: List[str]) -> Tuple[Optional[str], int]:
    if len(txs) < 6:
        return None, 0

    tail = txs[-6:]
    if all(tail[i] != tail[i - 1] for i in range(1, len(tail))):
        return ("Xỉu" if tail[-1] == "Tài" else "Tài"), 74

    return None, 0


def long_bias_analysis(txs: List[str]) -> Tuple[Optional[str], int]:
    if len(txs) < 20:
        return None, 0

    t = txs.count("Tài")
    x = txs.count("Xỉu")
    gap = abs(t - x)

    if gap >= 20:
        return ("Tài" if t < x else "Xỉu"), 82
    if gap >= 12:
        return ("Tài" if t < x else "Xỉu"), 76
    if gap >= 8:
        return ("Tài" if t < x else "Xỉu"), 68

    return None, 0


def volatility_penalty(txs: List[str]) -> float:
    tail = txs[-12:]
    if len(tail) < 8:
        return 1.0

    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    ratio = changes / (len(tail) - 1)

    if ratio >= 0.85 and abs(tail.count("Tài") - tail.count("Xỉu")) <= 2:
        return 0.82
    if ratio >= 0.75 and abs(tail.count("Tài") - tail.count("Xỉu")) <= 2:
        return 0.90
    return 1.0


def super_meta_ai(d: Dict[str, Any]) -> Tuple[str, str]:
    txs = d.get("tx_history", [])
    if len(txs) < 10:
        return "NORMAL", "📊 Đang học"

    t_total = txs.count("Tài")
    x_total = txs.count("Xỉu")
    gap_total = abs(t_total - x_total)

    tail = txs[-20:]
    zigzag = len(tail) >= 8 and all(tail[i] != tail[i - 1] for i in range(1, len(tail)))

    if zigzag and abs(tail.count("Tài") - tail.count("Xỉu")) <= 2:
        return "DANGER", "🚫 Cầu ảo (zigzag)"

    if d["lose"] >= 5:
        return "RECOVER", "♻️ Thua sâu"

    if gap_total >= 20:
        return "BALANCE", "⚖️ Lệch mạnh"

    if d["win"] >= 6 and d["lose"] == 0:
        return "OVERCONF", "⚠️ Win ảo"

    return "NORMAL", "✅ Ổn định"


def final_ai(d: Dict[str, Any]) -> Tuple[Optional[str], int, str, Dict[str, float]]:
    txs = d.get("tx_history", [])
    if len(txs) < 15:
        return None, 0, "📊 Thiếu dữ liệu", {}

    mode, note = super_meta_ai(d)
    d["ai_mode"] = mode

    if mode == "DANGER":
        return None, 0, note, {}

    score = defaultdict(float)
    breakdown: Dict[str, float] = {
        "Markov": 0.0,
        "Recent": 0.0,
        "Pattern": 0.0,
        "Cycle": 0.0,
        "Short": 0.0,
        "Long": 0.0,
    }

    for order in range(1, 6):
        pred = transition_predict(d, order)
        if pred:
            p, s = pred
            score[p] += s
            breakdown["Markov"] += s

    for order in range(2, 6):
        pred = recent_suffix_predict(txs, order)
        if pred:
            p, s = pred
            score[p] += s
            breakdown["Recent"] += s

    if len(txs) >= 3:
        key3 = "|".join(txs[-3:])
        for pat, cnt in d["pattern_memory"].items():
            parts = pat.split("|")
            if len(parts) >= 4 and "|".join(parts[:3]) == key3:
                add = min(float(cnt) * 1.8, 42.0)
                score[parts[3]] += add
                breakdown["Pattern"] += add

    cycle_pred, cycle_conf = detect_cycle(d)
    if cycle_pred:
        score[cycle_pred] += float(cycle_conf)
        breakdown["Cycle"] += float(cycle_conf)

    streak_pred, streak_conf = streak_analysis(txs)
    if streak_pred:
        add = float(streak_conf) * 0.70
        score[streak_pred] += add
        breakdown["Short"] += add

    alt_pred, alt_conf = alternating_analysis(txs)
    if alt_pred:
        score[alt_pred] += float(alt_conf)
        breakdown["Short"] += float(alt_conf)

    long_pred, long_conf = long_bias_analysis(txs)
    if long_pred:
        score[long_pred] += float(long_conf)
        breakdown["Long"] += float(long_conf)

    if not score:
        return None, 50, "❓ Không rõ", breakdown

    if mode == "BALANCE":
        for k in list(score.keys()):
            score[k] *= 0.84
    if mode == "RECOVER":
        for k in list(score.keys()):
            score[k] *= 0.74

    penalty = volatility_penalty(txs)
    for k in list(score.keys()):
        score[k] *= penalty

    best = max(score, key=score.get)
    total = sum(score.values())
    if len(score) > 1:
        sorted_scores = sorted(score.values(), reverse=True)
        margin = sorted_scores[0] - sorted_scores[1]
    else:
        margin = score[best]

    conf = int((score[best] / (total + 1.0)) * 100 + (margin / (total + 1.0)) * 18)
    conf = max(0, min(conf, 95))

    if mode == "OVERCONF":
        conf = int(conf * 0.86)

    if conf < 58:
        return None, conf, f"🛑 STOP | {note}", breakdown

    return best, conf, f"{note} | 🤖 AI CONTROL", breakdown


# =========================
# COMMANDS
# =========================
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        users.pop(chat_id, None)
        await delete_chat_state(chat_id)
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
        await update.message.reply_text("🧼 FACTORY RESET XONG: xóa sạch toàn bộ dữ liệu và file lưu")
    except Exception as e:
        logger.exception("factory_reset failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi factory reset")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = get_key(update)
        d = await load_user(chat_id)
        total = d["win"] + d["lose"]

        recent = d["eval_log"][-RECENT_EVAL_WINDOW:]
        recent_total = len(recent)
        recent_acc = int((sum(recent) / recent_total) * 100) if recent_total else 0
        overall_acc = int((d["win"] / total) * 100) if total else 0
        hist_len = len(d.get("tx_history", []))

        await update.message.reply_text(
            f"""
📊 THỐNG KÊ

• Win: {d['win']}
• Lose: {d['lose']}
• Tỷ lệ đúng tổng: {overall_acc}%
• Tỷ lệ đúng {recent_total} lượt gần nhất: {recent_acc}%
• Mode hiện tại: {d.get('ai_mode', 'NORMAL')}
• Số lượt đã học: {d.get('updates', 0)}
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
            """
📘 LỆNH HỖ TRỢ

/stats - xem thống kê
/reset - xóa dữ liệu của chat hiện tại
/factory_reset - xóa sạch toàn bộ bot, như mới tạo

Gửi các số để hệ thống tự học và phân tích.
Hỗ trợ cả chuỗi số dài trong một tin nhắn.
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
        d = normalize_user_state(await load_user(chat_id))

        nums = parse_input(update.message.text)
        if not nums:
            return

        for n in nums:
            tx = to_tx(n)

            if d["last_pred"]:
                correct = int(d["last_pred"] == tx)
                if correct:
                    d["win"] += 1
                else:
                    d["lose"] += 1
                d["eval_log"].append(correct)
                d["eval_log"] = d["eval_log"][-RECENT_EVAL_WINDOW:]

            d["history"].append({"tx": tx, "source": "real", "conf": 1.0})
            d["tx_history"].append(tx)
            d["updates"] += 1

            await append_history(chat_id, tx, "real", 1.0)

            ops = update_transitions_in_memory(d, chat_id, tx)
            for order_n, context_key, next_tx in ops:
                await upsert_transition(chat_id, order_n, context_key, next_tx, 1.0)

            for pattern, inc in update_pattern_memory_in_memory(d):
                await upsert_pattern(chat_id, pattern, inc)

            await save_user_meta(chat_id, d)

        msg = await update.message.reply_text("🧠 AI đang phân tích...")
        await asyncio.sleep(0.08)

        pred, conf, status, breakdown = final_ai(d)
        hist = format_history(d.get("tx_history", []))

        if pred is None:
            await msg.edit_text(
                f"""
╔══ 🚫 AI STOP ══╗
{status}

📊 {hist}
╚═══════════════╝
                """.strip()
            )
            await save_user_meta(chat_id, d)
            return

        d["last_pred"] = pred
        await save_user_meta(chat_id, d)

        pred_icon = "⚫" if pred == "Tài" else "⚪"
        await msg.edit_text(
            f"""
╔══ 🤖 AI MASTER ══╗
{status}

📊 {hist}

📈 Win: {d['win']} | ❌ Lose: {d['lose']}

🎯 {pred_icon} {pred.upper()}
🔥 {conf}%

🧩 Markov: {int(breakdown.get('Markov', 0))}
🧩 Recent: {int(breakdown.get('Recent', 0))}
🧩 Pattern: {int(breakdown.get('Pattern', 0))}
🧩 Cycle: {int(breakdown.get('Cycle', 0))}
🧩 Short: {int(breakdown.get('Short', 0))}
🧩 Long: {int(breakdown.get('Long', 0))}
╚══════════════════╝
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

    print("🔥 AI MASTER CONTROL RUNNING...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
