import os
import logging
import random
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
logging.getLogger("httpx").setLevel(logging.WARNING)

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

# ===== BUILD DATA =====
def build_ai_data(rounds=300):  # 👉 chỉnh 100 / 300 tùy bạn
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})

    prev = random_dice()

    for _ in range(rounds):
        current = random_dice()
        result = classify_total(sum(current))

        mapping[tuple(prev)][result] += 1
        prev = current

    return mapping

print("🔥 Build AI...")
AI_MAPPING = build_ai_data()
print("✅ READY")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🔥 TX TOOL</b>\n\n"
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
        "base_bet": int(m * 0.05),
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "last_bet_choice": None
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        await update.message.reply_text("❗ Chưa set tiền")
        return

    start_money = users[uid]["start_money"]

    users[uid] = {
        "money": start_money,
        "start_money": start_money,
        "base_bet": int(start_money * 0.05),
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "last_bet_choice": None
    }

    await update.message.reply_text("🔄 Đã reset phiên")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid in users:
        del users[uid]

    await update.message.reply_text("💣 Đã xoá toàn bộ dữ liệu")

# ===== AI =====
def ai_predict(dice):
    key = tuple(sorted(dice))

    if key not in AI_MAPPING:
        return "Tài"

    data = AI_MAPPING[key]
    return "Tài" if data["Tài"] >= data["Xỉu"] else "Xỉu"

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

    prev_bet = user["last_bet"]
    result_text = "..."

    # ===== WIN/LOSE =====
    if user["last_bet_choice"] is not None:
        if user["last_bet_choice"] == real:
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

    # ===== AI + ĐẢO =====
    ai_pred = ai_predict(dice)

    # 🔥 luôn đánh ngược
    bet_choice = "Tài" if ai_pred == "Xỉu" else "Xỉu"

    # ===== BET =====
    bet = user["base_bet"] * user["step"]

    if bet > user["money"]:
        bet = user["money"]

    if bet <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = ai_pred
    user["last_bet"] = int(bet)
    user["last_bet_choice"] = bet_choice

    percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    # ===== UI (CHỈ HIỆN BOT) =====
    msg = (
        "<pre>"
        "🔥 TX TOOL\n"
        "━━━━━━━━━━━━━━\n"
        f"🎲 {dice} = {real}\n\n"

        f"📊 {result_text}\n"
        "━━━━━━━━━━━━━━\n"

        f"🎯 Dự đoán: {bet_choice}\n"
        f"💰 Cược: {money(bet)}\n"
        "━━━━━━━━━━━━━━\n"

        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        f"📊 {percent:.1f}%\n"
        "━━━━━━━━━━━━━━\n"

        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "</pre>"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

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
