import os
import asyncio
import logging
import random
import requests
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")  # OCR online API key
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")
if not OCR_API_KEY:
    raise Exception("❌ Thiếu OCR_API_KEY")

users = {}
markov = defaultdict(Counter)

# ===== UTIL =====
def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    nums = [int(x) for x in text.split() if x.isdigit()]
    if len(nums) == 3:
        return sum(nums)
    if len(nums) == 1:
        return nums[0]
    return None

# ===== AI =====
def update_markov(history):
    for i in range(len(history)-1):
        markov[history[i]][history[i+1]] += 1

def markov_predict(history):
    if not history:
        return "Tài", 50
    last = history[-1]
    if last in markov:
        nxt = markov[last]
        total = sum(nxt.values())
        pred = nxt.most_common(1)[0][0]
        conf = int(nxt[pred]/total*100)
        return pred, conf
    return random.choice(["Tài","Xỉu"]), 50

def freq_predict(history):
    if len(history) < 5:
        return "Tài", 50
    c = Counter(history[-30:])
    pred = c.most_common(1)[0][0]
    conf = int(c[pred]/len(history[-30:])*100)
    return pred, conf

def anti_streak(history):
    if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
        return ("Xỉu" if history[-1]=="Tài" else "Tài"), 65
    return history[-1] if history else "Tài", 55

def detect_pattern(history):
    if len(history) < 6:
        return None
    if history[-1] == history[-3] and history[-2] == history[-4]:
        return "repeat"
    return None

def final_ai(history):
    update_markov(history)
    preds, confs = [], []
    for f in [markov_predict, freq_predict, anti_streak]:
        p,c = f(history)
        preds.append(p)
        confs.append(c)
    pattern = detect_pattern(history)
    if pattern == "repeat":
        preds.append(history[-1])
        confs.append(75)
    final = Counter(preds).most_common(1)[0][0]
    conf = int(sum(confs)/len(confs))
    return final, conf

# ===== OCR ONLINE =====
def ocr_image(path):
    """Gọi OCR miễn phí online (api.ocr.space)"""
    url_api = "https://api.ocr.space/parse/image"
    with open(path, "rb") as f:
        r = requests.post(
            url_api,
            files={"file": f},
            data={"apikey": OCR_API_KEY, "language":"eng"}
        )
    try:
        result = r.json()
        text = result["ParsedResults"][0]["ParsedText"]
        return text
    except:
        return ""

def extract_numbers(text):
    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]
    # Gom từng 3 viên xí ngầu
    sums = []
    for i in range(0, len(nums),3):
        if i+2 < len(nums):
            sums.append(sum(nums[i:i+3]))
    return sums

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, {"history":[], "win":0, "lose":0, "total":0, "last_pred":None})
    data = users[key]

    # Xử lý reset
    if update.message.text and update.message.text.strip()=="/reset":
        users[key] = {"history":[], "win":0,"lose":0,"total":0,"last_pred":None}
        await update.message.reply_text("Đã reset")
        return

    nums = []
    if update.message.text:
        n = parse_input(update.message.text.strip())
        if n: nums.append(n)

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        path = f"{key}.jpg"
        await file.download_to_drive(path)
        await update.message.reply_text("Đang đọc ảnh...")
        text = ocr_image(path)
        nums.extend(extract_numbers(text))

    if not nums:
        return

    for n in nums:
        if not (3 <= n <= 18):
            continue
        tx = to_tx(n)
        msg = f"Kết quả: {n} ({tx})"
        if data["last_pred"]:
            if data["last_pred"] == tx:
                data["win"] += 1
                msg += f"\nDự đoán trước: {data['last_pred']} → Thắng"
            else:
                data["lose"] += 1
                msg += f"\nDự đoán trước: {data['last_pred']} → Thua"
        await update.message.reply_text(msg)
        data["total"] += 1
        data["history"].append(tx)
        rate = int((data["win"]/data["total"])*100) if data["total"] else 0
        await update.message.reply_text(f"Tổng: {data['total']}\nThắng: {data['win']}\nThua: {data['lose']}\nTỷ lệ: {rate}%")

    # Dự đoán AI
    wait = await update.message.reply_text("Đang phân tích...")
    await asyncio.sleep(5)
    pred, conf = final_ai(data["history"])
    data["last_pred"] = pred
    await wait.edit_text(f"Dự đoán: {pred}\nTỷ lệ: {conf}%")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle))
    app.run_polling()
