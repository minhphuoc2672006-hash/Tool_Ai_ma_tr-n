#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import html
import os
import re
import unicodedata
from collections import Counter
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
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0").strip()

try:
    ADMIN_ID = int(ADMIN_ID_RAW or "0")
except ValueError as exc:
    raise RuntimeError("ADMIN_ID trong .env phải là số nguyên") from exc

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
    """
    Chỉ nhận feedback thật sự là TÀI / XỈU.
    Không bắt nhầm các câu như "HIEN TAI".
    """
    t = normalize_text(text)
    t = re.sub(r"\s+", " ", t)
    tokens = re.findall(r"[A-Z0-9]+", t)

    if not tokens:
        return None

    if tokens in (["T"], ["TAI"]):
        return "TÀI"
    if tokens in (["X"], ["XIU"]):
        return "XỈU"

    if len(tokens) <= 3 and tokens[0] in {"KET", "RESULT", "DAPAN", "DAP", "PHANHOI", "KQ"}:
        if tokens[-1] == "TAI" and "XIU" not in tokens:
            return "TÀI"
        if tokens[-1] == "XIU" and "TAI" not in tokens:
            return "XỈU"

    return None


# =========================
# DISPLAY HELPERS
# =========================
def esc(text: str) -> str:
    return html.escape(str(text))


def format_prediction_message(result: str, confidence: int, score: int, h: str) -> str:
    short_hash = h[:18] + ("..." if len(h) > 18 else "")
    return (
        f"🧠 <b>AI PREDICTION</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📥 <b>Hash:</b> <code>{esc(short_hash)}</code>\n"
        f"🎯 <b>Kết quả:</b> <b>{esc(result)}</b>\n"
        f"📊 <b>Độ tin cậy:</b> <b>{confidence}%</b>\n"
        f"⚡ <b>Score:</b> <b>{score}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🔄 Gửi <b>TÀI</b> hoặc <b>XỈU</b> để bot học."
    )


def format_feedback_message(actual: str, final_pred: str, confidence: int, model_correct: int, model_total: int) -> str:
    return (
        f"✅ <b>PHẢN HỒI ĐÃ GHI NHẬN</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧾 <b>Kết quả thật:</b> <b>{esc(actual)}</b>\n"
        f"🤖 <b>Bot đã đoán:</b> <b>{esc(final_pred)}</b>\n"
        f"📊 <b>Độ tin cậy:</b> <b>{confidence}%</b>\n"
        f"📈 <b>Mô hình đúng:</b> <b>{model_correct}/{model_total}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧠 Bot đã tự cập nhật."
    )


def format_status_message(text: str) -> str:
    return (
        f"📡 <b>TRẠNG THÁI BOT</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<pre>{esc(text)}</pre>"
    )


def format_start_message() -> str:
    return (
        f"🚀 <b>BOT TÀI/XỈU</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"• Gửi hash hex để phân tích\n"
        f"• Gửi <b>TÀI</b> hoặc <b>XỈU</b> để feedback\n"
        f"• /status xem trạng thái\n"
        f"• /reset để xóa sạch bộ nhớ\n"
    )


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


def hex_chunks(h: str, size: int) -> List[str]:
    size = max(1, size)
    return [h[i:i + size] for i in range(0, len(h), size) if h[i:i + size]]


def nibble_values(h: str) -> List[int]:
    return [int(c, 16) for c in h]


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
    parts = hex_chunks(h, step)
    v = 0
    for p in parts:
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
# MODEL CONFIG
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
    "head_mod": 1.10,
    "mid_mod": 1.10,
    "tail_mod": 1.10,
    "slice_consensus": 1.30,
    "xor_mix": 1.25,
    "power_mod": 1.15,
    "dice3": 1.20,
    "position_weight": 1.00,
    "rolling_chunk": 1.15,
    "reverse_parity": 1.05,
    "bit_count_parity": 1.05,
    "pair_diff": 1.10,
    "alternating_sum": 1.05,
    "chunk_sum": 1.05,
    "entropy_balance": 1.00,
    "edge_balance": 1.05,
    "ascii_mix": 1.00,
    "mirror_balance": 1.10,
    "window_mix": 1.05,
    "zigzag_balance": 1.05,
}


