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

# ===== DATA =====
users = {}
markov3 = defaultdict(Counter)
markov5 = defaultdict(Counter)
markov7 = defaultdict(Counter)

# ===== UTIL =====
def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    parts = text.replace("-", " ").split()
    return [int(p) for p in parts if p.isdigit() and 1 <= int(p) <= 18]

# ===== MARKOV UPDATE =====
def update_markov(history):
    for k in [3,5,7]:
        if len(history) >= k+1:
            for i in range(len(history)-k):
                key = tuple(history[i:i+k])
                nxt = history[i+k]
                if k == 3:
                    markov3[key][nxt] += 1
                elif k == 5:
                    markov5[key][nxt] += 1
                elif k == 7:
                    markov7[key][nxt] += 1

# ===== MARKOV PREDICT =====
def markov_predict(history):
    preds = []

    def get_pred(table, k, weight):
        if len(history) >= k:
            key = tuple(history[-k:])
            if key in table:
                nxt = table[key]
                pred = nxt.most_common(1)[0][0]
                conf = nxt[pred]/sum(nxt.values())
                preds.append((pred, conf * weight))

    get_pred(markov7, 7, 120)
    get_pred(markov5, 5, 100)
    get_pred(markov3, 3, 80)

    return preds

# ===== PHÂN TÍCH CHUỖI =====
def streak_analysis(history):
    if len(history) < 3:
        return []

    last = history[-1]
    count = 1
    for i in range(len(history)-2, -1, -1):
        if history[i] == last:
            count += 1
        else:
            break

    preds = []

    if count >= 4:
        preds.append(("Xỉu" if last=="Tài" else "Tài", 90))
    elif count == 3:
        preds.append(("Xỉu" if last=="Tài" else "Tài", 75))
    else:
        preds.append((last, 55))

    return preds

# ===== PATTERN =====
def pattern_analysis(history):
    preds = []

    if len(history) >= 6:
        if history[-1] != history[-2] and history[-3] != history[-4]:
            preds.append((history[-2], 70))

        if history[-1] == history[-3]:
            preds.append((history[-1], 65))

    return preds

# ===== TẦN SUẤT =====
def freq_analysis(history):
    if len(history) < 6:
        return []

    c = Counter(history)
    total = len(history)

    pred = c.most_common(1)[0][0]
    conf = c[pred]/total * 100

    return [(pred, conf)]

# ===== ANTI LOSE =====
def anti_lose(data):
    return data["lose"] >= 2

# ===== NHẬN DIỆN CẦU SẮP GÃY =====
def detect_break_risk(history):
    if len(history) < 6:
        return 0

    risk = 0

    last = history[-1]
    count = 1
    for i in range(len(history)-2, -1, -1):
        if history[i] == last:
            count += 1
        else:
            break

    if count >= 3:
        risk += 1

    if history[-1] != history[-2] and history[-2] != history[-3]:
        risk += 1

    if history[-1] != history[-3]:
        risk += 1

    return risk

# ===== RANDOM THÔNG MINH (ĐÃ NÂNG CẤP) =====
def smart_random(pred, conf, data):
    history = data["history"]
    risk = detect_break_risk(history)

    flip_rate = 0.05

    if risk == 1:
        flip_rate = 0.10
    elif risk >= 2:
        flip_rate = 0.15

    if data["lose"] >= 2:
        flip_rate = 0.20

    if data["lose"] >= 3:
        flip_rate = 0.25

    if conf < 55:
        flip_rate += 0.05

    if random.random() < flip_rate:
        pred = "Xỉu" if pred == "Tài" else "Tài"

    if flip_rate >= 0.2:
        noise = random.randint(-10, 10)
    else:
        noise = random.randint(-5, 5)

    conf = max(50, min(95, conf + noise))

    return pred, conf

# ===== AI TỔNG =====
def final_ai(data):
    history = data["history"]
    update_markov(history)

    all_preds = []
    all_preds += markov_predict(history)
    all_preds += streak_analysis(history)
    all_preds += pattern_analysis(history)
    all_preds += freq_analysis(history)

    if not all_preds:
        return random.choice(["Tài","Xỉu"]), 50

    score = defaultdict(float)

    for p,c in all_preds:
        score[p] += c

    final = max(score, key=score.get)

    if anti_lose(data):
        final = "Xỉu" if final=="Tài" else "Tài"

    conf = int(score[final] / sum(score.values()) * 100)

    # ===== RANDOM =====
    final, conf = smart_random(final, conf, data)

    return final, conf

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)

    users.setdefault(key,{
        "history":[],
        "win":0,
        "lose":0,
        "total":0,
        "last_pred":None
    })

    data = users[key]
    text = update.message.text.strip() if update.message.text else ""

    if text == "/reset":
        users[key] = {"history":[], "win":0,"lose":0,"total":0,"last_pred":None}
        await update.message.reply_text("Đã reset")
        return

    nums = parse_input(text)
    if not nums:
        return

    for n in nums:
        tx = to_tx(n)
        msg = f"Kết quả: {n} ({tx})"

        if data["last_pred"]:
            if data["last_pred"] == tx:
                data["win"] += 1
                data["lose"] = 0
                msg += f"\nDự đoán trước: {data['last_pred']} → ✅"
            else:
                data["lose"] += 1
                msg += f"\nDự đoán trước: {data['last_pred']} → ❌"

        await update.message.reply_text(msg)

        data["total"] += 1
        data["history"].append(tx)

        rate = int((data["win"]/data["total"])*100) if data["total"] else 0

        await update.message.reply_text(
            f"Tổng: {data['total']}\nThắng: {data['win']}\nThua: {data['lose']}\nTỷ lệ: {rate}%"
        )

    wait = await update.message.reply_text("Đang phân tích...")
    await asyncio.sleep(2)

    pred, conf = final_ai(data)
    data["last_pred"] = pred

    await wait.edit_text(f"Dự đoán: {pred}\nTỷ lệ: {conf}%")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
