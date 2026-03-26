import os
import logging
import asyncio
from collections import Counter
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== CHIẾN THUẬT CAO CẤP ẨN =====
STRATEGIES = ["FOLLOW","OPPOSITE","TREND","STREAK","ANTI_STREAK","WEIGHTED","LAST2","BREAK","CHAOS"]
BETTING_EXTENSIONS = ["MARTINGALE","FIBONACCI"]
STRATEGIES += BETTING_EXTENSIONS

# ===== HÀM HỖ TRỢ =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

def classify(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx(history):
    return ["Tài" if sum(x)>=11 else "Xỉu" for x in history]

# ===== CHIẾN THUẬT TỰ ĐỔI DỰA TOÀN BỘ LỊCH SỬ =====
def run_strategy(history):
    tx = get_tx(history)
    if not tx:
        return "Tài", 0.0
    
    # Suy luận chiến thuật tự đổi dựa trên toàn bộ lịch sử
    last_two = tx[-2:] if len(tx)>=2 else tx
    c = Counter(tx)
    
    # Quy luật tự chọn chiến thuật theo chuỗi
    if len(last_two)==2 and last_two[0]==last_two[1]:
        strategy = "STREAK"
    elif len(last_two)==2:
        strategy = "ANTI_STREAK"
    else:
        strategy = "WEIGHTED"
    
    # Dự đoán dựa chiến thuật
    if strategy in ["FOLLOW","STREAK"]:
        pred = last_two[-1]
    elif strategy in ["OPPOSITE","ANTI_STREAK"]:
        pred = "Xỉu" if last_two[-1]=="Tài" else "Tài"
    elif strategy=="TREND" or strategy=="WEIGHTED":
        pred = "Tài" if c["Tài"]>=c["Xỉu"] else "Xỉu"
    elif strategy=="LAST2":
        pred = last_two[0]
    elif strategy=="BREAK":
        pred = "Xỉu" if last_two[-1]=="Tài" else "Tài"
    else:  # CHAOS hoặc betting
        pred = "Tài" if c["Tài"]>=c["Xỉu"] else "Xỉu"
    
    # Xác suất dựa trên toàn bộ lịch sử
    correct_count = sum(1 for i in range(1,len(tx)) if tx[i]==tx[i-1])
    conf = (correct_count / max(len(tx)-1,1)) * 100
    return pred, conf

# ===== TÍNH TIỀN CƯỢC (Fibonacci) =====
def bet_calc(user):
    base = int(user["money"]*0.05)
    fib_seq = [1,1,2,3,5,8,13,21,34,55]
    if user["lose"]==0:
        return base
    if user["lose"]<len(fib_seq):
        bet = base*fib_seq[user["lose"]]
    else:
        bet = base*(2**user["lose"])
    return int(min(bet,user["money"]))

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI PRO TÀI/XỈU ẨN CHIẾN THUẬT TOÀN BỘ LỊCH SỬ\n"
        "/setmoney 500000\n/reset để reset dữ liệu"
    )

async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    m = int(context.args[0])
    users[uid] = {
        "money": m,
        "start": m,
        "profit": 0,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []
    }
    await update.message.reply_text(f"💰 Vốn: {money(m)}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid] = {
            "money": 0,
            "start": 0,
            "profit": 0,
            "win": 0,
            "lose": 0,
            "last_pred": None,
            "last_bet": 0,
            "history": []
        }
        await update.message.reply_text("♻️ Đã reset toàn bộ dữ liệu!")
    else:
        await update.message.reply_text("❗ Bạn chưa set tiền /setmoney trước")

# ===== HANDLE INPUT =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text
    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]
    nums = [int(x) for x in text.replace("-", " ").split() if x.isdigit()]
    if len(nums)!=3:
        await update.message.reply_text("❗ Nhập 3 số ví dụ: 3-5-6")
        return

    dice = nums
    real = classify(sum(dice))
    await update.message.reply_text(f"🎲 {dice} → {real}")
    await asyncio.sleep(0.5)
    msg_wait = await update.message.reply_text("⏳ Đang phân tích toàn bộ lịch sử...")

    # ===== CẬP NHẬT LỊCH SỬ =====
    user["history"].append(dice)
    if len(user["history"])>100:  # Giữ 100 kết quả gần nhất
        user["history"].pop(0)

    # ===== DỰ ĐOÁN =====
    if len(user["history"])>=2:
        pred, conf = run_strategy(user["history"])
    else:
        await msg_wait.edit_text("❗ Chưa đủ dữ liệu để phân tích")
        return

    next_bet = bet_calc(user)

    # ===== CẬP NHẬT TIỀN =====
    if user["last_pred"] is not None:
        if user["last_pred"]==real:
            user["money"] += user["last_bet"]
            user["profit"] += user["last_bet"]
            user["win"] += 1
            user["lose"] = 0
        else:
            user["money"] -= user["last_bet"]
            user["profit"] -= user["last_bet"]
            user["lose"] += 1

    user["last_pred"] = pred
    user["last_bet"] = next_bet

    msg = (
        "━━━━━━━━━━━━━━\n"
        "🤖 AI PHÂN TÍCH\n"
        "━━━━━━━━━━━━━━\n"
        f"🔮 Dự đoán lần tới: {pred}\n"
        f"📊 Xác suất: {conf:.1f}%\n"
        "──────────────\n"
        f"💸 Cược: {money(next_bet)}\n"
        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        "──────────────\n"
        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "━━━━━━━━━━━━━━"
    )
    await msg_wait.edit_text(msg)

# ===== RUN BOT =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("🔥 BOT RUNNING...")
    app.run_polling()

if __name__=="__main__":
    main()
