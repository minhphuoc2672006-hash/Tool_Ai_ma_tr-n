import os
import logging
import asyncio
import random
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx_history(history):
    return [classify_total(sum(x)) for x in history]

# ===== AI MARKOV =====
def build_markov(history):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})
    tx = get_tx_history(history)

    for i in range(len(tx) - 3):
        seq = tuple(tx[i:i+3])
        mapping[seq][tx[i+3]] += 1

    return mapping

# ===== PHÂN TÍCH XU HƯỚNG =====
def analyze_trend(tx):
    if len(tx) < 5:
        return None

    last5 = tx[-5:]
    if last5.count("Tài") >= 4:
        return "Tài"
    if last5.count("Xỉu") >= 4:
        return "Xỉu"
    return None

# ===== AI DỰ ĐOÁN =====
def ai_predict(user):
    history = user["history"]
    tx = get_tx_history(history)
    mapping = build_markov(history)

    markov_pred = None
    confidence = 0

    # MARKOV
    if len(tx) >= 3:
        key = tuple(tx[-3:])
        data = mapping[key]
        total = data["Tài"] + data["Xỉu"]

        if total > 0:
            markov_pred = "Tài" if data["Tài"] > data["Xỉu"] else "Xỉu"
            confidence = max(data["Tài"], data["Xỉu"]) / total

    # TREND
    trend_pred = analyze_trend(tx)

    # COMBINE
    if markov_pred and trend_pred:
        if markov_pred == trend_pred:
            pred = markov_pred
            confidence += 0.15
        else:
            pred = markov_pred
    elif markov_pred:
        pred = markov_pred
    elif trend_pred:
        pred = trend_pred
    else:
        pred = random.choice(["Tài", "Xỉu"])

    # CASINO CONTROL
    winrate = user["win"] / (user["win"] + user["lose"] + 1)

    if winrate > 0.65:
        confidence -= 0.15
    elif winrate < 0.45:
        confidence += 0.15

    # RANDOM THÔNG MINH
    if winrate > 0.6:
        confidence -= 0.08
    elif winrate < 0.4:
        confidence += 0.08

    confidence = max(0.52, min(0.90, confidence))

    if random.random() > confidence:
        pred = "Xỉu" if pred == "Tài" else "Tài"

    return pred, confidence

# ===== % =====
def calculate_percent(conf):
    return conf * 100

# ===== GẤP THÉP =====
def calculate_bet(user):
    base = user["money"] * 0.05

    if user["lose"] == 0:
        bet = base
    else:
        bet = base * (2 ** (user["lose"] - 1))

    bet = min(bet, user["money"] * 0.9)
    return max(1, int(bet))

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI CASINO PRO\n\n"
        "💰 /setmoney 500000\n"
        "📥 Nhập xúc xắc: 3-5-6"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    m = int(context.args[0])

    users[uid] = {
        "money": m,
        "start_money": m,
        "profit": 0,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []
    }

    await update.message.reply_text(f"💰 Đã đặt vốn: {money(m)}")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Hãy dùng /setmoney trước")
        return

    user = users[uid]

    # RESET RANDOM mỗi lần nhập
    random.seed(str(user["history"]) + str(user["money"]))

    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]

    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập đúng dạng: 3-5-6")
        return

    dice = nums
    total = sum(dice)
    real = classify_total(total)

    msg_wait = await update.message.reply_text("⏳ AI đang phân tích...")
    await asyncio.sleep(1)

    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    result_text = "Chưa có"
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += user["last_bet"]
            user["profit"] += user["last_bet"]
            user["win"] += 1
            user["lose"] = 0
            result_text = "✅ Thắng"
        else:
            user["money"] -= user["last_bet"]
            user["profit"] -= user["last_bet"]
            user["lose"] += 1
            result_text = "❌ Thua"

    pred, conf = ai_predict(user)
    bet = calculate_bet(user)

    user["last_pred"] = pred
    user["last_bet"] = bet

    percent_total = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    msg = (
        "🤖 AI CASINO PRO\n"
        "━━━━━━━━━━━━\n"
        f"🎲 Kết quả: {dice} → {real}\n\n"
        f"📌 Trạng thái: {result_text}\n"
        "━━━━━━━━━━━━\n"
        f"🔮 Dự đoán tiếp: {pred}\n"
        f"📊 Độ tin cậy: {calculate_percent(conf):.1f}%\n"
        f"💰 Số tiền cược: {money(bet)}\n"
        "━━━━━━━━━━━━\n"
        f"💼 Số dư: {money(user['money'])}\n"
        f"📈 Lợi nhuận: {money(user['profit'])}\n"
        f"📊 Hiệu suất: {percent_total:.1f}%\n"
        "━━━━━━━━━━━━\n"
        f"🏆 Thắng: {user['win']} | ❌ Thua: {user['lose']}"
    )

    await msg_wait.edit_text(msg)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🤖 AI CASINO PRO ĐANG CHẠY...")
    app.run_polling()

if __name__ == "__main__":
    main()
