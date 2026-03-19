import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
    kb = [
        [InlineKeyboardButton("♻️ Reset phiên", callback_data="resetgame")],
        [InlineKeyboardButton("💣 Reset ALL", callback_data="resetall")]
    ]
    await update.message.reply_text(
        "🔥 TX PRO MAX 🔥\n\n"
        "💰 /setmoney 500000\n\n"
        "👉 Nhập: 14-12-6 | 14 12 | 14,12\n"
        "💸 +3000 | -5000",
        reply_markup=InlineKeyboardMarkup(kb)
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
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
def do_resetgame(user):
    user["money"] = user["start_money"]
    user["profit"] = 0
    user["history"] = []
    user["step"] = 1
    user["win"] = 0
    user["lose"] = 0

def do_resetall(user):
    do_resetgame(user)

# ===== BUTTON =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    if uid not in users:
        return

    user = users[uid]

    if query.data == "resetgame":
        do_resetgame(user)
        await query.edit_message_text("♻️ Đã reset phiên!")
    elif query.data == "resetall":
        do_resetall(user)
        await query.edit_message_text("💣 RESET ALL thành công!")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Dùng /setmoney trước")
        return

    user = users[uid]

    # ===== + / - tiền tay =====
    if text.startswith("+") or text.startswith("-"):
        try:
            val = int(text)
            user["money"] += val
            user["profit"] += val
            await update.message.reply_text(
                f"💸 {money(val)}\n💰 {money(user['money'])}"
            )
        except:
            pass
        return

    # ===== parse input =====
    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = []
    for x in text.split():
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
            msgs.append(f"📥 {num} → chờ...")
            continue

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
                result = f"✅ +{money(bet)}"
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["step"] *= 2
                user["lose"] += 1
                result = f"❌ -{money(bet)}"

        percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

        # auto reset
        if percent >= 30 or percent <= -30:
            do_resetgame(user)
            await update.message.reply_text("🔄 Đạt ±30% → Reset phiên!")
            return

        msg = (
            "🎯 TX PRO MAX\n"
            "━━━━━━━━━━━━━━\n"
            f"🔮 {pred} | 🎲 {num}\n"
            f"💵 Cược: {money(bet)}\n"
            f"📊 {result}\n"
            "━━━━━━━━━━━━━━\n"
            f"💰 Vốn: {money(user['money'])}\n"
            f"📈 Lãi: {money(user['profit'])} ({percent:.1f}%)\n"
            f"🏆 {user['win']} | ❌ {user['lose']}\n"
        )

        msgs.append(msg)

    await update.message.reply_text("\n\n".join(msgs))

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
