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

MIN_SAMPLE = 20  # số mẫu tối thiểu để dự đoán

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

# ===== RANDOM AI =====
def random_ai_generate(n):
    return [random.choice(["Tài","Xỉu"]) for _ in range(n)]

def update_random_ai(data, real_results):
    preds = random_ai_generate(len(real_results))
    data["random_history"].extend(preds)

    for i in range(len(real_results)):
        if preds[i] == real_results[i]:
            data["random_win"] += 1
        data["random_total"] += 1

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

# ===== PHÂN TÍCH =====
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

def pattern_analysis(history):
    preds = []
    if len(history) >= 6:
        if history[-1] != history[-2] and history[-3] != history[-4]:
            preds.append((history[-2], 70))
        if history[-1] == history[-3]:
            preds.append((history[-1], 65))
    return preds

def freq_analysis(history):
    if len(history) < 6:
        return []
    c = Counter(history)
    total = len(history)
    pred = c.most_common(1)[0][0]
    conf = c[pred]/total * 100
    return [(pred, conf)]

# ===== AI CHÍNH =====
def final_ai(data):
    history = data["history"]

    # ❗ CHƯA ĐỦ MẪU
    if len(history) < MIN_SAMPLE:
        return None, None

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
    conf = int(score[final] / sum(score.values()) * 100)

    return final, conf

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)

    users.setdefault(key,{
        "history":[],
        "win":0,
        "lose":0,
        "total":0,
        "last_pred":None,

        # RANDOM AI
        "random_history":[],
        "random_win":0,
        "random_total":0
    })

    data = users[key]
    text = update.message.text.strip() if update.message.text else ""

    # RESET
    if text == "/reset":
        users[key] = {
            "history":[],
            "win":0,"lose":0,"total":0,"last_pred":None,
            "random_history":[],
            "random_win":0,"random_total":0
        }
        await update.message.reply_text("Đã reset toàn bộ")
        return

    nums = parse_input(text)
    if not nums:
        return

    real_results = []

    for n in nums:
        tx = to_tx(n)
        real_results.append(tx)

        msg = f"Kết quả: {n} ({tx})"

        if data["last_pred"]:
            if data["last_pred"] == tx:
                data["win"] += 1
                data["lose"] = 0
                msg += f"\nAI: {data['last_pred']} → ✅"
            else:
                data["lose"] += 1
                msg += f"\nAI: {data['last_pred']} → ❌"

        await update.message.reply_text(msg)

        data["total"] += 1
        data["history"].append(tx)

    # ===== RANDOM AI UPDATE =====
    update_random_ai(data, real_results)

    # ===== THỐNG KÊ =====
    rate = int((data["win"]/data["total"])*100) if data["total"] else 0
    random_rate = int((data["random_win"]/data["random_total"])*100) if data["random_total"] else 0

    await update.message.reply_text(
        f"📊 AI Chính\n"
        f"Tổng: {data['total']} | Thắng: {data['win']} | Thua: {data['lose']} | Rate: {rate}%\n\n"
        f"🎲 Random AI\n"
        f"Tổng: {data['random_total']} | Thắng: {data['random_win']} | Rate: {random_rate}%"
    )

    wait = await update.message.reply_text("Đang phân tích...")
    await asyncio.sleep(2)

    pred, conf = final_ai(data)

    if pred is None:
        await wait.edit_text(
            f"⚠️ Chưa đủ dữ liệu ({len(data['history'])}/{MIN_SAMPLE})\n"
            f"➡️ Chưa đủ tự tin để dự đoán"
        )
        return

    data["last_pred"] = pred

    await wait.edit_text(f"🔥 Dự đoán: {pred}\n🎯 Tỷ lệ: {conf}%")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
