import os
import asyncio
import logging
import random
import hashlib
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

MIN_SAMPLE = 20

# ===== DATA =====
users = {}

# ===== RESET CHUẨN =====
def new_user():
    return {
        "history":[],
        "win":0,"lose":0,"total":0,"last_pred":None,

        "markov3":defaultdict(Counter),
        "markov5":defaultdict(Counter),
        "markov7":defaultdict(Counter),

        "random_history":[],
        "random_win":0,
        "random_total":0
    }

# ===== UTIL =====
def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def short(tx):
    return "T" if tx == "Tài" else "X"

def parse_input(text):
    parts = text.replace("-", " ").split()
    return [int(p) for p in parts if p.isdigit() and 1 <= int(p) <= 18]

# ===== RANDOM GAME =====
def random_from_number(num):
    h = hashlib.md5(str(num).encode()).hexdigest()
    seed = int(h[:8], 16)
    random.seed(seed)
    roll = random.randint(3, 18)
    return "Tài" if roll >= 11 else "Xỉu", roll

# ===== MARKOV =====
def update_markov(data):
    history = data["history"]

    # 🔥 RESET TRƯỚC KHI BUILD (fix cộng dồn)
    data["markov3"].clear()
    data["markov5"].clear()
    data["markov7"].clear()

    for k in [3,5,7]:
        if len(history) >= k+1:
            for i in range(len(history)-k):
                key = tuple(history[i:i+k])
                nxt = history[i+k]
                data[f"markov{k}"][key][nxt] += 1

def markov_predict(data):
    history = data["history"]
    preds = []

    def get_pred(table, k, w):
        if len(history) >= k:
            key = tuple(history[-k:])
            if key in table:
                nxt = table[key]
                pred = nxt.most_common(1)[0][0]
                conf = nxt[pred]/sum(nxt.values())
                preds.append((pred, conf * w))

    get_pred(data["markov7"], 7, 120)
    get_pred(data["markov5"], 5, 100)
    get_pred(data["markov3"], 3, 80)

    return preds

# ===== PHÂN TÍCH =====
def streak(history):
    if len(history) < 3: return []
    last = history[-1]
    c = 1
    for i in range(len(history)-2,-1,-1):
        if history[i] == last: c+=1
        else: break

    if c >= 4:
        return [("Xỉu" if last=="Tài" else "Tài",90)]
    elif c == 3:
        return [("Xỉu" if last=="Tài" else "Tài",75)]
    else:
        return [(last,55)]

def pattern(history):
    p=[]
    if len(history)>=6:
        if history[-1]!=history[-2] and history[-3]!=history[-4]:
            p.append((history[-2],70))
        if history[-1]==history[-3]:
            p.append((history[-1],65))
    return p

def freq(history):
    if len(history)<6: return []
    c=Counter(history)
    t=len(history)
    pred=c.most_common(1)[0][0]
    return [(pred, c[pred]/t*100)]

# ===== AI =====
def ai_predict(data):
    if len(data["history"]) < MIN_SAMPLE:
        return None, None

    update_markov(data)

    preds=[]
    preds+=markov_predict(data)
    preds+=streak(data["history"])
    preds+=pattern(data["history"])
    preds+=freq(data["history"])

    if not preds:
        return random.choice(["Tài","Xỉu"]),50

    score=defaultdict(float)
    for p,c in preds:
        score[p]+=c

    final=max(score,key=score.get)
    conf=int(score[final]/sum(score.values())*100)
    return final, conf

# ===== CHỌN AI MẠNH =====
def choose_ai(data):
    ai_rate = (data["win"]/data["total"]*100) if data["total"] else 0
    rd_rate = (data["random_win"]/data["random_total"]*100) if data["random_total"] else 0

    if rd_rate > ai_rate:
        return "RANDOM"
    return "AI"

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key=get_key(update)

    users.setdefault(key, new_user())
    data=users[key]

    text=update.message.text.strip() if update.message.text else ""

    # ===== RESET =====
    if text=="/reset":
        users[key] = new_user()
        await update.message.reply_text("🔄 Reset sạch toàn bộ dữ liệu")
        return

    # ===== RANDOM INPUT =====
    if text.isdigit() and len(text)>=3:
        tx,roll=random_from_number(int(text))
        data["random_history"].append(tx)
        await update.message.reply_text(f"🎲 Random: {roll} → {tx}")
        return

    nums=parse_input(text)
    if not nums: return

    for n in nums:
        tx=to_tx(n)
        msg=f"KQ: {n} ({tx})"

        if data["last_pred"]:
            if data["last_pred"]==tx:
                data["win"]+=1
                data["lose"]=0
                msg+=" → ✅"
            else:
                data["lose"]+=1
                msg+=" → ❌"

        await update.message.reply_text(msg)

        data["history"].append(tx)
        data["total"]+=1

    # ===== FIX RANDOM WIN (KHÔNG CỘNG DỒN) =====
    data["random_win"] = 0
    for i in range(min(len(data["history"]), len(data["random_history"]))):
        if data["history"][i]==data["random_history"][i]:
            data["random_win"]+=1

    data["random_total"]=len(data["random_history"])

    # ===== HIỂN THỊ =====
    cau=" ".join([short(x) for x in data["history"][-20:]])

    ai_rate=int((data["win"]/data["total"])*100) if data["total"] else 0
    rd_rate=int((data["random_win"]/data["random_total"])*100) if data["random_total"] else 0

    await update.message.reply_text(
        f"📊 AI: {ai_rate}% | 🎲 Random: {rd_rate}%\n"
        f"📈 Cầu: {cau}"
    )

    wait=await update.message.reply_text("⏳ Đang phân tích...")
    await asyncio.sleep(1)

    pred,conf=ai_predict(data)

    if pred is None:
        await wait.edit_text(f"⚠️ Chưa đủ mẫu ({len(data['history'])}/{MIN_SAMPLE})")
        return

    mode=choose_ai(data)

    if mode=="RANDOM":
        pred=random.choice(["Tài","Xỉu"])
        conf=55

    data["last_pred"]=pred

    await wait.edit_text(
        f"🔥 Dự đoán: {pred}\n"
        f"🎯 Tỷ lệ: {conf}%\n"
        f"🤖 Mode: {mode}"
    )

# ===== RUN =====
if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()
