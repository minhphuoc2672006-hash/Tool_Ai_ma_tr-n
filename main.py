# ====== IMPORT ======
import os
import logging
import asyncio
import random
from collections import defaultdict, Counter, deque
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ====== CONFIG ======
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== FORMAT TIỀN =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI TÀI/XỈU =====
def classify(total):
    return "Tài" if total >= 11 else "Xỉu"

def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx(history):
    return ["Tài" if sum(x)>=11 else "Xỉu" for x in history]

def get_tx_history(history):
    return [classify_total(sum(x)) for x in history]

# ===== CHIẾN THUẬT MỞ RỘNG =====
BASE_STRATEGIES = [
    "FOLLOW","OPPOSITE","TREND","RANDOM",
    "STREAK","ANTI_STREAK","WEIGHTED",
    "LAST2","ALT","BREAK","CHAOS"
]

PATTERN_EXTENSIONS = [f"P{i}" for i in range(1,21)]
MARKOV_EXTENSIONS = [f"M{i}" for i in range(1,11)]
HYBRID_EXTENSIONS = [f"H{i}" for i in range(1,21)]
BETTING_EXTENSIONS = ["MARTINGALE","REVERSE","FIBONACCI"]

STRATEGIES = []
for base in BASE_STRATEGIES:
    for pat in PATTERN_EXTENSIONS:
        for mark in MARKOV_EXTENSIONS:
            for hyb in HYBRID_EXTENSIONS:
                STRATEGIES.append(f"{base}_{pat}_{mark}_{hyb}")
STRATEGIES += BETTING_EXTENSIONS
STRATEGIES = STRATEGIES[:1200]

# ===== NHẬN DIỆN CÁC LOẠI CẦU LOGIC (ẨN) =====
def analyze_patterns(history):
    if not history:
        return {}
    tx = get_tx_history(history)
    patterns = defaultdict(int)
    # Cầu bệt
    for dice in history[-3:]:
        if dice[0]==dice[1]==dice[2]:
            patterns["bệt"] +=1
    # Cầu dài
    if len(tx)>=4:
        last4 = tx[-4:]
        if last4.count(last4[0])==4:
            patterns["dài"] +=1
    # Cầu nối
    for i in range(len(tx)-3):
        if tx[i:i+2]==tx[i+2:i+4]:
            patterns["nối"] +=1
    # Cầu chu kỳ
    for i in range(len(tx)-3):
        cycle = tx[i:i+4]
        if cycle[0]==cycle[2] and cycle[1]==cycle[3]:
            patterns["chu kỳ"] +=1
    # Zigzag
    for i in range(len(tx)-1):
        if tx[i]!=tx[i+1]:
            patterns["zigzag"] +=1
    # Cầu phức tạp
    for i in range(len(tx)-5):
        pattern = tx[i:i+6]
        if pattern[0]==pattern[2] and pattern[1]==pattern[3] and pattern[4]!=pattern[5]:
            patterns["phức tạp"] +=1
    return patterns

