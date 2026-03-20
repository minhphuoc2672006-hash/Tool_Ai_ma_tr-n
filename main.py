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
    return f"{int(x):,}".replace(",", ".")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🎯 TX TOOL PRO</b>\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset (reset phiên)\n"
        "💣 /resetall (xoá toàn bộ)\n\n"
        "📥 Nhập: 14-12-6 | 14 12 | 14,12\n",
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
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "stopped": False
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
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "stopped": False
    }

    await update.message.reply_text("🔄 RESET SẠCH PHIÊN")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid in users:
        del users[uid]

    await update.message.reply_text("💣 ĐÃ XOÁ TOÀN BỘ DỮ LIỆU")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    if user["stopped"]:
        await update.message.reply_text("🛑 ĐÃ DỪNG → /reset")
        return

    # ===== PARSE =====
    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 18]
    if not nums:
        return

    for num in nums:
        user["history"].append(num)
        real = "Tài" if num >= 11 else "Xỉu"

        result = "..."
        round_profit = 0
        prev_bet = user["last_bet"]

        # ===== XỬ LÝ KẾT QUẢ VÁN TRƯỚC =====
        if len(user["history"]) >= 2 and user["last_pred"] is not None:
            if user["last_pred"] == real:
                user["money"] += prev_bet
                user["profit"] += prev_bet
                user["win"] += 1
                user["step"] = 1
                result = "✅ WIN"
                round_profit = prev_bet
            else:
                user["money"] -= prev_bet
                user["profit"] -= prev_bet
                user["lose"] += 1
                result = "❌ LOSE"
                round_profit = -prev_bet
                user["step"] *= 2

        # ===== DỰ ĐOÁN =====
        if len(user["history"]) >= 2:
            prev = user["history"][-2]
            d = abs(num - prev)
            pred = mapping.get(d, "Tài")
        else:
            pred = "..."

        # ===== CƯỢC GẤP THẾP + CHẶN -20% =====
        base_bet = user["base_bet"]
        bet = base_bet * user["step"]

        if bet > user["money"]:
            bet = user["money"]

        # 🔥 CHẶN KHÔNG CHO LỖ QUÁ -20%
        min_money = user["start_money"] * 0.8

        if user["money"] - bet < min_money:
            bet = user["money"] - min_money

        if bet <= 0:
            user["stopped"] = True
            await update.message.reply_text("🛑 CHẠM -20% → DỪNG")
            return

        user["last_pred"] = pred
        user["last_bet"] = int(bet)

        percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

        # ===== STOP =====
        stop_msg = ""
        if percent >= 30:
            user["stopped"] = True
            stop_msg = "🟢🔥 +30% → CHỐT LỜI"
        elif percent <= -20:
            user["stopped"] = True
            stop_msg = "🔴⚠️ -20% → CẮT LỖ"

        # ===== UI =====
        msg = (
            "<pre>"
            "🎯 TX TOOL PRO\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎲 Kết quả: {num}\n\n"

            f"💵 Cược trước: {money(prev_bet)}\n"
            f"📊 KQ: {result}\n"
            f"💸 Lãi: {money(round_profit)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"

            f"🔮 Dự đoán tiếp: {pred}\n"
            f"💰 Cược tiếp: {money(bet)}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"

            f"💰 Vốn: {money(user['money'])}\n"
            f"📈 Tổng lãi: {money(user['profit'])}\n"
            f"📊 %: {percent:.1f}%\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"

            f"🏆 Thắng: {user['win']}\n"
            f"❌ Thua: {user['lose']}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        if stop_msg:
            msg += stop_msg + "\n"

        msg += "</pre>"

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
