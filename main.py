import os
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

# mapping dự đoán
mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

users = {}
last_num = {}
stats = defaultdict(lambda: {"win":0,"lose":0})
d_stats = defaultdict(lambda: {"win":0,"total":0})

def kb():
    k=[]
    row=[]
    for i in range(1,19):
        row.append(InlineKeyboardButton(str(i),callback_data=str(i)))
        if len(row)==6:
            k.append(row); row=[]
    k.append([
        InlineKeyboardButton("📊 Stats","stats"),
        InlineKeyboardButton("🔄 Reset vốn","reset")
    ])
    return InlineKeyboardMarkup(k)

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 BOT TX PRO\n\n/setmoney 1000",
        reply_markup=kb()
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    total = int(context.args[0])

    users[uid] = {
        "start": total,
        "current": total,
        "target": int(total*1.3),
        "stop": int(total*0.7),
        "lose_streak": 0
    }

    await update.message.reply_text(
        f"💰 {total}\n🎯 {int(total*1.3)}\n❌ {int(total*0.7)}"
    )

# ===== BUTTON =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "reset":
        if uid in users:
            profit = users[uid]["current"] - users[uid]["start"]
        else:
            profit = 0

        users.pop(uid,None)
        last_num.pop(uid,None)

        await q.edit_message_text(
            f"🔄 Reset\n💰 Lãi giữ: {profit}\n/setmoney lại",
            reply_markup=kb()
        )
        return

    if data == "stats":
        s = stats[uid]
        await q.answer(
            f"🏆 {s['win']} | ❌ {s['lose']}",
            show_alert=True
        )
        return

    num = int(data)

    if uid not in last_num:
        last_num[uid] = num
        await q.edit_message_text(f"✅ Lưu {num}", reply_markup=kb())
        return

    if uid not in users:
        await q.edit_message_text("❗ /setmoney", reply_markup=kb())
        return

    user = users[uid]

    # STOP
    if user["current"] >= user["target"]:
        await q.edit_message_text("🎯 ĐẠT TARGET - DỪNG", reply_markup=kb())
        return

    if user["current"] <= user["stop"]:
        await q.edit_message_text("❌ STOP LOSS - DỪNG", reply_markup=kb())
        return

    prev = last_num[uid]
    d = abs(num - prev)
    pred = mapping[d]

    # ===== WINRATE THẬT =====
    ds = d_stats[d]
    winrate = (ds["win"]/ds["total"]*100) if ds["total"]>0 else 50

    # ===== BET LOGIC =====
    base = int(user["current"]*0.02)

    if winrate >= 65:
        bet = base*2
        level="🔥"
    elif winrate >= 55:
        bet = base
        level="⚠️"
    else:
        bet = 0
        level="❌"

    # ===== KẾT QUẢ THẬT =====
    real = "Tài" if num>=11 else "Xỉu"

    if bet>0:
        if pred == real:
            user["current"] += bet
            user["lose_streak"]=0
            stats[uid]["win"]+=1
            d_stats[d]["win"]+=1
            result="✅"
        else:
            user["current"] -= bet
            user["lose_streak"]+=1
            stats[uid]["lose"]+=1
            result="❌"

        d_stats[d]["total"]+=1
    else:
        result="⏭"

    last_num[uid]=num

    msg = (
        f"📊 {prev}->{num} (d={d})\n"
        f"🎯 {pred}\n\n"
        f"{level} Winrate: {round(winrate,1)}%\n"
        f"{result}\n\n"
        f"💰 {user['current']}\n"
        f"🎯 {user['target']} | ❌ {user['stop']}\n"
        f"🏆 {stats[uid]['win']} | ❌ {stats[uid]['lose']}"
    )

    await q.edit_message_text(msg, reply_markup=kb())

# ===== RUN =====
TOKEN = os.getenv("TOKEN")
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
app.add_handler(CallbackQueryHandler(button))

app.run_polling()
