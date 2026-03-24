import os
import logging
import random
import asyncio
from collections import defaultdict
from telegram import Update
from telegram.constants import ParseMode
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

# ===== RANDOM =====
def random_dice():
    return sorted([random.randint(1,6) for _ in range(3)])

# ===== BUILD RANDOM (fallback) =====
def build_ai_data(rounds=100):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})
    prev = random_dice()

    for _ in range(rounds):
        curr = random_dice()
        result = classify_total(sum(curr))
        mapping[tuple(prev)][result] += 1
        prev = curr

    return mapping

# ===== BUILD SMART AI =====
def build_smart_ai(history, rounds=100):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})

    # 🔥 DATA THẬT (trọng số cao)
    for i in range(len(history) - 1):
        prev = tuple(sorted(history[i]))
        curr = history[i + 1]
        result = classify_total(sum(curr))
        mapping[prev][result] += 3

    # 🔥 RANDOM (tránh bị bí)
    prev = random_dice()
    for _ in range(rounds):
        curr = random_dice()
        result = classify_total(sum(curr))
        mapping[tuple(prev)][result] += 1
        prev = curr

    return mapping

# ===== AI PREDICT =====
def ai_predict(dice, mapping):
    key = tuple(sorted(dice))

    if key not in mapping:
        return "Tài", 50

    data = mapping[key]
    tai = data["Tài"]
    xiu = data["Xỉu"]
    total = tai + xiu

    if total == 0:
        return "Tài", 50

    pred = "Tài" if tai >= xiu else "Xỉu"

    diff = abs(tai - xiu) / total
    percent = (1 - diff) * 100  # 🔥 đảo %

    return pred, percent

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🔥 TX AI SMART</b>\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6\n",
        parse_mode=ParseMode.HTML
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
        "base_percent": 0.05,
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []   # 🔥 TRÍ NHỚ
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        return

    start_money = users[uid]["start_money"]

    users[uid].update({
        "money": start_money,
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []
    })

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

    # chuẩn hoá input
    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]

    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập dạng: 3-5-6")
        return

    dice = nums
    total = sum(dice)
    real = classify_total(total)

    # ===== HIỆU ỨNG =====
    msg_wait = await update.message.reply_text("⏳ Đang phân tích...")
    await asyncio.sleep(3)

    # ===== LƯU LỊCH SỬ =====
    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    # ===== BUILD AI =====
    if len(user["history"]) < 5:
        AI_MAPPING = build_ai_data(100)
    else:
        AI_MAPPING = build_smart_ai(user["history"], 100)

    # ===== WIN / LOSE =====
    prev_bet = user["last_bet"]
    result_text = "..."

    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += prev_bet
            user["profit"] += prev_bet
            user["win"] += 1
            user["step"] = 1
            result_text = "✅ WIN"
        else:
            user["money"] -= prev_bet
            user["profit"] -= prev_bet
            user["lose"] += 1
            user["step"] *= 2
            result_text = "❌ LOSE"

    # ===== AI =====
    pred, percent = ai_predict(dice, AI_MAPPING)

    # ===== BET =====
    base_bet = user["money"] * user["base_percent"]
    bet = int(base_bet * user["step"])

    if bet > user["money"]:
        bet = user["money"]

    if bet <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = bet

    percent_total = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    # ===== UI =====
    msg = (
        "<pre>"
        "🔥 TX AI SMART\n"
        "━━━━━━━━━━━━\n"
        f"🎲 {dice} → {real}\n\n"

        f"{result_text}\n"
        "━━━━━━━━━━━━\n"

        f"🔮 {pred}\n"
        f"📊 {percent:.1f}%\n"
        f"💰 {money(bet)}\n"
        "━━━━━━━━━━━━\n"

        f"💼 {money(user['money'])}\n"
        f"📈 {money(user['profit'])}\n"
        f"📊 {percent_total:.1f}%\n"
        "━━━━━━━━━━━━\n"

        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "</pre>"
    )

    await msg_wait.edit_text(msg, parse_mode=ParseMode.HTML)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
