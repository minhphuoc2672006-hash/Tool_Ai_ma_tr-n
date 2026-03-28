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

# ===== AI WEIGHT (TỰ HỌC) =====
ai_weight = {
    "markov": 1.5,
    "streak": 1.0,
    "pattern": 1.0,
    "freq": 0.8
}

# ===== UTIL =====
def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    parts = text.replace("-", " ").split()
    return [int(p) for p in parts if p.isdigit() and 1 <= int(p) <= 18]

# ===== MARKOV FULL =====
def markov_full_predict(history):
    preds = []

    n = len(history)
    if n < 2:
        return preds

    score = Counter()

    for k in range(n-1, 0, -1):
        key = tuple(history[-k:])
        match_count = 0

        for i in range(n - k):
            if tuple(history[i:i+k]) == key:
                nxt = history[i+k]

                recency = (i + 1) / n
                weight = (k * 15) + (recency * 10)

                score[nxt] += weight
                match_count += 1

        if match_count >= 2 and k > 3:
            break

    if not score:
        return preds

    total = sum(score.values())
    pred = score.most_common(1)[0][0]
    conf = score[pred] / total

    preds.append((pred, conf * 200))
    return preds

# ===== STREAK =====
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

    if count >= 4:
        return [("Xỉu" if last=="Tài" else "Tài", 90)]
    elif count == 3:
        return [("Xỉu" if last=="Tài" else "Tài", 75)]
    else:
        return [(last, 55)]

# ===== PATTERN =====
def pattern_analysis(history):
    preds = []

    if len(history) >= 6:
        if history[-1] != history[-2] and history[-3] != history[-4]:
            preds.append((history[-2], 70))

        if history[-1] == history[-3]:
            preds.append((history[-1], 65))

    return preds

# ===== FREQ =====
def freq_analysis(history):
    if len(history) < 6:
        return []

    c = Counter(history)
    total = len(history)

    pred = c.most_common(1)[0][0]
    conf = c[pred]/total * 100

    return [(pred, conf)]

# ===== 💀 PHÁT HIỆN CẦU CHẾT =====
def detect_bad_bridge(data):
    history = data["history"]
    lose = data["lose"]

    # thua chuỗi
    if lose >= 3:
        return True

    # nhiễu: T X T X T X
    if len(history) >= 6:
        alt = True
        for i in range(1,6):
            if history[-i] == history[-i-1]:
                alt = False
                break
        if alt:
            return True

    return False

# ===== 🤖 UPDATE AI WEIGHT =====
def update_ai_weight(data, real):
    last_ai = data.get("last_ai")

    if not last_ai:
        return

    if last_ai == real:
        ai_weight[data["last_ai_type"]] *= 1.05
    else:
        ai_weight[data["last_ai_type"]] *= 0.95

# ===== AI TỔNG =====
def final_ai(data):
    history = data["history"]

    if detect_bad_bridge(data):
        return None, 0, "STOP"

    all_preds = []

    sources = {
        "markov": markov_full_predict(history),
        "streak": streak_analysis(history),
        "pattern": pattern_analysis(history),
        "freq": freq_analysis(history)
    }

    score = defaultdict(float)

    for name, preds in sources.items():
        for p,c in preds:
            score[p] += c * ai_weight[name]

    if not score:
        return random.choice(["Tài","Xỉu"]), 50, "RANDOM"

    final = max(score, key=score.get)
    conf = int(score[final] / sum(score.values()) * 100)

    # lưu AI dùng
    data["last_ai"] = final
    data["last_ai_type"] = max(sources, key=lambda k: sum([c for _,c in sources[k]]) if sources[k] else 0)

    return final, conf, "OK"

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)

    users.setdefault(key,{
        "history":[],
        "win":0,
        "lose":0,
        "total":0,
        "last_pred":None,
        "last_ai":None,
        "last_ai_type":None
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

        # update AI học
        update_ai_weight(data, tx)

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

    pred, conf, status = final_ai(data)

    if status == "STOP":
        await wait.edit_text("⛔ Cầu chết - không nên đánh")
        return

    data["last_pred"] = pred

    await wait.edit_text(f"Dự đoán: {pred}\nTỷ lệ: {conf}%")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
