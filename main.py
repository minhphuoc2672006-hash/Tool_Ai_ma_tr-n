#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters


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
HEX_RE = re.compile(r"([0-9a-fA-F]{8,64})")


def extract_hex(text: str) -> Optional[str]:
    if not text:
        return None
    m = HEX_RE.search(text)
    if not m:
        return None
    return m.group(1).lower()


def strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def normalize_text(text: str) -> str:
    return strip_accents(text).upper().strip()


def parse_result_label(text: str) -> Optional[str]:
    t = normalize_text(text)
    t = re.sub(r"\s+", " ", t)

    if t in {"T", "TAI", "TÀI"}:
        return "TÀI"
    if t in {"X", "XIU", "XỈU"}:
        return "XỈU"

    # Cho phép kiểu "KET QUA TAI", "RESULT XIU", v.v.
    if "TAI" in t and "XIU" not in t:
        return "TÀI"
    if "XIU" in t and "TAI" not in t:
        return "XỈU"
    return None


# =========================
# HELPERS
# =========================
def norm_hex(h: str) -> str:
    return h.strip().lower()


def hex_to_int(h: str) -> int:
    return int(h, 16)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def classify_by_mod_value(v: int, m: int, bias: float = 0.5) -> str:
    return "TÀI" if (v % m) >= (m * bias) else "XỈU"


