#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
from pathlib import Path
from typing import Callable, List, Tuple, Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters


# =========================
# ENV
# =========================
def load_env() -> None:
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong file .env")
if not ADMIN_ID:
    raise RuntimeError("Thiếu ADMIN_ID trong file .env")


# =========================
# INPUT VALIDATION
# =========================
# Nhận chuỗi hex từ 8 đến 64 ký tự
HEX_RE = re.compile(r"([0-9a-fA-F]{8,64})")


def extract_hex(text: str) -> Optional[str]:
    """
    Lấy chuỗi hex hợp lệ đầu tiên trong message.
    Ví dụ:
    - "d4c72a5b3218f246041be8a0bf9ca207"
    - "hash: d4c72a5b3218f246041be8a0bf9ca207 "
    """
    if not text:
        return None
    m = HEX_RE.search(text)
    if not m:
        return None
    return m.group(1).lower()


# =========================
# HELPERS
# =========================
def norm_hex(h: str) -> str:
    return h.strip().lower()


def hex_to_int(h: str) -> int:
    return int(h, 16)


def classify_by_mod_value(v: int, m: int, bias: float = 0.5) -> str:
    """
    Nếu remainder >= m * bias => TÀI, ngược lại => XỈU
    """
    return "TÀI" if (v % m) >= (m * bias) else "XỈU"


def mod_vote(v: int, mods: List[int], bias: float = 0.5, weights: Optional[dict] = None) -> str:
    tai = 0.0
    xiu = 0.0
    weights = weights or {}
    for m in mods:
        w = float(weights.get(m, 1.0))
        if classify_by_mod_value(v, m, bias) == "TÀI":
            tai += w
        else:
            xiu += w
    return "TÀI" if tai >= xiu else "XỈU"


