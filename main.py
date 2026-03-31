import os
import asyncio
import logging
import random
from collections import Counter, defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_HISTORY = 300

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

# ===== USER =====
def new_user():
    return {
        "history": [],
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "same_pred": 0,
        "markov3": defaultdict(Counter),
        "markov4": defaultdict(Counter),
        "ai_bias": {"Tài": 1.0, "Xỉu": 1.0},
        "confidence_memory": []
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split()
            if x.isdigit() and 1 <= int(x) <= 18]

# ===== UI =====
def format_history(h):
    out = []
    for x,t,w in h[-20:]:
        icon = "⚫" if x=="Tài" else "⚪"
        if w < 0.6:
            icon = "🔸" + icon
        out.append(icon)
    return " ".join(out)

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]

    if len(h) >= 4:
        key = tuple(x[0] for x in h[-4:-1])
        d["markov3"][key][h[-1][0]] += 1

    if len(h) >= 5:
        key = tuple(x[0] for x in h[-5:-1])
        d["markov4"][key][h[-1][0]] += 1

def markov_predict(d):
    h = [x[0] for x in d["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in d["markov3"]:
            c = d["markov3"][key]
            total = sum(c.values())
            if total:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*120))

    return res

# ===== ADVANCED PATTERN =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    # Bệt
    if len(set(h[-4:])) == 1:
        res.append(("BỆT", h[-1], 92))

    # Zigzag
    if all(h[i] != h[i-1] for i in range(-1, -6, -1)):
        res.append(("ZIGZAG", h[-2], 85))

    # Cầu 2-1
    if h[-3:] == [h[-1], h[-1], h[-2]]:
        res.append(("2-1", h[-2], 85))

    # Cầu gãy
    if len(set(h[-6:-2])) > 1 and len(set(h[-2:])) == 1:
        res.append(("GÃY", h[-1], 87))

    return res

# ===== CẦU ẢO / CẦU DỤ =====
def detect_fake_trap(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    # cầu dụ: zigzag rồi bệt
    if all(h[i] != h[i-1] for i in range(-6, -2)) and len(set(h[-2:])) == 1:
        res.append(("CẦU DỤ", h[-1], 95))

    # cầu ảo: đổi liên tục không pattern
    if len(set(h[-6:])) > 1 and not all(h[i] != h[i-1] for i in range(-1, -6, -1)):
        res.append(("CẦU ẢO", None, 0))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 70)]

# ===== AI CONTROL =====
def ai_control(d, score, patterns, traps):
    notes = []

    # nếu có cầu ảo → stop
    for name,p,_ in traps:
        if name == "CẦU ẢO":
            return None, "🚫 Cầu ảo → STOP"

    # nếu cầu dụ → đảo nhẹ
    for name,p,_ in traps:
        if name == "CẦU DỤ":
            score[p] += 40
            notes.append("🎭 Bắt cầu dụ")

    # kiểm tra độ ổn định
    if len(d["confidence_memory"]) >= 3:
        if sum(d["confidence_memory"][-3:]) / 3 < 65:
            return None, "⚠️ AI không ổn định"

    return score, notes

# ===== AI =====
def final_ai(d):
    if len(d["history"]) < 15:
        return None, 0, [], [], "📊 Thiếu dữ liệu"

    score = defaultdict(float)

    for p,c in markov_predict(d):
        score[p] += c

    patterns = detect_patterns(d["history"])
    for name,p,w in patterns:
        score[p] += w

    traps = detect_fake_trap(d["history"])

    for p,c in trend_score(d["history"]):
        score[p] += c

    # bias
    for k in score:
        score[k] *= d["ai_bias"][k]

    # control layer
    ctrl, notes = ai_control(d, score, patterns, traps)
    if ctrl is None:
        return None, 0, patterns, traps, notes

    score = ctrl

    if not score:
        return None,50,patterns,traps,"❓ Không rõ"

    best = max(score, key=score.get)
    conf = int(score[best]/sum(score.values())*100)

    d["confidence_memory"].append(conf)

    if conf < 60:
        return None, conf, patterns, traps, "🛑 STOP"

    return best, conf, patterns, traps, "✅ OK"

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    d = users[key]

    text = update.message.text.strip()
    nums = parse_input(text)

    if not nums:
        return

    results = []

    for n in nums:
        tx = to_tx(n)
        results.append(f"{n}→{'⚫' if tx=='Tài' else '⚪'}")

        if d["last_pred"]:
            if d["last_pred"] == tx:
                d["win"] += 1
                d["lose"] = 0
                d["ai_bias"][tx] *= 1.05
            else:
                d["lose"] += 1
                d["ai_bias"][d["last_pred"]] *= 0.9

        d["history"].append((tx, "real", 1.0))

        if len(d["history"]) > MAX_HISTORY:
            d["history"] = d["history"][-MAX_HISTORY:]

        update_markov(d)

    await update.message.reply_text("🎲 " + " | ".join(results))

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    pred, conf, patterns, traps, status = final_ai(d)

    hist = format_history(d["history"])

    p_text = "\n".join([f"• {n} → {p}" for n,p,_ in patterns[:3]])
    t_text = "\n".join([f"• {n}" for n,_,_ in traps[:2]])

    if pred is None:
        await wait.edit_text(
f"""🚫 {status}

📊 {hist}

🔍 Pattern:
{p_text if p_text else "• Không rõ"}

⚠️ Trap:
{t_text if t_text else "• Không có"}
"""
        )
        return

    d["last_pred"] = pred

    await wait.edit_text(
f"""╔══ 🤖 BOT AI ══╗
{status}

📊 {hist}

📈 Win: {d['win']} | ❌ Lose: {d['lose']}

🔍 Pattern:
{p_text if p_text else "• Không rõ"}

⚠️ Trap:
{t_text if t_text else "• Không có"}

🎯 {'⚫ TÀI' if pred=='Tài' else '⚪ XỈU'}
🔥 {conf}%
╚═══════════════╝"""
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 ULTRA AI CONTROL RUNNING...")
    app.run_polling()
