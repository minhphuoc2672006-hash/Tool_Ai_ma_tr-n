import os
import logging
from telegram import Update
from telegram.constants import ParseMode
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
        "<b>🎯 TX TOOL PRO</b>\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n\n"
        "📥 Nhập: 14-12-6 | 14 12 | 14,12\n"
        "💸 +3000 | -5000",
        parse_mode=ParseMode.HTML
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("❗ Ví dụ: /setmoney 500000")
        return

    users[uid] = {
        "money": m,
        "start_money": m,
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "stopped": False
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user["stopped"] = False

    await update.message.reply_text("💣 ĐÃ RESET - TIẾP TỤC CHƠI")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Dùng /setmoney trước")
        return

    user = users[uid]

    if user["stopped"]:
        await update.message.reply_text("🛑 ĐÃ DỪNG → /reset để chơi lại")
        return

    # ===== + / - tiền =====
    if text.startswith("+") or text.startswith("-"):
        try:
            val = int(text)
            user["money"] += val
            user["profit"] += val
            await update.message.reply_text(f"💰 Vốn: {money(user['money'])}")
        except:
            pass
        return

    # ===== parse =====
    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 18]
    if not nums:
        return

    for num in nums:
        user["history"].append(num)
        real = "Tài" if num >= 11 else "Xỉu"

        # ===== DỰ ĐOÁN =====
        pred = "..."
        if len(user["history"]) >= 2:
            prev = user["history"][-2]
            d = abs(num - prev)
            pred = mapping.get(d, "Tài")

        # ===== CƯỢC CHUẨN =====
        base_bet = int(user["start_money"] * 0.05)
        bet = min(base_bet * user["step"], user["money"])

        result = "..."
        round_profit = 0

        # ===== SO KẾT QUẢ =====
        if pred != "...":
            if pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["step"] = 1
                user["win"] += 1
                user["lose"] = 0
                result = "✅ WIN"
                round_profit = bet
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["lose"] += 1

                # né chuỗi thua
                if user["lose"] >= 2:
                    user["step"] = 1
                else:
                    user["step"] *= 2

                result = "❌ LOSE"
                round_profit = -bet

        percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

        # ===== DỪNG ±30% =====
        if percent >= 30 or percent <= -30:
            user["stopped"] = True

        # ===== UI =====
        msg = (
            "<pre>"
            "🎯 TX TOOL PRO\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎲 Kết quả   : {num}\n"
            f"🔮 Dự đoán  : {pred}\n"
            f"💵 Cược     : {money(bet)}\n"
            f"📊 Trạng thái: {result}\n"
            f"💸 Lãi ván  : {money(round_profit)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Vốn      : {money(user['money'])}\n"
            f"📈 Tổng Lãi : {money(user['profit'])}\n"
            f"📊 %        : {percent:.1f}%\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 Thắng    : {user['win']}\n"
            f"❌ Thua     : {user['lose']}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        if user["stopped"]:
            msg += "🛑 ĐÃ DỪNG (±30%)\n"

        msg += "</pre>"

        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
