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

# ===== USER =====
def new_user():
    return {
        "history":[],   # (value, type) → ("Tài","real/fake")
        "win":0,
        "lose":0,
        "last_pred":None,

        "markov3":defaultdict(Counter),
        "markov4":defaultdict(Counter),

        "pattern_stats":defaultdict(lambda: {"win":0,"lose":0})
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    parts = text.replace("-", " ").split()
    return [int(p) for p in parts if p.isdigit() and 1 <= int(p) <= 18]

# ===== FAKE SERVER =====
def fake_server(n=200):
    seq = []
    last = random.choice(["Tài","Xỉu"])

    for _ in range(n):
        r = random.random()

        if len(seq) < 3:
            seq.append(last)
            continue

        if r < 0.4:
            seq.append(last)
        elif r < 0.7:
            last = "Xỉu" if last=="Tài" else "Tài"
            seq.append(last)
        elif r < 0.9:
            seq.append(seq[-2])
        else:
            seq.append(random.choice(["Tài","Xỉu"]))

        last = seq[-1]

    return [(x,"fake") for x in seq]

# ===== FAKE CLONE =====
def fake_clone(data, n=100):
    real = [x for x,t in data["history"] if t=="real"]
    if len(real) < 10:
        return fake_server(n)

    seq = []
    for _ in range(n):
        seq.append(random.choice(real))

    return [(x,"fake") for x in seq]

# ===== FORMAT =====
def format_history(h, n=20):
    return " ".join(["T" if x[0]=="Tài" else "X" for x in h[-n:]])

# ===== MARKOV =====
def update_markov(data):
    h = data["history"]

    def weight(t):
        return 1 if t=="real" else 0.4

    if len(h) >= 4:
        key = tuple([x[0] for x in h[-4:-1]])
        val,typ = h[-1]
        data["markov3"][key][val] += weight(typ)

    if len(h) >= 5:
        key = tuple([x[0] for x in h[-5:-1]])
        val,typ = h[-1]
        data["markov4"][key][val] += weight(typ)

def markov_predict(data):
    h = [x[0] for x in data["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in data["markov3"]:
            c = data["markov3"][key]
            t = sum(c.values())
            if t >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/t*100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in data["markov4"]:
            c = data["markov4"][key]
            t = sum(c.values())
            if t >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/t*120))

    return res

# ===== PATTERN =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    last = h[-1]

    streak = 1
    for i in range(len(h)-2,-1,-1):
        if h[i]==last: streak+=1
        else: break
    if streak >= 4:
        res.append(("BỆT", last, 100))

    if all(h[i]!=h[i-1] for i in range(-1,-6,-1)):
        res.append(("ZIGZAG", h[-2], 90))

    if h[-1]!=h[-2] and h[-2]!=h[-3]:
        res.append(("1-1", h[-2], 80))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 90)]

# ===== AUTO =====
def detect_noise(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 12:
        return False
    flip = sum(1 for i in range(1,12) if h[-i]!=h[-i-1])
    return flip >= 9

def remove_noise(data):
    data["history"] = data["history"][:-12]

def reset_logic(data):
    data["markov3"].clear()
    data["markov4"].clear()
    data["pattern_stats"].clear()
    data["lose"] = 0

# ===== AI =====
def final_ai(data):
    if len(data["history"]) < 15:
        return None,0,[],"📊 Chưa đủ"

    score = defaultdict(float)

    for p,c in markov_predict(data):
        score[p] += c

    patterns = detect_patterns(data["history"])
    for name,p,w in patterns:
        score[p] += w

    for p,c in trend_score(data["history"]):
        score[p] += c

    if not score:
        return None,50,patterns,"❓ Không rõ"

    best = max(score, key=score.get)
    conf = int(score[best]/sum(score.values())*100)

    if conf < 65:
        return None,conf,patterns,"⚠️ Kèo yếu"

    return best,conf,patterns,"✅ OK"

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    data = users[key]

    text = update.message.text.strip()

    # ===== COMMAND =====
    if text.startswith("/fake"):
        data["history"] = fake_server(200)
        await update.message.reply_text("🎲 Fake SERVER 200 ván")
        return

    if text.startswith("/clone"):
        data["history"] += fake_clone(data,100)
        await update.message.reply_text("🧬 Fake CLONE 100 ván")
        return

    if text == "/reset":
        data["history"] = data["history"][-50:]
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
        msg = f"🎲 {n} → {tx}"

        if data["last_pred"]:
            if data["last_pred"] == tx:
                data["win"] += 1
                data["lose"] = 0
                msg += "\n✅ Đúng"
            else:
                data["lose"] += 1
                msg += "\n❌ Sai"

        await update.message.reply_text(msg)

        data["history"].append((tx,"real"))
        update_markov(data)

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    # AUTO
    if detect_noise(data["history"]):
        remove_noise(data)
        await wait.edit_text("🧹 AUTO: Nhiễu → Xoá đoạn")
        return

    if data["lose"] >= 3:
        users[key] = new_user()
        await wait.edit_text("💥 AUTO: Thua sâu → Reset ALL")
        return

    if data["lose"] >= 2:
        reset_logic(data)
        await wait.edit_text("⚠️ AUTO: Thua 2 → Reset logic")
        return

    # AI
    pred,conf,patterns,status = final_ai(data)

    hist = format_history(data["history"])
    pattern_text = "\n".join([f"• {p[0]} → {p[1]}" for p in patterns]) or "Không rõ"

    if pred is None:
        await wait.edit_text(
            f"{status}\n\n📜 {hist}\n\n📊 Pattern:\n{pattern_text}\n\n🚫 BỎ QUA\n📉 {conf}%"
        )
        return

    data["last_pred"] = pred

    level = "🔥 CẦU ĐẸP" if conf >= 80 else "✅ ỔN"

    await wait.edit_text(
        f"{status} - {level}\n\n📜 {hist}\n\n📊 Pattern:\n{pattern_text}\n\n🎯 {pred}\n📈 {conf}%"
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(CommandHandler("resetall", handle))
    app.add_handler(CommandHandler("fake", handle))
    app.add_handler(CommandHandler("clone", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