# =========================
# STATE
# =========================
@dataclass
class PendingCase:
    hash_text: str
    model_preds: Dict[str, str]
    final_pred: str
    confidence: int
    score: int


@dataclass
class AdaptiveBrain:
    model_skill: Dict[str, float] = field(default_factory=dict)
    mod_skill: Dict[int, float] = field(default_factory=dict)
    pending_case: Optional[PendingCase] = None

    prediction_count: int = 0
    feedback_count: int = 0
    streak_loss: int = 0
    streak_win: int = 0
    last_hash: Optional[str] = None
    last_feedback: Optional[str] = None

    def __post_init__(self) -> None:
        self.reset_all()

    def reset_all(self) -> None:
        self.model_skill = {k: 0.0 for k in MODEL_BASE_WEIGHTS}
        self.mod_skill = {k: 0.0 for k in MODS_MAIN}
        self.pending_case = None

        self.prediction_count = 0
        self.feedback_count = 0
        self.streak_loss = 0
        self.streak_win = 0
        self.last_hash = None
        self.last_feedback = None

    def model_weight(self, model_name: str) -> float:
        base = MODEL_BASE_WEIGHTS.get(model_name, 1.0)
        skill = self.model_skill.get(model_name, 0.0)
        factor = 1.0 + (skill * 0.03)
        factor = clamp(factor, 0.97, 1.03)
        return base * factor

    def mod_weight(self, m: int) -> float:
        base = BASE_MOD_WEIGHTS.get(m, 1.0)
        skill = self.mod_skill.get(m, 0.0)
        factor = 1.0 + (skill * 0.03)
        factor = clamp(factor, 0.97, 1.03)
        return base * factor

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

    # ===== Individual models =====
    def model_01_baseline_sum16(self, h: str) -> str:
        total = sum(int(c, 16) for c in h)
        score = (total % 16) + 3
        return "TÀI" if score >= 11 else "XỈU"

    def model_02_full_mod(self, h: str) -> str:
        v = hex_to_int(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_03_prime_mod(self, h: str) -> str:
        v = hex_to_int(h)
        return self.vote_mod(v, PRIME_MODS, bias=0.50)

    def model_04_head_mod(self, h: str) -> str:
        head, _, _ = slice_hex(h)
        v = int(head, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_05_mid_mod(self, h: str) -> str:
        _, mid, _ = slice_hex(h)
        v = int(mid, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_06_tail_mod(self, h: str) -> str:
        _, _, tail = slice_hex(h)
        v = int(tail, 16)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_07_slice_consensus(self, h: str) -> str:
        votes = [
            self.model_04_head_mod(h),
            self.model_05_mid_mod(h),
            self.model_06_tail_mod(h),
        ]
        tai = votes.count("TÀI")
        xiu = votes.count("XỈU")
        return "TÀI" if tai >= xiu else "XỈU"

    def model_08_xor_mix(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = v ^ (v >> 7) ^ (v << 11)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_09_power_mod(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = (v * v) ^ (v >> 17) ^ (v << 9)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_10_dice3(self, h: str) -> str:
        _, _, _, total = dice3_from_hash(h)
        return "TÀI" if total >= 11 else "XỈU"

    def model_11_position_weight(self, h: str) -> str:
        v = weighted_position_value(h)
        return "TÀI" if (v % 16) >= 8 else "XỈU"

    def model_12_rolling_chunk(self, h: str) -> str:
        v = chunk_xor_value(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_13_reverse_parity(self, h: str) -> str:
        r = h[::-1]
        v = int(r, 16)
        return "TÀI" if (v % 16) >= 8 else "XỈU"

    def model_14_bit_count_parity(self, h: str) -> str:
        bits = bin(int(h, 16))[2:]
        ones = bits.count("1")
        return "TÀI" if (ones % 2) == 0 else "XỈU"

    def model_15_pair_diff(self, h: str) -> str:
        vals = nibble_values(h)
        diff = 0
        for i in range(len(vals) - 1):
            diff += abs(vals[i] - vals[i + 1])
        return "TÀI" if (diff % 16) >= 8 else "XỈU"

    def model_16_alternating_sum(self, h: str) -> str:
        vals = nibble_values(h)
        alt = 0
        for i, v in enumerate(vals):
            alt += v if i % 2 == 0 else -v
        alt = abs(alt)
        return "TÀI" if (alt % 16) >= 8 else "XỈU"

    def model_17_chunk_sum(self, h: str) -> str:
        parts = hex_chunks(h, max(2, len(h) // 4))
        s = sum(int(p, 16) for p in parts)
        return "TÀI" if (s % 16) >= 8 else "XỈU"

    def model_18_entropy_balance(self, h: str) -> str:
        freq = Counter(h)
        score = sum(v * v for v in freq.values())
        return "TÀI" if (score % 2) == 0 else "XỈU"

    def model_19_edge_balance(self, h: str) -> str:
        head, _, tail = slice_hex(h)
        left = int(head, 16)
        right = int(tail, 16)
        return "TÀI" if ((left ^ right) % 16) >= 8 else "XỈU"

    def model_20_ascii_mix(self, h: str) -> str:
        total = sum(ord(c) for c in h)
        return "TÀI" if (total % 2) == 0 else "XỈU"

    def model_21_mirror_balance(self, h: str) -> str:
        half = len(h) // 2
        left = h[:half]
        right = h[-half:][::-1]
        lv = int(left or "0", 16)
        rv = int(right or "0", 16)
        return "TÀI" if ((lv + rv) % 16) >= 8 else "XỈU"

    def model_22_window_mix(self, h: str) -> str:
        size = max(2, len(h) // 5)
        parts = hex_chunks(h, size)
        score = 0
        for p in parts:
            score += int(p, 16)
        return "TÀI" if (score % 16) >= 8 else "XỈU"

    def model_23_zigzag_balance(self, h: str) -> str:
        vals = nibble_values(h)
        even_sum = sum(vals[::2])
        odd_sum = sum(vals[1::2])
        z = abs(even_sum - odd_sum)
        return "TÀI" if (z % 16) >= 8 else "XỈU"

    def predict(self, h: str) -> Tuple[str, int, int, Dict[str, str]]:
        h = norm_hex(h)

        models: List[Tuple[str, Callable[[str], str]]] = [
            ("baseline_sum16", self.model_01_baseline_sum16),
            ("full_mod", self.model_02_full_mod),
            ("prime_mod", self.model_03_prime_mod),
            ("head_mod", self.model_04_head_mod),
            ("mid_mod", self.model_05_mid_mod),
            ("tail_mod", self.model_06_tail_mod),
            ("slice_consensus", self.model_07_slice_consensus),
            ("xor_mix", self.model_08_xor_mix),
            ("power_mod", self.model_09_power_mod),
            ("dice3", self.model_10_dice3),
            ("position_weight", self.model_11_position_weight),
            ("rolling_chunk", self.model_12_rolling_chunk),
            ("reverse_parity", self.model_13_reverse_parity),
            ("bit_count_parity", self.model_14_bit_count_parity),
            ("pair_diff", self.model_15_pair_diff),
            ("alternating_sum", self.model_16_alternating_sum),
            ("chunk_sum", self.model_17_chunk_sum),
            ("entropy_balance", self.model_18_entropy_balance),
            ("edge_balance", self.model_19_edge_balance),
            ("ascii_mix", self.model_20_ascii_mix),
            ("mirror_balance", self.model_21_mirror_balance),
            ("window_mix", self.model_22_window_mix),
            ("zigzag_balance", self.model_23_zigzag_balance),
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
        confidence = max(50, min(99, confidence))

        score = int(round((tai_weight - xiu_weight) * 10))

        self.prediction_count += 1
        self.last_hash = h

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
            new_skill = (self.model_skill.get(name, 0.0) * 0.985) + (delta * 0.015)
            self.model_skill[name] = clamp(new_skill, -1.0, 1.0)
            if pred == actual:
                model_correct += 1

        v = int(case.hash_text, 16)
        for m in MODS_MAIN:
            pred_m = classify_by_mod_value(v, m, bias=0.50)
            delta = 1.0 if pred_m == actual else -1.0
            new_skill = (self.mod_skill.get(m, 0.0) * 0.985) + (delta * 0.015)
            self.mod_skill[m] = clamp(new_skill, -1.0, 1.0)

        self.feedback_count += 1
        self.last_feedback = actual

        if case.final_pred == actual:
            self.streak_win += 1
            self.streak_loss = 0
        else:
            self.streak_loss += 1
            self.streak_win = 0

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
        top_models = sorted(self.model_skill.items(), key=lambda x: x[1], reverse=True)[:5]
        top_mods = sorted(self.mod_skill.items(), key=lambda x: x[1], reverse=True)[:5]

        lines = ["Trạng thái hiện tại:"]
        lines.append("Mô hình mạnh nhất: " + ", ".join(f"{n}({v:+.2f})" for n, v in top_models))
        lines.append("Mod mạnh nhất: " + ", ".join(f"{m}({v:+.2f})" for m, v in top_mods))
        lines.append(f"Số dự đoán: {self.prediction_count}")
        lines.append(f"Số feedback: {self.feedback_count}")
        lines.append(f"Chuỗi đúng: {self.streak_win}")
        lines.append(f"Chuỗi sai: {self.streak_loss}")

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

    await update.message.reply_text(format_start_message(), parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "📖 <b>HƯỚNG DẪN</b>\n"
        "━━━━━━━━━━━━━━\n"
        "1) Gửi hash hex\n"
        "2) Nhận kết quả TÀI/XỈU + %\n"
        "3) Gửi kết quả thật là TÀI hoặc XỈU để bot tự ghi nhận\n"
        "4) /reset để xóa sạch toàn bộ trạng thái",
        parse_mode="HTML",
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    async with LOCK:
        await update.message.reply_text(
            format_status_message(brain.status_text()),
            parse_mode="HTML",
        )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    async with LOCK:
        brain.reset_all()

    await update.message.reply_text(
        "📦 <b>Đã reset sạch toàn bộ trạng thái.</b>\n"
        "Bot đã quay về như mới.",
        parse_mode="HTML",
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    async with LOCK:
        actual = parse_result_label(text)
        if actual and brain.pending_case is not None:
            info = brain.learn_from_feedback(actual)
            if info is None:
                await update.message.reply_text(
                    "⚠️ <b>Chưa có ca dự đoán nào để học.</b>",
                    parse_mode="HTML",
                )
                return

            await update.message.reply_text(
                format_feedback_message(
                    actual=actual,
                    final_pred=info["final_pred"],
                    confidence=info["confidence"],
                    model_correct=info["model_correct"],
                    model_total=info["model_total"],
                ),
                parse_mode="HTML",
            )
            return

        h = extract_hex(text)
        if h:
            if len(h) < 8:
                await update.message.reply_text(
                    "⚠️ <b>Chuỗi hex quá ngắn.</b>",
                    parse_mode="HTML",
                )
                return

            result, confidence, score, _preds = brain.predict(h)

            await update.message.reply_text(
                format_prediction_message(result, confidence, score, h),
                parse_mode="HTML",
            )
            return

        await update.message.reply_text(
            "❌ <b>Không thấy hash hex hợp lệ.</b>\n"
            "Gửi hash hoặc gửi <b>TÀI</b>/<b>XỈU</b> để feedback.",
            parse_mode="HTML",
        )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("hardreset", reset_cmd))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle))

    print("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
