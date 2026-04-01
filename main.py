import os
import re
import json
import asyncio
import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STATE_FILE = "ai_state.json"

MAX_HISTORY = 500
RECENT_EVAL_WINDOW = 80
DECAY_EVERY_UPDATES = 120
DECAY_FACTOR = 0.992
PRUNE_BELOW = 0.35

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")


# =========================
# STATE
# =========================
def new_transition_bank() -> Dict[str, defaultdict]:
    return {str(order): defaultdict(Counter) for order in range(1, 6)}


def new_user() -> Dict[str, Any]:
    return {
        "history": [],
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "transitions": new_transition_bank(),      # order -> key -> Counter(next)
        "pattern_memory": defaultdict(float),      # pattern 4/5 -> weight
        "ai_mode": "NORMAL",
        "eval_log": [],                            # recent correctness log
        "updates": 0,                              # total observed outcomes
    }


users: Dict[int, Dict[str, Any]] = {}


# =========================
# HELPERS
# =========================
def get_key(update: Update) -> int:
    return update.effective_chat.id


def to_tx(n: int) -> str:
    return "Tài" if n >= 11 else "Xỉu"


def parse_input(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"\b(1[0-8]|[1-9])\b", text)]


def tx_of(item: Any) -> str:
    if isinstance(item, dict):
        return item.get("tx", "")
    if isinstance(item, (list, tuple)) and item:
        return item[0]
    return ""


def ensure_user(chat_id: int) -> Dict[str, Any]:
    users.setdefault(chat_id, new_user())
    return users[chat_id]


def history_txs(d: Dict[str, Any], limit: Optional[int] = None) -> List[str]:
    h = d["history"] if limit is None else d["history"][-limit:]
    out = []
    for item in h:
        tx = tx_of(item)
        if tx in ("Tài", "Xỉu"):
            out.append(tx)
    return out


def format_history(h: List[dict]) -> str:
    out = []
    for item in h[-20:]:
        tx = tx_of(item)
        if tx == "Tài":
            out.append("⚫")
        elif tx == "Xỉu":
            out.append("⚪")
    return " ".join(out) if out else "(trống)"


def serialize_transition_bank(bank: Dict[str, defaultdict]) -> Dict[str, Dict[str, Dict[str, float]]]:
    data: Dict[str, Dict[str, Dict[str, float]]] = {}
    for order, model in bank.items():
        data[order] = {}
        for key, counter in model.items():
            data[order][key] = {k: float(v) for k, v in counter.items()}
    return data


def deserialize_transition_bank(raw: Dict[str, Dict[str, Dict[str, float]]]) -> Dict[str, defaultdict]:
    bank = new_transition_bank()
    for order, model in raw.items():
        dd = defaultdict(Counter)
        for key, counter in model.items():
            dd[key] = Counter({k: float(v) for k, v in counter.items()})
        bank[order] = dd
    return bank


def serialize_float_map(src: Dict[str, float]) -> Dict[str, float]:
    return {k: float(v) for k, v in src.items()}


def deserialize_float_map(src: Dict[str, float]) -> defaultdict:
    out = defaultdict(float)
    for k, v in src.items():
        out[k] = float(v)
    return out


async def save_state() -> None:
    data = {}
    for chat_id, d in users.items():
        data[str(chat_id)] = {
            "history": d.get("history", []),
            "win": d.get("win", 0),
            "lose": d.get("lose", 0),
            "last_pred": d.get("last_pred"),
            "transitions": serialize_transition_bank(d.get("transitions", new_transition_bank())),
            "pattern_memory": serialize_float_map(d.get("pattern_memory", {})),
            "ai_mode": d.get("ai_mode", "NORMAL"),
            "eval_log": d.get("eval_log", []),
            "updates": d.get("updates", 0),
        }

    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state() -> None:
    global users
    if not os.path.exists(STATE_FILE):
        users = {}
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        loaded: Dict[int, Dict[str, Any]] = {}
        for chat_id_str, d in raw.items():
            state = new_user()
            state["history"] = d.get("history", [])
            state["win"] = int(d.get("win", 0))
            state["lose"] = int(d.get("lose", 0))
            state["last_pred"] = d.get("last_pred")
            state["ai_mode"] = d.get("ai_mode", "NORMAL")
            state["eval_log"] = list(d.get("eval_log", []))
            state["updates"] = int(d.get("updates", 0))

            if "transitions" in d:
                state["transitions"] = deserialize_transition_bank(d.get("transitions", {}))
            if "pattern_memory" in d:
                state["pattern_memory"] = deserialize_float_map(d.get("pattern_memory", {}))

            loaded[int(chat_id_str)] = state

        users = loaded
        logger.info("Loaded state for %d chat(s)", len(users))
    except Exception as e:
        logger.exception("Failed to load state: %s", e)
        users = {}


