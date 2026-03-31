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

# ===== USER =====
def new_user():
    return {
        "history": [],
        "win": 0,
        "lose": 0,
        "win_streak": 0,
        "last_pred": None,
        "mode": "normal",
        "markov3": defaultdict(Counter),
        "markov4": defaultdict(Counter),
        "same_pred": 0
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split() if x.isdigit() and 1 <= int(x) <= 18]

# ===== FAKE =====
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
    return [(x, "fake") for x in seq]

# ===== FORMAT =====
def format_history(h):
    return " ".join(("⚫" if x=="Tài" else "⚪") + ("̶" if t=="fake" else "") for x,t in h[-20:])

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]
    if len(h) >= 4:
        d["markov3"][tuple(x[0] for x in h[-4:-1])][h[-1][0]] += 1
    if len(h) >= 5:
        d["markov4"][tuple(x[0] for x in h[-5:-1])][h[-1][0]] += 1

def markov_predict(d):
    h = [x[0] for x in d["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in d["markov3"]:
            c = d["markov3"][key]
            total = sum(c.values())
            if total >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*120))

    return res

# ===== PATTERN FULL =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    # BỆT
    streak = 1
    for i in range(len(h)-1, 0, -1):
        if h[i] == h[i-1]:
            streak += 1
        else:
            break

    if streak >= 3:
        res.append(("BỆT NGẮN", h[-1], 80))
    if streak >= 5:
        res.append(("BỆT DÀI", h[-1], 100))

    # ZIGZAG
    if all(h[i] != h[i-1] for i in range(-1, -6, -1)):
        res.append(("ZIGZAG", h[-2], 90))

    # 2-1
    if h[-1] == h[-2] and h[-3] != h[-2]:
        res.append(("2-1", h[-1], 75))

    # 2-2
    if len(h) >= 6:
        if h[-1]==h[-2] and h[-3]==h[-4] and h[-2]!=h[-3]:
            res.append(("2-2", h[-1], 85))

    # ĐẢO
    if h[-1] != h[-2] and h[-2] == h[-3]:
        res.append(("ĐẢO", h[-1], 70))

    # GÃY
    if h[-1] != h[-2] != h[-3]:
        res.append(("GÃY", h[-1], 65))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 85)]

# ===== AI =====
def final_ai(d):
    if len(d["history"]) < 15:
        return None, 0, [], "📊 Thiếu dữ liệu"

    score = defaultdict(float)

    for p,c in markov_predict(d):
        score[p]+=c

    patterns = detect_patterns(d["history"])
    for name,p,w in patterns:
        score[p]+=w

    for p,c in trend_score(d["history"]):
        score[p]+=c

    if not score:
        return None,50,patterns,"❓ Không rõ"

    best = max(score, key=score.get)
    conf = int(score[best]/sum(score.values())*100)

    if conf < 60:
        return None, conf, patterns, "🛑 STOP"

    return best, conf, patterns, "✅ OK"

# ===== AI GUARD FULL =====
def ai_guard(d, pred, conf, patterns):
    if pred is None:
        return None, conf, "🛑 STOP"

    h = [x[0] for x in d["history"]]

    flip = sum(1 for i in range(1, min(len(h),10)) if h[-i]!=h[-i-1])
    if flip >= 8:
        return None, conf, "🧠 Nhiễu cao"

    if len(set(h[-6:])) == 1:
        return None, conf, "🚨 Bệt dài"

    if d["same_pred"] >= 3:
        return None, conf, "⚠️ Spam 1 cửa"

    if len(patterns)>=2 and patterns[0][1]!=patterns[1][1]:
        return None, conf-10, "⚠️ Xung đột"

    if conf < 65:
        return None, conf, "🛑 Guard chặn"

    return pred, conf, "✅ Guard OK"

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
        d["history"] = d["history"][-50:]
        d["markov3"].clear()
        d["markov4"].clear()
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
                d["win_streak"] += 1
            else:
                d["lose"] += 1
                d["win_streak"] = 0

        d["history"].append((tx, "real"))
        update_markov(d)

        await update.message.reply_text(f"🎲 {n} → {'⚫' if tx=='Tài' else '⚪'}")

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    pred, conf, patterns, status = final_ai(d)

    # track spam
    if pred == d.get("last_pred"):
        d["same_pred"] += 1
    else:
        d["same_pred"] = 0

    pred, conf, guard = ai_guard(d, pred, conf, patterns)

    hist = format_history(d["history"])

    if pred is None:
        await wait.edit_text(f"╔══ AI BOT ══╗\n{status} | {guard}\n\n{hist}")
        return

    d["last_pred"] = pred

    await wait.edit_text(
        f"╔══ AI BOT ══╗\n{status} | {guard}\n\n{hist}\n\n🎯 {'⚫' if pred=='Tài' else '⚪'} {pred}\n📈 {conf}%"
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("fake", handle))
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(CommandHandler("resetall", handle))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT FULL PRO MAX RUNNING...")
    app.run_polling()
