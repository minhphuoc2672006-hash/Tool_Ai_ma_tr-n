import os
import random
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

users = {}

def money(x):
    return f"{x:,}".replace(",", ".")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 TX PRO MAX 🔥\n\n"
        "👉 /setmoney 500000\n\n"
        "♻️ /resetgame → reset bảng chơi\n"
        "📊 /resettotal → reset bảng tổng\n"
        "💣 /resetall → reset tất cả\n\n"
        "+30% / -30% → reset bảng chơi\n\n"
        "👉 Nhập: 14-12-6"
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

        # bảng chơi
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,

        # bảng tổng
        "total_profit": 0,
        "total_loss": 0,
        "total_win": 0,
        "total_lose": 0,
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET GAME =====
async def resetgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        return

    user = users[uid]

    user["money"] = user["start_money"]
    user["profit"] = 0
    user["history"] = []
    user["step"] = 1
    user["win"] = 0
    user["lose"] = 0

    await update.message.reply_text("🔄 Đã reset bảng chơi!")

# ===== RESET TOTAL =====
async def resettotal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        return

    user = users[uid]

    user["total_profit"] = 0
    user["total_loss"] = 0
    user["total_win"] = 0
    user["total_lose"] = 0

    await update.message.reply_text("📊 Đã reset bảng tổng!")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        return

    user = users[uid]

    # reset bảng chơi
    user["money"] = user["start_money"]
    user["profit"] = 0
    user["history"] = []
    user["step"] = 1
    user["win"] = 0
    user["lose"] = 0

    # reset bảng tổng
    user["total_profit"] = 0
    user["total_loss"] = 0
    user["total_win"] = 0
    user["total_lose"] = 0

    await update.message.reply_text("💣 RESET ALL thành công!")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Dùng /setmoney trước")
        return

    user = users[uid]

    nums = []
    for x in text.replace("-", " ").split():
        if x.isdigit():
            n = int(x)
            if 1 <= n <= 18:
                nums.append(n)

    if not nums:
        return

    msgs = []

    for num in nums:
        user["history"].append(num)
        real = "Tài" if num >= 11 else "Xỉu"

        if len(user["history"]) < 2:
            msgs.append(f"📥 Nhận: {num} → chờ...")
            continue

        prev = user["history"][-2]
        d = abs(num - prev)
        pred = mapping[d]

        base = int(user["money"] * 0.05)
        bet = base * user["step"]

        result = ""

        if len(user["history"]) >= 3:
            prev_d = abs(user["history"][-2] - user["history"][-3])
            prev_pred = mapping[prev_d]

            if prev_pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["step"] = 1
                user["win"] += 1

                user["total_profit"] += bet
                user["total_win"] += 1

                result = f"✅ WIN +{money(bet)}"
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["step"] *= 2
                user["lose"] += 1

                user["total_loss"] += bet
                user["total_lose"] += 1

                result = f"❌ LOSE -{money(bet)}"

        # check %
        start = user["start_money"]
        current = user["money"]
        percent = ((current - start) / start * 100)

        if percent >= 30 or percent <= -30:
            # reset bảng chơi
            user["money"] = user["start_money"]
            user["profit"] = 0
            user["history"] = []
            user["step"] = 1
            user["win"] = 0
            user["lose"] = 0

            await update.message.reply_text("🔄 Đạt 30% → reset bảng chơi")
            return

        msg = (
            "🎯 TX PRO MAX\n\n"
            f"Dự đoán: {pred}\n"
            f"{result}\n\n"

            f"💰 Vốn: {money(user['money'])}\n"
            f"📈 Lãi: {money(user['profit'])}\n"
            f"📊 {percent:.1f}%\n\n"

            f"🏆 {user['win']} | ❌ {user['lose']}\n\n"

            "📊 BẢNG TỔNG\n"
            f"💰 Lời: {money(user['total_profit'])}\n"
            f"💸 Lỗ: {money(user['total_loss'])}\n"
            f"🏆 {user['total_win']} | ❌ {user['total_lose']}\n"
        )

        msgs.append(msg)

    await update.message.reply_text("\n".join(msgs))

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("resetgame", resetgame))
    app.add_handler(CommandHandler("resettotal", resettotal))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
