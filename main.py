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

# ===== FORMAT TIỀN =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

# ===== RANDOM DICE =====
def random_dice():
    return sorted([random.randint(1,6) for _ in range(3)])

# ===== BUILD AI 10 TRIỆU =====
def build_ai_data(rounds=10_000_000):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})

    prev = random_dice()

    for i in range(rounds):
        current = random_dice()
        result = classify_total(sum(current))

        key = tuple(prev)
        mapping[key][result] += 1

        prev = current

        # log nhẹ cho biết đang chạy
        if i % 1_000_000 == 0 and i > 0:
            print(f"⏳ Đã build: {i:,} ván")

    return mapping

print("🔥 Đang build AI 10 TRIỆU...")
AI_MAPPING = build_ai_data()
print("✅ AI READY (10M DATA)")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🔥 TX SUPER AI (10M DATA)</b>\n\n"
        "💰 /setmoney 500000\n"
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
        "last_bet": 0
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== AI DỰ ĐOÁN =====
def ai_predict(dice):
    key = tuple(sorted(dice))

    if key not in AI_MAPPING:
        return "Tài", 0

    data = AI_MAPPING[key]
    tai = data["Tài"]
    xiu = data["Xỉu"]
    total = tai + xiu

    if total == 0:
        return "Tài", 0

    pred = "Tài" if tai >= xiu else "Xỉu"
    diff = abs(tai - xiu) / total

    return pred, diff

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    # parse input
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

    # ===== CHECK WIN/LOSE =====
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
    pred, diff = ai_predict(dice)

    # ===== BET =====
    bet = user["base_bet"] * user["step"]

    if bet > user["money"]:
        bet = user["money"]

    if bet <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = int(bet)

    percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    # ===== UI =====
    msg = (
        "<pre>"
        "🔥 TX SUPER AI\n"
        "━━━━━━━━━━━━━━\n"
        f"🎲 {dice} = {real}\n\n"

        f"📊 {result_text}\n"
        "━━━━━━━━━━━━━━\n"

        f"🔮 Dự đoán: {pred}\n"
        f"📊 Độ lệch: {diff*100:.2f}%\n"
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING (10M DATA)")
    app.run_polling()

if __name__ == "__main__":
    main()