# ===== CHẠY CHIẾN THUẬT =====
def run_strategy(name, history):
    tx = get_tx(history)
    if not tx:
        return random.choice(["Tài","Xỉu"])
    base = name.split("_")[0]
    if base == "FOLLOW": return tx[-1]
    if base == "OPPOSITE": return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base == "TREND":
        if len(tx)>=4:
            return "Tài" if tx[-4:].count("Tài")>tx[-4:].count("Xỉu") else "Xỉu"
    if base == "STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return tx[-1]
    if base=="ANTI_STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base=="WEIGHTED":
        c = Counter(tx)
        return "Tài" if c["Tài"]>c["Xỉu"] else "Xỉu"
    if base=="LAST2":
        if len(tx)>=2:
            return tx[-2]
    if base=="ALT":
        if len(tx)>=2:
            return "Xỉu" if tx[-1]==tx[-2] else tx[-1]
    if base=="BREAK":
        if len(tx)>=3 and tx[-1]==tx[-2]==tx[-3]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base=="CHAOS":
        return random.choice(tx)
    if "P" in name:
        if len(tx)>=3:
            return "Tài" if tx[-3:].count("Tài")>tx[-3:].count("Xỉu") else "Xỉu"
    if "M" in name:
        if len(tx)>=2:
            last = tx[-2:]
            count_follow = sum(1 for i in range(len(tx)-2) if tx[i:i+2]==last and tx[i+2]=="Tài")
            count_follow_xiu = sum(1 for i in range(len(tx)-2) if tx[i:i+2]==last and tx[i+2]=="Xỉu")
            if count_follow + count_follow_xiu==0:
                return random.choice(["Tài","Xỉu"])
            return "Tài" if count_follow>=count_follow_xiu else "Xỉu"
    if "H" in name:
        c = Counter(tx[-5:])
        return "Tài" if c["Tài"]>=c["Xỉu"] else "Xỉu"
    if name in BETTING_EXTENSIONS:
        return random.choice(["Tài","Xỉu"])
    return random.choice(["Tài","Xỉu"])

def evaluate_strategies(history):
    if len(history)<5: return None
    scores={}
    tx=get_tx(history)
    for strat in STRATEGIES:
        wins=0
        for i in range(1,len(history)):
            pred=run_strategy(strat,history[:i])
            real=tx[i]
            if pred==real: wins+=1
        scores[strat]=wins
    best=max(scores,key=lambda k:scores[k])
    return best

# ===== AI DỰ ĐOÁN =====
def ai_predict(user):
    history=user["history"]
    tx=get_tx_history(history)
    patterns=analyze_patterns(history)
    votes=[]
    if patterns:
        if patterns.get("bệt",0): votes.append("Xỉu")
        if patterns.get("dài",0): votes.append(tx[-1])
        if patterns.get("nối",0): votes.append("Tài" if tx[-1]=="Xỉu" else "Xỉu")
        if patterns.get("chu kỳ",0): votes.append(tx[-1])
        if patterns.get("zigzag",0): votes.append("Tài" if tx[-1]=="Tài" else "Xỉu")
        if patterns.get("phức tạp",0): votes.append(tx[-1])
    if len(tx)>=3:
        key=tuple(tx[-3:])
        counts=Counter()
        for i in range(len(tx)-3):
            if tuple(tx[i:i+3])==key:
                counts[tx[i+3]]+=1
        if counts:
            markov_pred="Tài" if counts["Tài"]>counts["Xỉu"] else "Xỉu"
            votes.append(markov_pred)
    if len(tx)>=5:
        last5=tx[-5:]
        if last5.count("Tài")>=4: votes.append("Tài")
        elif last5.count("Xỉu")>=4: votes.append("Xỉu")
    if len(tx)>=6:
        last6=tx[-6:]
        tai=last6.count("Tài")
        xiu=last6.count("Xỉu")
        votes.append("Tài" if tai>xiu else "Xỉu")
    if votes:
        count=Counter(votes)
        pred="Tài" if count["Tài"]>=count["Xỉu"] else "Xỉu"
        conf=count[pred]/len(votes)
    else:
        pred=random.choice(["Tài","Xỉu"])
        conf=0.5
    winrate=user["win"]/(user["win"]+user["lose"]+1)
    if winrate>0.65: conf-=0.1
    elif winrate<0.45: conf+=0.1
    conf=max(0.51,min(0.95,conf))
    if random.random()>conf:
        pred="Tài" if pred=="Xỉu" else "Xỉu"
    return pred, conf

# ===== GẤP THÉP / TÍNH TIỀN =====
def bet_calc(user):
    base=user["start"]*0.05
    if user["lose"]==0:
        return int(base)
    fib_seq=[1,1,2,3,5,8,13,21,34,55]
    bet=base*(2**user["lose"])
    if user["lose"]<len(fib_seq):
        bet=base*fib_seq[user["lose"]]
    return int(min(bet,user["money"]))

