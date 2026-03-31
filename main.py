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

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

# ===== USER DATA =====
def new_user():
    return {
        "history": [],  # (Tài/Xỉu, real/fake, weight)
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "same_pred": 0,
        "markov3": defaultdict(Counter),
        "markov4": defaultdict(Counter)
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

# ===== INPUT SAFE =====
def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split()
            if x.isdigit() and 1 <= int(x) <= 18]

# ===== FAKE DATA =====
def fake_server(n=200):
    seq = []
    last = random.choice(["Tài", "Xỉu"])
    for _ in range(n):
        r = random.random()
        if r < 0.45:
            seq.append(last)
        elif r < 0.75:
            last = "Xỉu" if last == "Tài" else "Tài"
            seq.append(last)
        else:
            seq.append(random.choice(["Tài", "Xỉu"]))
        last = seq[-1]
    return [(x, "fake", 0.6) for x in seq]

# ===== FORMAT UI =====
def format_history(h):
    out = []
    for x,t,w in h[-20:]:
        icon = "⚫" if x=="Tài" else "⚪"
        if w < 0.6:
            icon = "🔸" + icon
        elif t == "fake":
            icon += "̶"
        out.append(icon)
    return " ".join(out)

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]

    if len(h) >= 4:
        key = tuple(x[0] for x in h[-4:-1])
        val = h[-1][0]
        w = max(0.1, min(h[-1][2], 1.5))
        d["markov3"][key][val] += w

    if len(h) >= 5:
        key = tuple(x[0] for x in h[-5:-1])
        val = h[-1][0]
        w = max(0.1, min(h[-1][2], 1.5))
        d["markov4"][key][val] += w

def markov_predict(d):
    h = [x[0] for x in d["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in d["markov3"]:
            c = d["markov3"][key]
            total = sum(c.values())
            if total > 0:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total > 0:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*120))

    return res

# ===== PATTERN =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    if len(set(h[-4:])) == 1:
        res.append(("BỆT", h[-1], 90))

    if all(h[i] != h[i-1] for i in range(-1, -6, -1)):
        res.append(("ZIGZAG", h[-2], 85))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 80)]

# ===== AI CORE =====
def final_ai(d):
    if len(d["history"]) < 15:
        return None, 0, [], "📊 Thiếu dữ liệu"

    score = defaultdict(float)

    for p,c in markov_predict(d):
        score[p] += c

    patterns = detect_patterns(d["history"])
    for _,p,w in patterns:
        score[p] += w

    for p,c in trend_score(d["history"]):
        score[p] += c

    if not score:
        return None,50,patterns,"❓ Không rõ"

    best = max(score, key=score.get)
    conf = int(score[best]/sum(score.values())*100)

    if conf < 60:
        return None, conf, patterns, "🛑 STOP"

    return best, conf, patterns, "✅ OK"

# ===== GUARD =====
def ai_guard(d, pred, conf):
    if pred is None:
        return None, "🛑 STOP"

    h = [x[0] for x in d["history"]]

    if len(h)>=6 and len(set(h[-6:]))==1:
        return None, "🚨 Bệt"

    if d["same_pred"] >= 3:
        return None, "⚠️ Spam"

    if conf < 65:
        return None, "🛑 Low"

    return pred, "✅ Guard OK"

# ===== SELF-HEAL =====
def ai_self_heal(d):
    actions = []

    if d["lose"] == 1:
        for i in range(1, min(5,len(d["history"]))):
            tx,t,w = d["history"][-i]
            d["history"][-i] = (tx,t,max(0.1, w*0.5))
        d["markov3"].clear()
        d["markov4"].clear()
        actions.append("🔍 Thua 1 → giảm weight")

    if d["lose"] >= 2:
        for i in range(len(d["history"])):
            tx,t,w = d["history"][i]
            d["history"][i] = (tx,t,max(0.1, w*0.7))
        actions.append("💀 Thua 2 → giảm toàn bộ")

    return actions

# ===== AI MANAGER =====
def ai_manager(d):
    actions = []
    h = d["history"]

    if len(h) < 10:
        return actions

    last = [x[0] for x in h[-8:]]
    if last.count("Tài") == 8 or last.count("Xỉu") == 8:
        d["markov3"].clear()
        d["markov4"].clear()
        actions.append("🧠 Manager: lệch → reset")

    if len(h) > 0:
        fake_ratio = sum(1 for x in h if x[1]=="fake") / len(h)
        if fake_ratio > 0.5:
            for i in range(len(h)):
                tx,t,w = h[i]
                if t=="fake":
                    h[i]=(tx,t,max(0.1, w*0.3))
            actions.append("🧽 Manager: giảm fake")

    return actions

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    d = users[key]

    text = update.message.text.strip()

    if text == "/fake":
        d["history"] = fake_server()
        await update.message.reply_text("🎲 Fake loaded")
        return

    if text == "/reset":
        d["history"] = d["history"][-30:]
        await update.message.reply_text("🔄 Reset nhẹ")
        return

    if text == "/resetall":
        users[key] = new_user()
        await update.message.reply_text("💥 Reset ALL")
        return

    nums = parse_input(text)
    if not nums:
        return

    for n in nums:
        tx = to_tx(n)

        if d["last_pred"]:
            if d["last_pred"] == tx:
                d["win"] += 1
                d["lose"] = 0
            else:
                d["lose"] += 1

        d["history"].append((tx, "real", 1.0))
        update_markov(d)

        await update.message.reply_text(f"🎲 {n} → {'⚫' if tx=='Tài' else '⚪'}")

    # MANAGER
    m = ai_manager(d)
    if m:
        await update.message.reply_text("🧠 AI MANAGER:\n" + "\n".join(m))

    # SELF HEAL
    s = ai_self_heal(d)
    if s:
        await update.message.reply_text("🛠 SELF-HEAL:\n" + "\n".join(s))

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    pred, conf, _, status = final_ai(d)

    if pred == d.get("last_pred"):
        d["same_pred"] += 1
    else:
        d["same_pred"] = 0

    pred, guard = ai_guard(d, pred, conf)

    hist = format_history(d["history"])

    if pred is None:
        await wait.edit_text(f"{status} | {guard}\n\n📊 {hist}")
        return

    d["last_pred"] = pred

    await wait.edit_text(
        f"""{status} | {guard}

📊 {hist}

📈 Win: {d['win']} | ❌ Lose: {d['lose']}

🎯 {'⚫ TÀI' if pred=='Tài' else '⚪ XỈU'}
🔥 {conf}%"""
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("fake", handle))
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(CommandHandler("resetall", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT AI FINAL STABLE RUNNING...")
    app.run_polling()
