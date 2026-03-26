# ====== IMPORT ======
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

# ====== CONFIG ======
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI TÀI/XỈU =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx_history(history):
    return [classify_total(sum(x)) for x in history]

# ===== NHẬN DIỆN CÁC LOẠI CẦU =====
def analyze_dice_patterns(history):
    """
    Nhận diện mọi loại cầu: bệt, sâu, dài, nối, chu kỳ, zigzag
    """
    if not history:
        return None

    patterns = defaultdict(int)
    last3 = history[-3:] if len(history) >= 3 else history

    # ===== Cầu bệt =====
    for dice in last3:
        if dice[0] == dice[1] == dice[2]:
            patterns["bệt"] += 1

    # ===== Cầu dài =====
    tx = get_tx_history(history)
    if len(tx) >= 4:
        last4 = tx[-4:]
        if last4.count(last4[0]) == 4:
            patterns["dài"] += 1

    # ===== Cầu nối =====
    if len(tx) >= 6:
        for i in range(len(tx)-3):
            if tx[i:i+2] == tx[i+2:i+4]:
                patterns["nối"] += 1

    # ===== Cầu chu kỳ =====
    for i in range(len(tx)-3):
        cycle = tx[i:i+4]
        if cycle[0] == cycle[2] and cycle[1] == cycle[3]:
            patterns["chu kỳ"] += 1

    # ===== Zigzag =====
    for i in range(len(tx)-1):
        if tx[i] != tx[i+1]:
            patterns["zigzag"] += 1

    return patterns

# ===== AI DỰ ĐOÁN =====
def ai_predict(user):
    history = user["history"]
    tx = get_tx_history(history)
    patterns = analyze_dice_patterns(history)

    votes = []

    # ===== Nhận diện cầu =====
    if patterns:
        if patterns.get("bệt",0) > 0:
            votes.append("Xỉu")  # ví dụ bệt -> thiên về Xỉu
        if patterns.get("dài",0) > 0:
            votes.append(tx[-1])
        if patterns.get("nối",0) > 0:
            votes.append("Tài" if tx[-1]=="Xỉu" else "Xỉu")
        if patterns.get("chu kỳ",0) > 0:
            votes.append(tx[-1])
        if patterns.get("zigzag",0) > 0:
            votes.append("Tài" if tx[-1]=="Tài" else "Xỉu")

    # ===== Markov đơn giản =====
    markov_pred = None
    if len(tx) >= 3:
        key = tuple(tx[-3:])
        counts = Counter()
        for i in range(len(tx)-3):
            if tuple(tx[i:i+3]) == key:
                counts[tx[i+3]] += 1
        if counts:
            markov_pred = "Tài" if counts["Tài"] > counts["Xỉu"] else "Xỉu"
            votes.append(markov_pred)

    # ===== Trend =====
    if len(tx) >= 5:
        last5 = tx[-5:]
        if last5.count("Tài")>=4:
            votes.append("Tài")
        elif last5.count("Xỉu")>=4:
            votes.append("Xỉu")

    # ===== Mega Strategy =====
    if len(tx)>=6:
        last6 = tx[-6:]
        tai = last6.count("Tài")
        xiu = last6.count("Xỉu")
        votes.append("Tài" if tai>xiu else "Xỉu")

    # ===== Vote final =====
    if votes:
        count = Counter(votes)
        pred = "Tài" if count["Tài"] >= count["Xỉu"] else "Xỉu"
        conf = count[pred]/len(votes)
    else:
        pred = random.choice(["Tài","Xỉu"])
        conf = 0.5

    # ===== Điều chỉnh theo winrate =====
    winrate = user["win"] / (user["win"] + user["lose"] + 1)
    if winrate > 0.65: conf -= 0.1
    elif winrate < 0.45: conf += 0.1
    conf = max(0.51, min(0.95, conf))

    # ===== Ẩn random (giống casino) =====
    if random.random() > conf:
        pred = "Tài" if pred=="Xỉu" else "Xỉu"

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

    # ===== GIAO DIỆN ĐẸP TIẾNG VIỆT =====
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
    print("🔥 AI CASINO PRO MAX 3.0 RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
