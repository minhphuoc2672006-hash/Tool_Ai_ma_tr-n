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

# ===== HÀM HỖ TRỢ =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

def classify(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx(history):
    return ["Tài" if sum(x)>=11 else "Xỉu" for x in history]

# ===== AI TÀI/XỈU THÔNG MINH =====
def ai_predict_smart(history):
    tx = get_tx(history)
    if not tx or len(tx) < 3:
        # Chưa đủ dữ liệu → chọn ngẫu nhiên
        return "Tài", 50.0
    
    # Thống kê tần suất Tài/Xỉu
    c = Counter(tx)
    total = len(tx)
    freq_tai = c["Tài"]/total
    freq_xiu = c["Xỉu"]/total

    # Dự đoán theo xu hướng gần đây
    recent = tx[-5:]
    c_recent = Counter(recent)
    trend_pred = "Tài" if c_recent["Tài"] >= c_recent["Xỉu"] else "Xỉu"

    # Dự đoán theo chiến thuật chuỗi
    if len(tx) >= 2 and tx[-1]==tx[-2]:
        chain_pred = tx[-1]  # theo chuỗi
    else:
        chain_pred = "Xỉu" if tx[-1]=="Tài" else "Tài"

    # Kết hợp tất cả, tính tỷ lệ tin cậy
    pred_scores = {"Tài": freq_tai, "Xỉu": freq_xiu}
    pred_scores[trend_pred] += 0.1
    pred_scores[chain_pred] += 0.1

    # Chọn dự đoán cao nhất
    pred = max(pred_scores, key=lambda k: pred_scores[k])
    conf = min(pred_scores[pred]*100, 99.9)  # đảm bảo <=99.9%
    return pred, conf

# ===== GẤP THÉP =====
def bet_calc(user):
    base = max(int(user["start"] * 0.05),1)
    if user["lose"] == 0:
        return base
    fib_seq = [1,1,2,3,5,8,13,21,34,55]
    if user["lose"] < len(fib_seq):
        bet = base * fib_seq[user["lose"]]
    else:
        bet = base * (2**user["lose"])
    return min(int(bet), user["money"])

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI TÀI/XỈU THÔNG MINH ẨN CHIẾN THUẬT\n"
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
    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập 3 số ví dụ: 3-5-6")
        return

    dice = nums
    real = classify(sum(dice))
    await update.message.reply_text(f"🎲 {dice} → {real}")
    await asyncio.sleep(0.5)
    msg_wait = await update.message.reply_text("⏳ Đang phân tích toàn bộ lịch sử...")
    await asyncio.sleep(1)

    # ===== CẬP NHẬT LỊCH SỬ =====
    user["history"].append(dice)
    if len(user["history"]) > 100:
        user["history"].pop(0)

    # ===== DỰ ĐOÁN THÔNG MINH =====
    pred, conf = ai_predict_smart(user["history"])

    # ===== TÍNH TIỀN CƯỢC =====
    next_bet = bet_calc(user)

    # ===== CẬP NHẬT THẮNG/THUA =====
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
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

    # ===== HIỂN THỊ KẾT QUẢ =====
    msg = (
        "━━━━━━━━━━━━━━\n"
        "🤖 AI PHÂN TÍCH TÀI/XỈU THÔNG MINH\n"
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