def slice_hex(h: str) -> Tuple[str, str, str]:
    """
    Head / Mid / Tail:
    - Head: 8 ký tự đầu
    - Mid:  8 ký tự giữa
    - Tail: 8 ký tự cuối
    """
    n = len(h)
    head = h[:8]
    tail = h[-8:] if n >= 8 else h
    mid_start = max(0, (n // 2) - 4)
    mid_end = min(n, mid_start + 8)
    mid = h[mid_start:mid_end]
    return head, mid, tail


def entropy_ratio(h: str) -> float:
    # 0..1
    return len(set(h)) / max(1, len(h))


def weighted_position_value(h: str) -> int:
    # Ký tự đầu/cuối nặng hơn
    n = len(h)
    total = 0
    wsum = 0
    for i, c in enumerate(h):
        val = int(c, 16)
        w = 2 if i == 0 or i == n - 1 else 1
        total += val * w
        wsum += w
    return total // max(1, wsum)


def chunk_xor_value(h: str) -> int:
    # Chia thành 4 khúc rồi XOR lại
    n = len(h)
    step = max(1, n // 4)
    parts = [h[i:i + step] for i in range(0, n, step)]
    v = 0
    for p in parts:
        if p:
            v ^= int(p, 16)
    return v


def dice3_from_hash(h: str) -> Tuple[int, int, int, int]:
    """
    Tạo 3 viên xí ngầu từ 3 vùng khác nhau của hash.
    Tổng 3..18.
    """
    n = len(h)
    a = h[:10] if n >= 10 else h
    b_start = max(0, (n // 2) - 5)
    b = h[b_start:b_start + 10]
    c = h[-10:] if n >= 10 else h

    d1 = (int(a, 16) % 6) + 1
    d2 = (int(b, 16) % 6) + 1
    d3 = (int(c, 16) % 6) + 1
    total = d1 + d2 + d3
    return d1, d2, d3, total


# =========================
# 9 CORE MODELS
# =========================
def model_01_baseline_sum16(h: str) -> str:
    total = sum(int(c, 16) for c in h)
    score = (total % 16) + 3
    return "TÀI" if score >= 11 else "XỈU"


def model_02_full_mod(h: str) -> str:
    v = hex_to_int(h)
    weights = {3: 2, 5: 1.5, 7: 1.5, 9: 2, 11: 1.5, 13: 1.5, 17: 2}
    return mod_vote(v, list(range(3, 19)), bias=0.50, weights=weights)


def model_04_prime_mod(h: str) -> str:
    v = hex_to_int(h)
    primes = [3, 5, 7, 11, 13, 17]
    weights = {3: 2, 5: 1.5, 7: 1.5, 11: 1.5, 13: 1.5, 17: 2}
    return mod_vote(v, primes, bias=0.50, weights=weights)


def model_08_slice_consensus(h: str) -> str:
    votes = [
        model_05_head_mod(h),
        model_06_mid_mod(h),
        model_07_tail_mod(h),
    ]
    tai = votes.count("TÀI")
    xiu = votes.count("XỈU")
    return "TÀI" if tai >= xiu else "XỈU"


def model_05_head_mod(h: str) -> str:
    head, _, _ = slice_hex(h)
    v = int(head, 16)
    return mod_vote(v, list(range(3, 19)), bias=0.50)


def model_06_mid_mod(h: str) -> str:
    _, mid, _ = slice_hex(h)
    v = int(mid, 16)
    return mod_vote(v, list(range(3, 19)), bias=0.50)


def model_07_tail_mod(h: str) -> str:
    _, _, tail = slice_hex(h)
    v = int(tail, 16)
    return mod_vote(v, list(range(3, 19)), bias=0.50)


def model_13_xor_mix(h: str) -> str:
    v = hex_to_int(h)
    mixed = v ^ (v >> 7) ^ (v << 11)
    return mod_vote(mixed, list(range(3, 19)), bias=0.50)


def model_15_power_mod(h: str) -> str:
    v = hex_to_int(h)
    mixed = (v * v) ^ (v >> 17) ^ (v << 9)
    return mod_vote(mixed, list(range(3, 19)), bias=0.50)


def model_18_dice3(h: str) -> str:
    _, _, _, total = dice3_from_hash(h)
    return "TÀI" if total >= 11 else "XỈU"


def model_19_position_weight(h: str) -> str:
    v = weighted_position_value(h)
    return "TÀI" if (v % 16) >= 8 else "XỈU"


def model_21_rolling_chunk(h: str) -> str:
    v = chunk_xor_value(h)
    return mod_vote(v, list(range(3, 19)), bias=0.50)


MODELS: List[Tuple[str, Callable[[str], str], float]] = [
    ("baseline_sum16", model_01_baseline_sum16, 1.0),
    ("full_mod", model_02_full_mod, 1.4),
    ("prime_mod", model_04_prime_mod, 1.2),
    ("slice_consensus", model_08_slice_consensus, 1.3),
    ("xor_mix", model_13_xor_mix, 1.2),
    ("power_mod", model_15_power_mod, 1.1),
    ("dice3", model_18_dice3, 1.2),
    ("position_weight", model_19_position_weight, 1.0),
    ("rolling_chunk", model_21_rolling_chunk, 1.1),
]


# =========================
# ENSEMBLE
# =========================
def predict_hex(h: str) -> Tuple[str, int, float]:
    h = norm_hex(h)

    tai_weight = 0.0
    xiu_weight = 0.0

    for _, model_fn, weight in MODELS:
        pred = model_fn(h)
        if pred == "TÀI":
            tai_weight += weight
        else:
            xiu_weight += weight

    total_weight = tai_weight + xiu_weight
    result = "TÀI" if tai_weight >= xiu_weight else "XỈU"
    confidence = int(round((max(tai_weight, xiu_weight) / max(1e-9, total_weight)) * 100))
    score = int(round((tai_weight - xiu_weight) * 10))

    return result, confidence, score


# =========================
# TELEGRAM HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "Gửi hash hex vào đây, mình sẽ chốt TÀI/XỈU + % ngay. "
        "Bot không lưu lịch sử."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    md5 = extract_hex(text)

    if not md5:
        await update.message.reply_text("Không thấy chuỗi hex hợp lệ trong tin nhắn.")
        return

    if len(md5) < 8:
        await update.message.reply_text("Chuỗi hex quá ngắn.")
        return

    result, confidence, _score = predict_hex(md5)
    await update.message.reply_text(f"{result} - {confidence}%")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle))

    print("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
