import os
import logging
import random
import asyncio
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

# ===== HISTORY → TÀI/XỈU =====
def get_tx_history(history):
    return [classify_total(sum(x)) for x in history]

# ===== AI HỌC CHUỖI =====
def build_sequence_ai(history):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})
    tx = get_tx_history(history)
    for i in range(len(tx) - 3):
        seq = tuple(tx[i:i+3])
        next_val = tx[i+3]
        mapping[seq][next_val] += 1
    return mapping

# ===== AI DỰ ĐOÁN (80% RANDOM - 20% REAL + ĐẢO LIÊN TỤC) =====
def ai_predict(history, mapping):
    tx = get_tx_history(history)
    # REAL 20%
    if len(tx) >= 3:
        key = tuple(tx[-3:])
        data = mapping[key]
        tai = data["Tài"]
        xiu = data["Xỉu"]
        if tai + xiu == 0:
            real_pred = random.choice(["Tài", "Xỉu"])
        else:
            real_pred = "Tài" if tai > xiu else "Xỉu"
    else:
        tai = xiu = 1
        real_pred = random.choice(["Tài", "Xỉu"])

    # RANDOM 80%
    if random.random() < 0.8:
        pred = random.choice(["Tài", "Xỉu"])
    else:
        pred = real_pred

    # ĐẢO LIÊN TỤC
    if random.random() < 0.6:
        pred = "Xỉu" if pred == "Tài" else "Tài"

    return pred, tai, xiu

# ===== % DỰ ĐOÁN =====
def calculate_percent(tai, xiu):
    total = tai + xiu if (tai + xiu) != 0 else 1
    percent = (max(tai, xiu) / total) * 10 + random.uniform(50, 90)
    return max(50, min(95, percent))

# ===== TÍNH CƯỢC GẤP THÉP KHÔNG GIỚI HẠN =====
def calculate_bet(user):
    base_money = user["money"]
    base_percent = 0.05  # 5% vốn ban đầu

    if user["lose"] == 0:
        bet = base_money * base_percent
    else:
        bet = base_money * base_percent * (2 ** (user["lose"] - 1))

    # Giới hạn cược tối đa 90% vốn
    if bet > base_money * 0.9:
        bet = int(base_money * 0.9)
    else:
        bet = int(bet)

    if bet < 1:
        bet = 1

    return bet

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 TX AI RANDOM 80% + GẤP THÉP KHÔNG GIỚI HẠN\n\n"
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

    # lưu lịch sử
    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    # build AI
    AI_MAPPING = build_sequence_ai(user["history"])

    # tính win/lose
    result_text = "..."
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += user["last_bet"]
            user["profit"] += user["last_bet"]
            user["win"] += 1
            user["lose"] = 0  # reset gấp thép khi thắng
            result_text = "✅ WIN"
        else:
            user["money"] -= user["last_bet"]
            user["profit"] -= user["last_bet"]
            user["lose"] += 1
            result_text = "❌ LOSE"

    # AI predict
    pred, tai, xiu = ai_predict(user["history"], AI_MAPPING)

    # tính cược gấp thép không giới hạn
    bet = calculate_bet(user)

    if bet <= 0 or user["money"] <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = bet
    percent_total = ((user["money"] - user["start_money"]) / user["start_money"] * 100)
    percent = calculate_percent(tai, xiu)

    msg = (
        "🔥 TX AI RANDOM 80% + GẤP THÉP KHÔNG GIỚI HẠN\n"
        "━━━━━━━━━━━━\n"
        f"🎲 {dice} → {real}\n\n"
        f"{result_text}\n"
        "━━━━━━━━━━━━\n"
        f"🔮 Dự đoán: {pred}\n"
        f"📊 Xác suất: {percent:.1f}%\n"
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
    print("🔥 BOT RANDOM 80% + GẤP THÉP KHÔNG GIỚI HẠN RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