def calculate_bet(user):
    base_money=user["money"]
    base_percent=0.05
    if user["lose"]==0:
        bet=base_money*base_percent
    else:
        bet=base_money*base_percent*(2**(user["lose"]-1))
    bet=min(int(base_money*0.9),int(bet))
    return max(1,bet)

def calculate_percent(conf):
    return conf*100

# ===== TELEGRAM COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI CASINO PRO MAX 5.0 / TÀI XỈU MỞ RỘNG\n"
        "💰 /setmoney 1000\n🔄 /reset\n💣 /resetall\n"
        "📥 Nhập 3 số: 3-5-6"
    )

async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.message.from_user.id
    try: m=int(context.args[0])
    except: await update.message.reply_text("❗ /setmoney 1000"); return
    users[uid]={"money":m,"start":m,"start_money":m,"profit":0,"win":0,"lose":0,"last_pred":None,"last_bet":0,"history":[],"strategy":random.choice(STRATEGIES)}
    await update.message.reply_text(f"💰 Vốn: {money(m)}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.message.from_user.id
    if uid in users:
        start_money=users[uid]["start_money"]
        users[uid]={"money":start_money,"start_money":start_money,"profit":0,"win":0,"lose":0,"last_pred":None,"last_bet":0,"history":[],"strategy":random.choice(STRATEGIES)}
    await update.message.reply_text("🔄 Reset xong")

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.message.from_user.id
    if uid in users: del users[uid]
    await update.message.reply_text("💣 Xoá toàn bộ")

# ===== HANDLE MESSAGE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.message.from_user.id
    text=update.message.text.strip()
    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước"); return
    user=users[uid]
    for c in ["-","|",","]: text=text.replace(c," ")
    nums=[int(x) for x in text.split() if x.isdigit() and 1<=int(x)<=6]
    if len(nums)!=3:
        await update.message.reply_text("❗ Nhập dạng 3-5-6"); return
    dice=nums
    real=classify_total(sum(dice))
    msg_wait=await update.message.reply_text("⏳ AI đang phân tích...")
    await asyncio.sleep(1)
    user["history"].append(dice)
    if len(user["history"])>50: user["history"].pop(0)
    result_text="..."
    if user["last_pred"] is not None:
        if user["last_pred"]==real:
            user["money"]+=user["last_bet"]
            user["profit"]+=user["last_bet"]
            user["win"]+=1
            user["lose"]=0
            result_text="✅ WIN"
        else:
            user["money"]-=user["last_bet"]
            user["profit"]-=user["last_bet"]
            user["lose"]+=1
            result_text="❌ LOSE"
    best_strategy=evaluate_strategies(user["history"])
    if best_strategy: user["strategy"]=best_strategy
    pred,conf=ai_predict(user)
    next_bet=bet_calc(user)
    if user["money"]<=0: await update.message.reply_text("🛑 HẾT TIỀN"); return
    user["last_pred"]=pred
    user["last_bet"]=next_bet
    msg=(
        "━━━━━━━━━━━━━━\n"
        "🤖 AI CASINO PRO MAX 5.0\n"
        "━━━━━━━━━━━━━━\n"
        f"🔮 Dự đoán: {pred}\n"
        f"📊 Xác suất: {conf:.1f}%\n"
        "──────────────\n"
        f"💸 Tiền cược: {money(next_bet)}\n"
        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        "──────────────\n"
        f"🏆 Win | ❌ Lose: {user['win']} | {user['lose']}\n"
        "━━━━━━━━━━━━━━"
    )
    await msg_wait.edit_text(msg)

# ===== MAIN =====
def main():
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("setmoney",setmoney))
    app.add_handler(CommandHandler("reset",reset))
    app.add_handler(CommandHandler("resetall",resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))
    print("🔥 AI CASINO PRO MAX 5.0 RUNNING...")
    app.run_polling()

if __name__=="__main__":
    main()
