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

# ===== GLOBAL =====
users = {}
markov3 = defaultdict(Counter)
markov5 = defaultdict(Counter)
markov7 = defaultdict(Counter)

# ===== AI GUARD =====
def safe_run(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.error(f"🔥 Lỗi AI: {e}")
            return None
    return wrapper

# ===== SANITIZER =====
def clean_input(text):
    parts = text.replace("-", " ").split()
    nums = []
    for p in parts:
        if p.isdigit():
            n = int(p)
            if 1 <= n <= 18:
                nums.append(n)
    return nums[:50]  # chống spam

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

# ===== ANTI BIAS =====
def anti_bias(history, pred):
    if len(history) < 10:
        return pred

    c = Counter(history)
    diff = abs(c["Tài"] - c["Xỉu"])

    if diff > len(history)*0.4:
        return "Xỉu" if pred == "Tài" else "Tài"

    return pred

# ===== NOISE FILTER =====
def noise_filter(history):
    if len(history) < 6:
        return history

    filtered = []
    for i in range(len(history)):
        if i >= 2 and history[i] == history[i-1] == history[i-2]:
            continue
        filtered.append(history[i])
    return filtered

# ===== MARKOV =====
@safe_run
def update_markov(history, idx):
    for k in [3,5,7]:
        if len(history) >= k+1:
            key = tuple(history[idx-k:idx])
            nxt = history[idx]
            if k == 3:
                markov3[key][nxt] += 1
            elif k == 5:
                markov5[key][nxt] += 1
            elif k == 7:
                markov7[key][nxt] += 1

@safe_run
def markov_predict(history):
    preds = []

    def get(table, k, w):
        if len(history) >= k:
            key = tuple(history[-k:])
            if key in table:
                nxt = table[key]
                p = nxt.most_common(1)[0][0]
                conf = nxt[p]/sum(nxt.values())
                preds.append(("markov", p, conf*w))

    get(markov7,7,120)
    get(markov5,5,100)
    get(markov3,3,80)
    return preds

# ===== STREAK =====
def streak(history):
    if len(history) < 3:
        return []
    last = history[-1]
    count = 1
    for i in range(len(history)-2,-1,-1):
        if history[i] == last:
            count += 1
        else:
            break
    if count >= 4:
        return [("streak","Xỉu" if last=="Tài" else "Tài",90)]
    return [("streak",last,55)]

# ===== FREQ =====
def freq(history):
    if len(history) < 5:
        return []
    c = Counter(history)
    p = c.most_common(1)[0][0]
    return [("freq",p,c[p]/len(history)*100)]

# ===== DEEP =====
def deep(history):
    preds = []
    for k in range(4,7):
        if len(history) >= k:
            seq = tuple(history[-k:])
            nexts = Counter()
            for i in range(len(history)-k):
                if tuple(history[i:i+k]) == seq:
                    nexts[history[i+k]] += 1
            if nexts:
                p = nexts.most_common(1)[0][0]
                preds.append(("deep",p,70+k*5))
    return preds

# ===== SELF HEAL =====
def self_heal(data):
    try:
        if data["total"] < data["win"]:
            data["win"] = 0
        if data["total"] < 0:
            data["total"] = 0
    except:
        data["win"] = 0
        data["total"] = 0

# ===== AI =====
def predict(data):
    history = noise_filter(data["history"])

    preds = []
    preds += markov_predict(history) or []
    preds += streak(history)
    preds += freq(history)
    preds += deep(history)

    if not preds:
        return random.choice(["Tài","Xỉu"]),50

    score = defaultdict(float)

    for name,p,c in preds:
        score[p] += c

    final = max(score, key=score.get)

    final = anti_bias(history, final)

    total = sum(score.values())
    conf = int(score[final]/total*100) if total else 50

    return final, conf

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.effective_chat.id

    users.setdefault(key,{
        "history":[],
        "win":0,
        "lose":0,
        "total":0,
        "last":None
    })

    data = users[key]

    if update.message.text == "/reset":
        users[key] = {
            "history":[],
            "win":0,"lose":0,"total":0,
            "last":None
        }
        await update.message.reply_text("Đã reset")
        return

    nums = clean_input(update.message.text)
    if not nums:
        return

    for n in nums:
        tx = to_tx(n)

        if data["last"]:
            if data["last"] == tx:
                data["win"] += 1
            else:
                data["lose"] += 1

        data["history"].append(tx)
        data["total"] += 1

        update_markov(data["history"], len(data["history"])-1)

        if len(data["history"]) > 300:
            data["history"] = data["history"][-300:]

    self_heal(data)

    await update.message.reply_text("🤖 Đang phân tích an toàn...")

    await asyncio.sleep(1)

    pred, conf = predict(data)
    data["last"] = pred

    rate = int((data["win"]/data["total"])*100) if data["total"] else 0

    await update.message.reply_text(
        f"🎯 Dự đoán: {pred} ({conf}%)\n"
        f"📊 Tỷ lệ thắng: {rate}%\n"
        f"🧠 Chế độ: Ultra Safe AI"
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
