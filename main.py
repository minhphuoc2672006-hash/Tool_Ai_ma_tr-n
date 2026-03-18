import os
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes
)

# ===== CONFIG =====
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
history = defaultdict(list)

# ===== UI =====
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
        "🤖 BOT TX PRO MAX\n\n💰 /setmoney 1000",
        reply_markup=kb()
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    try:
        total = int(context.args[0])
    except:
        return await update.message.reply_text("❌ /setmoney 1000")

    old_profit = users.get(uid, {}).get("profit", 0)

    users[uid] = {
        "start": total,
        "current": total,
        "target": int(total*1.3),
        "stop": int(total*0.7),
        "lose_streak": 0,
        "profit": old_profit
    }

    await update.message.reply_text(
        f"💰 Vốn: {total}\n🎯 Target: {int(total*1.3)}\n❌ Stop: {int(total*0.7)}\n💵 Lãi giữ: {old_profit}"
    )

# ===== BUTTON =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    # ===== RESET =====
    if data == "reset":
        profit = users.get(uid, {}).get("profit", 0)

        users[uid] = {
            "start": 0,
            "current": 0,
            "target": 0,
            "stop": 0,
            "lose_streak": 0,
            "profit": profit
        }

        last_num.pop(uid, None)
        history[uid] = []

        await q.edit_message_text(
            f"🔄 RESET VỐN\n💰 Lãi giữ: {profit}\n\n👉 /setmoney lại",
            reply_markup=kb()
        )
        return

    # ===== STATS =====
    if data == "stats":
        s = stats[uid]
        total_d = sum(v["total"] for v in d_stats.values())
        win_d = sum(v["win"] for v in d_stats.values())
        wr = (win_d/total_d*100) if total_d>0 else 0

        await q.answer(
            f"🏆 {s['win']} | ❌ {s['lose']}\n📊 WR: {round(wr,1)}%",
            show_alert=True
        )
        return

    # ===== INPUT =====
    num = int(data)

    history[uid].append(num)
    history[uid] = history[uid][-20:]

    if uid not in last_num:
        last_num[uid] = num
        await q.edit_message_text(f"✅ Lưu {num}", reply_markup=kb())
        return

    if uid not in users or users[uid]["current"] == 0:
        await q.edit_message_text("❗ /setmoney trước", reply_markup=kb())
        return

    user = users[uid]

    # ===== STOP =====
    if user["current"] >= user["target"]:
        return await q.edit_message_text("🎯 ĐẠT TARGET - DỪNG", reply_markup=kb())

    if user["current"] <= user["stop"]:
        return await q.edit_message_text("❌ STOP LOSS - DỪNG", reply_markup=kb())

    prev = last_num[uid]
    d = abs(num - prev)
    pred = mapping.get(d, "Xỉu")

    # ===== WINRATE THEO D =====
    ds = d_stats[d]
    winrate_d = (ds["win"]/ds["total"]*100) if ds["total"]>0 else 50

    # ===== THÊM PHÂN TÍCH CHUỖI =====
    recent = history[uid][-10:]
    tai = sum(1 for x in recent if x >= 11)
    xiu = len(recent) - tai

    if tai > xiu:
        trend = "Tài"
    elif xiu > tai:
        trend = "Xỉu"
    else:
        trend = "Cân"

    # ===== KẾT HỢP AI =====
    if pred == trend:
        final_pred = pred
        boost = 10
    else:
        final_pred = pred
        boost = -5

    final_rate = max(40, min(95, winrate_d + boost))

    # ===== BET =====
    base = int(user["current"] * 0.02)

    if final_rate >= 70:
        bet = base * 3
        level = "🔥 SIÊU MẠNH"
    elif final_rate >= 60:
        bet = base * 2
        level = "🔥 MẠNH"
    elif final_rate >= 50:
        bet = base
        level = "⚠️ TB"
    else:
        bet = 0
        level = "❌ BỎ"

    # ===== KẾT QUẢ =====
    real = "Tài" if num >= 11 else "Xỉu"

    if bet > 0:
        if final_pred == real:
            user["current"] += bet
            user["profit"] += bet
            user["lose_streak"] = 0
            stats[uid]["win"] += 1
            d_stats[d]["win"] += 1
            result = "✅ THẮNG"
        else:
            user["current"] -= bet
            user["profit"] -= bet
            user["lose_streak"] += 1
            stats[uid]["lose"] += 1
            result = "❌ THUA"

        d_stats[d]["total"] += 1
    else:
        result = "⏭ BỎ"

    last_num[uid] = num

    msg = (
        f"📊 {prev} ➜ {num} (d={d})\n"
        f"🎯 Dự đoán: {final_pred}\n"
        f"📈 Trend: {trend}\n\n"
        f"{level} | {round(final_rate,1)}%\n"
        f"{result}\n\n"
        f"💰 Vốn: {user['current']}\n"
        f"💵 Lãi: {user['profit']}\n\n"
        f"🏆 {stats[uid]['win']} | ❌ {stats[uid]['lose']}"
    )

    await q.edit_message_text(msg, reply_markup=kb())

# ===== RUN =====
TOKEN = os.getenv("TOKEN")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
app.add_handler(CallbackQueryHandler(button))

print("🔥 BOT PRO MAX ĐANG CHẠY...")
app.run_polling()