# =========================
# MODEL UPDATE
# =========================
def update_transitions(d: Dict[str, Any]) -> None:
    txs = history_txs(d)
    current = txs[-1]

    for order in range(1, 6):
        if len(txs) >= order + 1:
            key = "|".join(txs[-(order + 1):-1])
            d["transitions"][str(order)][key][current] += 1.0


def update_pattern_memory(d: Dict[str, Any]) -> None:
    txs = history_txs(d)
    if len(txs) >= 4:
        d["pattern_memory"]["|".join(txs[-4:])] += 1.0
    if len(txs) >= 5:
        d["pattern_memory"]["|".join(txs[-5:])] += 0.7


def apply_decay(d: Dict[str, Any]) -> None:
    for order in list(d["transitions"].keys()):
        model = d["transitions"][order]
        for key in list(model.keys()):
            counter = model[key]
            for nxt in list(counter.keys()):
                counter[nxt] = float(counter[nxt]) * DECAY_FACTOR
                if counter[nxt] < PRUNE_BELOW:
                    del counter[nxt]
            if not counter:
                del model[key]

    for key in list(d["pattern_memory"].keys()):
        d["pattern_memory"][key] = float(d["pattern_memory"][key]) * DECAY_FACTOR
        if d["pattern_memory"][key] < PRUNE_BELOW:
            del d["pattern_memory"][key]


def maybe_decay(d: Dict[str, Any]) -> None:
    if d.get("updates", 0) > 0 and d["updates"] % DECAY_EVERY_UPDATES == 0:
        apply_decay(d)


# =========================
# SHORT / LONG ANALYSIS
# =========================
def transition_predict(d: Dict[str, Any], order: int) -> Optional[Tuple[str, float]]:
    txs = history_txs(d)
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

    start = max(0, len(txs) - 40)
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
    h = history_txs(d, 60)
    if len(h) < 10:
        return None, 0

    best_pred = None
    best_score = 0

    for size in range(2, 8):
        suffix = tuple(h[-size:])
        next_counts = Counter()
        occur = 0

        for i in range(len(h) - size):
            if tuple(h[i:i + size]) == suffix and i + size < len(h):
                occur += 1
                next_counts[h[i + size]] += 1

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

    tail = txs[-40:]
    t = tail.count("Tài")
    x = tail.count("Xỉu")
    gap = abs(t - x)

    if gap >= 14:
        return ("Tài" if t < x else "Xỉu"), min(82, 58 + gap)
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


# =========================
# META AI
# =========================
def super_meta_ai(d: Dict[str, Any]) -> Tuple[str, str]:
    txs = history_txs(d, 20)
    if len(txs) < 10:
        return "NORMAL", "📊 Đang học"

    t = txs.count("Tài")
    x = txs.count("Xỉu")
    gap = abs(t - x)
    zigzag = all(txs[i] != txs[i - 1] for i in range(1, len(txs)))

    if zigzag and gap <= 3:
        return "DANGER", "🚫 Cầu ảo (zigzag)"

    if d["lose"] >= 5:
        return "RECOVER", "♻️ Thua sâu"

    if gap >= 12:
        return "BALANCE", "⚖️ Lệch mạnh"

    if d["win"] >= 6 and d["lose"] == 0:
        return "OVERCONF", "⚠️ Win ảo"

    return "NORMAL", "✅ Ổn định"


