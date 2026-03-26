import os
import logging
import asyncio
from collections import defaultdict, Counter
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
import random

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx_history(history):
    return [classify_total(sum(x)) for x in history]

# ===== AI NHẬN DIỆN TOÀN BỘ CẦU LOGIC =====
def analyze_dice_patterns(history):
    if not history:
        return None
    patterns = defaultdict(int)
    tx = get_tx_history(history)

    for i in range(len(tx)):
        # Bệt
        if i >= 2 and history[i][0]==history[i][1]==history[i][2]:
            patterns["bệt"] +=1
        # Dài
        if i >=3 and len(set(tx[i-3:i+1]))==1:
            patterns["dài"] +=1
        # Nối
        if i >=3 and tx[i-3:i-1]==tx[i-1:i+1]:
            patterns["nối"] +=1
        # Chu kỳ
        if i >=3 and tx[i-3]==tx[i-1] and tx[i-2]==tx[i]:
            patterns["chu kỳ"] +=1
        # Zigzag
        if i >=1 and tx[i]!=tx[i-1]:
            patterns["zigzag"] +=1
        # Cầu 31123
        if i>=4 and tx[i-4:i+1]==[tx[i-4]]*3 + [tx[i-1]] + [tx[i]]:
            patterns["31123"] +=1
        # Cầu phức tạp
        if i>=5 and tx[i-5:i+1]==tx[i-5:i+1][::-1]:
            patterns["phức tạp"] +=1
    return patterns

# ===== AI DỰ ĐOÁN =====
def ai_predict(user):
    history = user["history"]
    tx = get_tx_history(history)
    patterns = analyze_dice_patterns(history)
    votes = []

    # Vote theo cầu
    if patterns:
        for k,v in patterns.items():
            if v>0:
                if k in ["bệt","31123"]: votes.append("Xỉu")
                elif k in ["dài","chu kỳ"]: votes.append(tx[-1])
                elif k=="nối": votes.append("Tài" if tx[-1]=="Xỉu" else "Xỉu")
                elif k=="zigzag": votes.append("Tài" if tx[-1]=="Tài" else "Xỉu")
                elif k=="phức tạp": votes.append(tx[-1])

    # Markov dài 3->5
    for l in range(3,6):
        if len(tx)>=l:
            key = tuple(tx[-l:])
            counts = Counter()
            for i in range(len(tx)-l):
                if tuple(tx[i:i+l])==key:
                    counts[tx[i+l]] +=1
            if counts:
                markov_pred = "Tài" if counts["Tài"]>counts["Xỉu"] else "Xỉu"
                votes.append(markov_pred)

    # Trend tổng thể
    total_tai = tx.count("Tài")
    total_xiu = tx.count("Xỉu")
    votes.append("Tài" if total_tai>total_xiu else "Xỉu")

    # Mega Strategy
    if len(tx)>=6:
        last6 = tx[-6:]
        tai = last6.count("Tài")
        xiu = last6.count("Xỉu")
        votes.append("Tài" if tai>xiu else "Xỉu")

    # Vote final
    count = Counter(votes)
    pred = "Tài" if count["Tài"]>=count["Xỉu"] else "Xỉu"
    conf = count[pred]/len(votes)

    # Ẩn random casino (90% thực tế, 10% Random)
    if random.random()>0.9: pred="Tài" if pred=="Xỉu" else "Xỉu"

    return pred, conf

# ===== TÍNH TIỀN CƯỢC =====
def calculate_bet(user):
    base_money = user["money"]
    base_percent = 0.05
    if user["lose"] == 0:
        bet = base_money * base_percent
    else:
        bet = base_money * base_percent * (2 ** (user["lose"]-1))
    if bet > base_money*0.9: bet = int(base_money*0.9)
    else: bet = int(bet)
    return max(1, bet)

def calculate_percent(conf):
    return conf*100

# ===== TELEGRAM COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI PHÂN TÍCH\n\n"
        "💰 /setmoney 1000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6 (3 viên xí ngầu)"
    )

async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    try: m = int(context.args[0])
    except: await update.message.reply_text("❗ /setmoney 1000"); return
    users[uid] = {"money": m,"start_money": m,"profit":0,"win":0,"lose":0,"last_pred":None,"last_bet":0,"history":[]}
    await update.message.reply_text(f"💰 Vốn: {money(m)}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        start_money = users[uid]["start_money"]
        users[uid] = {"money": start_money,"start_money": start_money,"profit":0,"win":0,"lose":0,"last_pred":None,"last_bet":0,"history":[]}
    await update.message.reply_text("🔄 Reset xong")

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users: del users[uid]
    await update.message.reply_text("💣 Xoá toàn bộ")

# ===== HANDLE MESSAGE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()
    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước"); return
    user = users[uid]

    for c in ["-", ",", "|"]: text = text.replace(c," ")
    nums = [int(x) for x in text.split() if x.isdigit() and 1<=int(x)<=6]
    if len(nums)!=3:
        await update.message.reply_text("❗ Nhập dạng: 3-5-6"); return
    dice = nums
    total = sum(dice)
    real = classify_total(total)

    # Hiển thị kết quả nhập
    await update.message.reply_text(f"🎲 Kết quả nhập: {dice} → {real}")

    msg_wait = await update.message.reply_text("⏳ AI đang phân tích...")
    await asyncio.sleep(1)

    # Lưu lịch sử
    user["history"].append(dice)
    if len(user["history"])>50: user["history"].pop(0)

    # WIN/LOSE
    result_text = "..."
    if user["last_pred"] is not None:
        if user["last_pred"]==real:
            user["money"] += user["last_bet"]
            user["profit"] += user["last_bet"]
            user["win"] += 1
            user["lose"] = 0
            result_text = "✅ WIN"
        else:
            user["money"] -= user["last_bet"]
            user["profit"] -= user["last_bet"]
            user["lose"] += 1
            result_text = "❌ LOSE"

    # AI dự đoán
    pred, conf = ai_predict(user)
    bet = calculate_bet(user)
    if user["money"]<=0: await update.message.reply_text("🛑 HẾT TIỀN"); return
    user["last_pred"] = pred
    user["last_bet"] = bet
    percent_total = ((user["money"]-user["start_money"])/user["start_money"]*100)

    # ===== GIAO DIỆN TELEGRAM =====
    msg = (
        "━━━━━━━━━━━━━━\n"
        "🤖 AI PHÂN TÍCH\n"
        "━━━━━━━━━━━━━━\n"
        f"🔮 Dự đoán lần tới: {pred}\n"
        f"📊 Xác suất: {calculate_percent(conf):.1f}%\n"
        "──────────────\n"
        f"💸 Cược: {money(bet)}\n"
        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        "──────────────\n"
        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "━━━━━━━━━━━━━━"
    )
    await msg_wait.edit_text(msg)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("🔥 AI CASINO PRO MAX 4.0 RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
