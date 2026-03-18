import os
import asyncio
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

# ===== MAP =====
mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

# ===== DATA =====
users = {}
last_num = {}
stats = defaultdict(lambda: {"win":0,"lose":0})
d_stats = defaultdict(lambda: {"win":0,"total":0})

# ===== UI SAFE =====
def kb():
    keyboard = []
    row = []

    for i in range(1, 19):
        btn = InlineKeyboardButton(text=str(i), callback_data=f"num_{i}")
        row.append(btn)

        if len(row) == 6:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("📊 Stats", callback_data="stats"),
        InlineKeyboardButton("🔄 Reset", callback_data="reset")
    ])

    return InlineKeyboardMarkup(keyboard)

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("👉 /start OK")

    await update.message.reply_text(
        "🤖 BOT TX PRO MAX\n\n💰 /setmoney 1000",
        reply_markup=kb()
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        total = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 1000")
        return

    users[uid] = {
        "start": total,
        "current": total,
        "target": int(total*1.3),
        "stop": int(total*0.7),
        "profit": 0,
        "lose_streak": 0
    }

    await update.message.reply_text(
        f"💰 Vốn: {total}\n🎯 {int(total*1.3)}\n❌ {int(total*0.7)}"
    )

# ===== BUTTON =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    data = q.data

    # ===== RESET =====
    if data == "reset":
        users.pop(uid, None)
        last_num.pop(uid, None)

        await q.edit_message_text("🔄 Reset xong", reply_markup=kb())
        return

    # ===== STATS =====
    if data == "stats":
        s = stats[uid]
        total = sum(v["total"] for v in d_stats.values())
        win = sum(v["win"] for v in d_stats.values())

        wr = (win/total*100) if total else 0

        await q.answer(f"🏆 {s['win']} | ❌ {s['lose']}\nWR: {round(wr,1)}%", show_alert=True)
        return

    # ===== NUM =====
    if not data.startswith("num_"):
        return

    num = int(data.split("_")[1])

    if uid not in last_num:
        last_num[uid] = num
        await q.edit_message_text(f"✅ Lưu {num}", reply_markup=kb())
        return

    if uid not in users:
        await q.edit_message_text("❗ /setmoney trước", reply_markup=kb())
        return

    user = users[uid]

    prev = last_num[uid]
    d = abs(num - prev)

    pred = mapping.get(d, "Tài")

    ds = d_stats[d]
    wr = (ds["win"]/ds["total"]*100) if ds["total"] else 50

    bet = int(user["current"] * 0.02)

    real = "Tài" if num >= 11 else "Xỉu"

    if pred == real:
        user["current"] += bet
        user["profit"] += bet
        stats[uid]["win"] += 1
        ds["win"] += 1
        result = "✅ THẮNG"
    else:
        user["current"] -= bet
        user["profit"] -= bet
        stats[uid]["lose"] += 1
        result = "❌ THUA"

    ds["total"] += 1
    last_num[uid] = num

    msg = (
        f"{prev} ➜ {num} (d={d})\n"
        f"🎯 {pred} | WR {round(wr,1)}%\n"
        f"{result}\n\n"
        f"💰 {user['current']} | 💵 {user['profit']}"
    )

    await q.edit_message_text(msg, reply_markup=kb())

# ===== RUN =====
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
app.add_handler(CallbackQueryHandler(button))

async def main():
    print("🔥 BOT ĐANG CHẠY...")

    await app.bot.delete_webhook(drop_pending_updates=True)

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