def slice_hex(h: str) -> Tuple[str, str, str]:
    n = len(h)
    head = h[:8]
    tail = h[-8:] if n >= 8 else h
    mid_start = max(0, (n // 2) - 4)
    mid_end = min(n, mid_start + 8)
    mid = h[mid_start:mid_end]
    return head, mid, tail


def weighted_position_value(h: str) -> int:
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
    n = len(h)
    step = max(1, n // 4)
    parts = [h[i:i + step] for i in range(0, n, step)]
    v = 0
    for p in parts:
        if p:
            v ^= int(p, 16)
    return v


def dice3_from_hash(h: str) -> Tuple[int, int, int, int]:
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
# ADAPTIVE BRAIN
# =========================
MODS_MAIN = [3, 5, 7, 11, 13, 17, 19, 23, 29]
PRIME_MODS = [3, 5, 7, 11, 13, 17, 19, 23, 29]

BASE_MOD_WEIGHTS: Dict[int, float] = {
    3: 1.20,
    5: 1.15,
    7: 1.25,
    11: 1.45,
    13: 1.45,
    17: 1.55,
    19: 1.60,
    23: 1.75,
    29: 1.90,
}

MODEL_BASE_WEIGHTS: Dict[str, float] = {
    "baseline_sum16": 1.00,
    "full_mod": 1.45,
    "prime_mod": 1.25,
    "slice_consensus": 1.30,
    "xor_mix": 1.25,
    "power_mod": 1.15,
    "dice3": 1.20,
    "position_weight": 1.00,
    "rolling_chunk": 1.15,
}


@dataclass
class PendingCase:
    hash_text: str
    model_preds: Dict[str, str]
    final_pred: str
    confidence: int
    score: int


@dataclass
class AdaptiveBrain:
    model_ewma: Dict[str, float] = field(default_factory=dict)
    mod_ewma: Dict[int, float] = field(default_factory=dict)
    pending_case: Optional[PendingCase] = None

    def __post_init__(self) -> None:
        for name in MODEL_BASE_WEIGHTS:
            self.model_ewma.setdefault(name, 0.0)
        for m in MODS_MAIN:
            self.mod_ewma.setdefault(m, 0.0)

    def reset_learning(self) -> None:
        for k in self.model_ewma:
            self.model_ewma[k] = 0.0
        for k in self.mod_ewma:
            self.mod_ewma[k] = 0.0
        self.pending_case = None

    def model_weight(self, model_name: str) -> float:
        base = MODEL_BASE_WEIGHTS.get(model_name, 1.0)
        skill = self.model_ewma.get(model_name, 0.0)
        return base * clamp(1.0 + (skill * 0.45), 0.55, 1.80)

    def mod_weight(self, m: int) -> float:
        base = BASE_MOD_WEIGHTS.get(m, 1.0)
        skill = self.mod_ewma.get(m, 0.0)
        return base * clamp(1.0 + (skill * 0.50), 0.55, 2.20)

    def vote_mod(self, v: int, mods: List[int], bias: float = 0.5) -> str:
        tai = 0.0
        xiu = 0.0
        for m in mods:
            w = self.mod_weight(m)
            if classify_by_mod_value(v, m, bias) == "TÀI":
                tai += w
            else:
                xiu += w
        return "TÀI" if tai >= xiu else "XỈU"

    def model_01_baseline_sum16(self, h: str) -> str:
        total = sum(int(c, 16) for c in h)
        score = (total % 16) + 3
        return "TÀI" if score >= 11 else "XỈU"

    def model_02_full_mod(self, h: str) -> str:
        v = hex_to_int(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_04_prime_mod(self, h: str) -> str:
        v = hex_to_int(h)
        return self.vote_mod(v, PRIME_MODS, bias=0.50)

    def model_05_head_mod(self, h: str) -> str:
        head, _, _ = slice_hex(h)
        v = int(head, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_06_mid_mod(self, h: str) -> str:
        _, mid, _ = slice_hex(h)
        v = int(mid, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_07_tail_mod(self, h: str) -> str:
        _, _, tail = slice_hex(h)
        v = int(tail, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_08_slice_consensus(self, h: str) -> str:
        votes = [
            self.model_05_head_mod(h),
            self.model_06_mid_mod(h),
            self.model_07_tail_mod(h),
        ]
        tai = votes.count("TÀI")
        xiu = votes.count("XỈU")
        return "TÀI" if tai >= xiu else "XỈU"

    def model_13_xor_mix(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = v ^ (v >> 7) ^ (v << 11)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_15_power_mod(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = (v * v) ^ (v >> 17) ^ (v << 9)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_18_dice3(self, h: str) -> str:
        _, _, _, total = dice3_from_hash(h)
        return "TÀI" if total >= 11 else "XỈU"

    def model_19_position_weight(self, h: str) -> str:
        v = weighted_position_value(h)
        return "TÀI" if (v % 16) >= 8 else "XỈU"

    def model_21_rolling_chunk(self, h: str) -> str:
        v = chunk_xor_value(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def predict(self, h: str) -> Tuple[str, int, int, Dict[str, str]]:
        h = norm_hex(h)

        models: List[Tuple[str, Callable[[str], str]]] = [
            ("baseline_sum16", self.model_01_baseline_sum16),
            ("full_mod", self.model_02_full_mod),
            ("prime_mod", self.model_04_prime_mod),
            ("slice_consensus", self.model_08_slice_consensus),
            ("xor_mix", self.model_13_xor_mix),
            ("power_mod", self.model_15_power_mod),
            ("dice3", self.model_18_dice3),
            ("position_weight", self.model_19_position_weight),
            ("rolling_chunk", self.model_21_rolling_chunk),
        ]

        tai_weight = 0.0
        xiu_weight = 0.0
        preds: Dict[str, str] = {}

        for name, fn in models:
            pred = fn(h)
            preds[name] = pred
            w = self.model_weight(name)
            if pred == "TÀI":
                tai_weight += w
            else:
                xiu_weight += w

        total_weight = tai_weight + xiu_weight
        result = "TÀI" if tai_weight >= xiu_weight else "XỈU"
        confidence = int(round((max(tai_weight, xiu_weight) / max(1e-9, total_weight)) * 100))
        score = int(round((tai_weight - xiu_weight) * 10))

        self.pending_case = PendingCase(
            hash_text=h,
            model_preds=preds,
            final_pred=result,
            confidence=confidence,
            score=score,
        )
        return result, confidence, score, preds

    def learn_from_feedback(self, actual: str) -> Optional[Dict[str, object]]:
        if self.pending_case is None:
            return None

        actual = actual.upper().strip()
        case = self.pending_case

        model_correct = 0
        for name, pred in case.model_preds.items():
            delta = 1.0 if pred == actual else -1.0
            self.model_ewma[name] = (self.model_ewma.get(name, 0.0) * 0.92) + (delta * 0.08)
            if pred == actual:
                model_correct += 1

        v = int(case.hash_text, 16)
        for m in MODS_MAIN:
            pred_m = classify_by_mod_value(v, m, bias=0.50)
            delta = 1.0 if pred_m == actual else -1.0
            self.mod_ewma[m] = (self.mod_ewma.get(m, 0.0) * 0.93) + (delta * 0.07)

        self.pending_case = None

        return {
            "actual": actual,
            "final_pred": case.final_pred,
            "confidence": case.confidence,
            "score": case.score,
            "model_correct": model_correct,
            "model_total": len(case.model_preds),
        }

    def status_text(self) -> str:
        top_models = sorted(self.model_ewma.items(), key=lambda x: x[1], reverse=True)[:3]
        top_mods = sorted(self.mod_ewma.items(), key=lambda x: x[1], reverse=True)[:3]

        lines = ["Trạng thái học hiện tại:"]
        lines.append("Mô hình mạnh nhất: " + ", ".join(f"{n}({v:+.2f})" for n, v in top_models))
        lines.append("Mod mạnh nhất: " + ", ".join(f"{m}({v:+.2f})" for m, v in top_mods))

        if self.pending_case:
            lines.append(f"Dự đoán gần nhất: {self.pending_case.final_pred} - {self.pending_case.confidence}%")

        return "\n".join(lines)


brain = AdaptiveBrain()
LOCK = asyncio.Lock()


# =========================
# TELEGRAM HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "Gửi hash hex vào đây để bot chốt TÀI/XỈU.\n"
        "Sau đó gửi kết quả thực tế là TÀI hoặc XỈU để bot tự học.\n"
        "Bot không lưu lịch sử chat, chỉ giữ trạng thái học và ca gần nhất."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "/start\n"
        "/status\n"
        "/reset\n\n"
        "Cách dùng:\n"
        "1) Gửi hash hex\n"
        "2) Nhận kết quả TÀI/XỈU + %\n"
        "3) Gửi kết quả thật là TÀI hoặc XỈU để bot tự thích nghi"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    async with LOCK:
        await update.message.reply_text(brain.status_text())


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    async with LOCK:
        brain.reset_learning()
    await update.message.reply_text("Đã xóa trạng thái học và ca dự đoán gần nhất.")


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    async with LOCK:
        # Ưu tiên nhận feedback nếu đang có ca gần nhất
        actual = parse_result_label(text)
        if actual and brain.pending_case is not None:
            info = brain.learn_from_feedback(actual)
            if info is None:
                await update.message.reply_text("Chưa có ca dự đoán nào để học.")
                return

            await update.message.reply_text(
                f"Đã học từ phản hồi: {actual}\n"
                f"Bot vừa dự đoán: {info['final_pred']} - {info['confidence']}%\n"
                f"Mô hình đúng: {info['model_correct']}/{info['model_total']}"
            )
            return

        # Nếu là hash thì dự đoán
        h = extract_hex(text)
        if h:
            if len(h) < 8:
                await update.message.reply_text("Chuỗi hex quá ngắn.")
                return

            result, confidence, score, _preds = brain.predict(h)

            await update.message.reply_text(
                f"{result} - {confidence}%\n"
                f"Score: {score}\n"
                f"Giờ hãy gửi kết quả thật: TÀI hoặc XỈU để bot tự thích nghi."
            )
            return

        # Không phải hash, không phải feedback
        await update.message.reply_text(
            "Không thấy hash hex hợp lệ.\n"
            "Hoặc gửi hash, hoặc gửi TÀI/XỈU để feedback."
        )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle))

    print("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
