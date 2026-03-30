import os
import asyncio
import logging
import random
from collections import Counter, defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

MAX_HISTORY = 300

def new_user():
    return {
        "history":[],
        "win":0,
        "lose":0,
        "last_pred":None,
        "last_patterns":[],

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

# ===== HISTORY =====
def format_history(h, n=20):
    return " ".join(["T" if x=="Tài" else "X" for x in h[-n:]])

# ===== MARKOV =====
def update_markov(data):
    h = data["history"]
    if len(h) >= 4:
        data["markov3"][tuple(h[-4:-1])][h[-1]] += 1
    if len(h) >= 5:
        data["markov4"][tuple(h[-5:-1])][h[-1]] += 1

def markov_predict(data):
    h = data["history"]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in data["markov3"]:
            c = data["markov3"][key]
            t = sum(c.values())
            if t >= 5:
                p = c.most_common(1)[0][0]
                res.append((p, c[p]/t * 100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in data["markov4"]:
            c = data["markov4"][key]
            t = sum(c.values())
            if t >= 5:
                p = c.most_common(1)[0][0]
                res.append((p, c[p]/t * 120))

    return res

# ===== PATTERN =====
def detect_patterns(h):
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

    if len(set(h[-4:])) == 2:
        res.append(("GÃY", h[-1], 75))

    if h[-3]==h[-2] and h[-1]!=h[-2]:
        res.append(("2-1", h[-2], 70))

    return res

# ===== TREND =====
def trend_score(h):
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 90)]

# ===== AUTO =====
def detect_noise(h):
    if len(h) < 12:
        return False
    flip = sum(1 for i in range(1,12) if h[-i]!=h[-i-1])
    return flip >= 9

def remove_noise(data):
    data["history"] = data["history"][:-12]

def detect_bad_data(data):
    bad = 0
    for v in data["pattern_stats"].values():
        t = v["win"] + v["lose"]
        if t >= 5 and v["win"]/t < 0.4:
            bad += 1
    return bad >= 2

def reset_logic(data):
    data["markov3"].clear()
    data["markov4"].clear()
    data["pattern_stats"].clear()
    data["lose"] = 0

# ===== AI =====
def final_ai(data):
    h = data["history"]
    if len(h) < 15:
        return None,0,[],"📊 Chưa đủ"

    score = defaultdict(float)

    for p,c in markov_predict(data):
        score[p] += c

    patterns = detect_patterns(h)
    for name,p,w in patterns:
        stat = data["pattern_stats"][name]
        t = stat["win"]+stat["lose"]
        if t >= 5:
            w *= (stat["win"]/t + 0.5)
        score[p] += w

    for p,c in trend_score(h):
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

        data["history"].append(tx)
        update_markov(data)

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    # ===== AUTO FLOW =====
    if detect_noise(data["history"]):
        remove_noise(data)
        await wait.edit_text("🧹 AUTO: Nhiễu → Đã xoá 12 ván")
        return

    if data["lose"] >= 3:
        users[key] = new_user()
        await wait.edit_text("💥 AUTO: Thua sâu → Reset ALL")
        return

    if data["lose"] >= 2:
        reset_logic(data)
        await wait.edit_text("⚠️ AUTO: Thua 2 → Reset logic")
        return

    if detect_bad_data(data):
        data["pattern_stats"].clear()
        await wait.edit_text("🧠 AUTO: Data xấu → Đã làm sạch")
        return

    # ===== AI =====
    pred,conf,patterns,status = final_ai(data)

    hist = format_history(data["history"])
    pattern_text = "\n".join([f"• {p[0]} → {p[1]}" for p in patterns]) or "Không rõ"

    if pred is None:
        await wait.edit_text(
            f"{status}\n\n📜 {hist}\n\n📊 Pattern:\n{pattern_text}\n\n🚫 BỎ QUA\n📉 {conf}%"
        )
        return

    data["last_pred"] = pred
    data["last_patterns"] = patterns

    level = "🔥 CẦU ĐẸP" if conf >= 80 else "✅ ỔN"

    await wait.edit_text(
        f"{status} - {level}\n\n📜 {hist}\n\n📊 Pattern:\n{pattern_text}\n\n🎯 {pred}\n📈 {conf}%"
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(CommandHandler("resetall", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
