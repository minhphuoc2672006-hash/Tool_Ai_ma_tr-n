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
    tai = last5.count("Tài")
    xiu = last5.count("Xỉu")

    if tai >= 4:
        return "Tài"
    elif xiu >= 4:
        return "Xỉu"
    return None

# ===== AI DỰ ĐOÁN CHUẨN CASINO =====
def ai_predict(user):
    history = user["history"]
    tx = get_tx_history(history)
    mapping = build_markov(history)

    markov_pred = None
    confidence = 0

    # ===== MARKOV =====
    if len(tx) >= 5:
        key = tuple(tx[-5:])
        data = mapping[key]
        total = data["Tài"] + data["Xỉu"]

        if total > 0:
            markov_pred = "Tài" if data["Tài"] > data["Xỉu"] else "Xỉu"
            confidence = max(data["Tài"], data["Xỉu"]) / total

    # ===== TREND =====
    trend_pred = analyze_trend(tx)

    # ===== COMBINE =====
    pred = None

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

    # ===== FAKE CASINO CONTROL (55-65%) =====
    # nếu đang win nhiều → giảm accuracy
    # nếu đang thua → tăng accuracy
    winrate = user["win"] / (user["win"] + user["lose"] + 1)

    adjust = 0

    if winrate > 0.65:
        adjust = -0.15
    elif winrate < 0.45:
        adjust = +0.15

    confidence += adjust

    # ===== LÀM MỀM (ẨN RANDOM) =====
    confidence = max(0.51, min(0.93, confidence))

    # đôi khi đảo nhẹ (giống casino)
    if random.random() > confidence:
        pred = "Xỉu" if pred == "Tài" else "Tài"

    return pred, confidence

# ===== % HIỂN THỊ (DỰA TRÊN CONFIDENCE) =====
def calculate_percent(conf):
    return conf * 100

# ===== GẤP THÉP =====
def calculate_bet(user):
    base_money = user["money"]
    base_percent = 0.05

    if user["lose"] == 0:
        bet = base_money * base_percent
    else:
        bet = base_money * base_percent * (2 ** (user["lose"] - 1))

    if bet > base_money * 0.9:
        bet = int(base_money * 0.9)
    else:
        bet = int(bet)

    return max(1, bet)

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI CASINO PRO (ẨN RANDOM)\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 500000")
        return

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

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        start_money = users[uid]["start_money"]
        users[uid] = {
            "money": start_money,
            "start_money": start_money,
            "profit": 0,
            "win": 0,
            "lose": 0,
            "last_pred": None,
            "last_bet": 0,
            "history": []
        }

    await update.message.reply_text("🔄 Reset xong")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        del users[uid]

    await update.message.reply_text("💣 Xoá toàn bộ")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]

    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập dạng: 3-5-6")
        return

    dice = nums
    total = sum(dice)
    real = classify_total(total)

    msg_wait = await update.message.reply_text("⏳ AI đang phân tích...")
    await asyncio.sleep(1)

    # lưu history
    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    # WIN/LOSE
    result_text = "..."
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += user["last_bet"]
            user["profit"] += user["last_bet"]
            user["win"] += 1
            user["lose"] = 0
            result_text = "✅ WIN"
        else:
            user["money"] -= user["last_bet"]
            user["profit"] -= user["last_bet"]
            user["lose"] += 1
            result_text = "❌ LOSE"

    # AI
    pred, conf = ai_predict(user)

    bet = calculate_bet(user)

    if user["money"] <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = bet

    percent_total = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    msg = (
        "🤖 AI CASINO PRO (ẨN RANDOM)\n"
        "━━━━━━━━━━━━\n"
        f"🎲 {dice} → {real}\n\n"
        f"{result_text}\n"
        "━━━━━━━━━━━━\n"
        f"🔮 Dự đoán: {pred}\n"
        f"📊 Xác suất: {calculate_percent(conf):.1f}%\n"
        f"💰 Cược: {money(bet)}\n"
        "━━━━━━━━━━━━\n"
        f"💼 Vốn: {money(user['money'])}\n"
        f"📈 Lợi nhuận: {money(user['profit'])}\n"
        f"📊 Tổng %: {percent_total:.1f}%\n"
        "━━━━━━━━━━━━\n"
        f"🏆 {user['win']} | ❌ {user['lose']}"
    )

    await msg_wait.edit_text(msg)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🤖 AI CASINO PRO RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