# =========================
# CORE AI
# =========================
def final_ai(d: Dict[str, Any]) -> Tuple[Optional[str], int, str, Dict[str, float]]:
    txs = history_txs(d)
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

    # Long-term transition models
    for order in range(1, 6):
        pred = transition_predict(d, order)
        if pred:
            p, s = pred
            score[p] += s
            breakdown["Markov"] += s

    # Short-term recent suffix patterns
    for order in range(2, 6):
        pred = recent_suffix_predict(txs, order)
        if pred:
            p, s = pred
            score[p] += s
            breakdown["Recent"] += s

    # Pattern memory
    if len(txs) >= 3:
        key3 = "|".join(txs[-3:])
        for pat, cnt in d["pattern_memory"].items():
            parts = pat.split("|")
            if len(parts) >= 4 and "|".join(parts[:3]) == key3:
                add = min(float(cnt) * 1.8, 42.0)
                score[parts[3]] += add
                breakdown["Pattern"] += add

    # Cycle detector
    cycle_pred, cycle_conf = detect_cycle(d)
    if cycle_pred:
        score[cycle_pred] += float(cycle_conf)
        breakdown["Cycle"] += float(cycle_conf)

    # Short-term heuristics
    streak_pred, streak_conf = streak_analysis(txs)
    if streak_pred:
        add = float(streak_conf) * 0.70
        score[streak_pred] += add
        breakdown["Short"] += add

    alt_pred, alt_conf = alternating_analysis(txs)
    if alt_pred:
        score[alt_pred] += float(alt_conf)
        breakdown["Short"] += float(alt_conf)

    # Long-term bias
    long_pred, long_conf = long_bias_analysis(txs)
    if long_pred:
        score[long_pred] += float(long_conf)
        breakdown["Long"] += float(long_conf)

    if not score:
        return None, 50, "❓ Không rõ", breakdown

    # Meta adjustments
    if mode == "BALANCE":
        for k in list(score.keys()):
            score[k] *= 0.84

    if mode == "RECOVER":
        for k in list(score.keys()):
            score[k] *= 0.74

    # Volatility penalty for noisy streaks
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
    chat_id = get_key(update)
    users[chat_id] = new_user()
    await save_state()
    await update.message.reply_text("🔄 RESET TOÀN BỘ AI")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_key(update)
    d = ensure_user(chat_id)
    total = d["win"] + d["lose"]

    recent = d["eval_log"][-RECENT_EVAL_WINDOW:]
    recent_total = len(recent)
    recent_acc = int((sum(recent) / recent_total) * 100) if recent_total else 0
    overall_acc = int((d["win"] / total) * 100) if total else 0

    await update.message.reply_text(
        f"""
📊 THỐNG KÊ

• Win: {d['win']}
• Lose: {d['lose']}
• Tỷ lệ đúng tổng: {overall_acc}%
• Tỷ lệ đúng {recent_total} lượt gần nhất: {recent_acc}%
• Mode hiện tại: {d.get('ai_mode', 'NORMAL')}
• Số lượt đã học: {d.get('updates', 0)}
        """.strip()
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """
📘 LỆNH HỖ TRỢ

/stats - xem thống kê
/reset - xóa toàn bộ dữ liệu đã học

Gửi các số từ 1 đến 18 để hệ thống tự học và phân tích.
        """.strip()
    )


# =========================
# MAIN HANDLE
# =========================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_key(update)
    d = ensure_user(chat_id)

    nums = parse_input(update.message.text)[:5]
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
        d["updates"] += 1

        update_transitions(d)
        update_pattern_memory(d)
        maybe_decay(d)

        if len(d["history"]) > MAX_HISTORY:
            d["history"] = d["history"][-MAX_HISTORY:]

    msg = await update.message.reply_text("🧠 AI đang phân tích...")
    await asyncio.sleep(0.08)

    pred, conf, status, breakdown = final_ai(d)
    hist = format_history(d["history"])

    if pred is None:
        await msg.edit_text(
            f"""
╔══ 🚫 AI STOP ══╗
{status}

📊 {hist}
╚═══════════════╝
            """.strip()
        )
        await save_state()
        return

    d["last_pred"] = pred
    await save_state()

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


# =========================
# RUN
# =========================
def main():
    load_state()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 AI MASTER CONTROL RUNNING...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
