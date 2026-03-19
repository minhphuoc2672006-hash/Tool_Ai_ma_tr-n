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
        "<b>TX TOOL</b>\n\n"
        "/setmoney 500000\n"
        "/reset\n\n"
        "Input: 14-12-6 | 14 12 | 14,12\n"
        "+3000 | -5000",
        parse_mode=ParseMode.HTML
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("Ví dụ: /setmoney 500000")
        return

    users[uid] = {
        "money": m,
        "start_money": m,
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
    }

    await update.message.reply_text(f"Vốn: {money(m)}")

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

    await update.message.reply_text("RESET DONE")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("Dùng /setmoney trước")
        return

    user = users[uid]

    # ===== + / - tiền =====
    if text.startswith("+") or text.startswith("-"):
        try:
            val = int(text)
            user["money"] += val
            user["profit"] += val
            await update.message.reply_text(f"Vốn: {money(user['money'])}")
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

        if len(user["history"]) < 2:
            await update.message.reply_text(f"{num} ...")
            return

        prev = user["history"][-2]
        d = abs(num - prev)
        pred = mapping[d]

        base = int(user["money"] * 0.05)
        bet = min(base * user["step"], user["money"])

        result = "..."

        if len(user["history"]) >= 3:
            prev_d = abs(user["history"][-2] - user["history"][-3])
            prev_pred = mapping[prev_d]

            if prev_pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["step"] = 1
                user["win"] += 1
                result = "WIN"
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["step"] *= 2
                user["lose"] += 1
                result = "LOSE"

        percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

        if percent >= 30 or percent <= -30:
            user["money"] = user["start_money"]
            user["profit"] = 0
            user["history"] = []
            user["step"] = 1
            user["win"] = 0
            user["lose"] = 0

            await update.message.reply_text("AUTO RESET ±30%")
            return

        # ===== UI VIP =====
        msg = (
            "<pre>"
            "TX TOOL\n"
            "──────────────\n"
            f"Roll : {num}\n"
            f"Pick : {pred}\n"
            f"Bet  : {money(bet)}\n"
            f"Res  : {result}\n"
            "──────────────\n"
            f"Bank : {money(user['money'])}\n"
            f"P/L  : {money(user['profit'])}\n"
            f"Rate : {percent:.1f}%\n"
            f"W/L  : {user['win']} / {user['lose']}\n"
            "</pre>"
        )

        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
