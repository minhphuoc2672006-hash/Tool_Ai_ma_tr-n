#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import html
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
    return int(h, 16) if h else 0


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
    return [int(c, 16) for c in h] if h else []


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
    if n == 0:
        return 0
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

    d1 = (int(a, 16) % 6) + 1 if a else 1
    d2 = (int(b, 16) % 6) + 1 if b else 1
    d3 = (int(c, 16) % 6) + 1 if c else 1
    total = d1 + d2 + d3
    return d1, d2, d3, total


def reverse_hex_int(h: str) -> int:
    return int(h[::-1], 16) if h else 0


def interleave_halves(h: str) -> str:
    if len(h) <= 1:
        return h
    mid = len(h) // 2
    left = h[:mid]
    right = h[mid:]
    out = []
    for i in range(max(len(left), len(right))):
        if i < len(left):
            out.append(left[i])
        if i < len(right):
            out.append(right[i])
    return "".join(out)


def entropy_score(h: str) -> float:
    vals = nibble_values(h)
    if not vals:
        return 0.0
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    n = len(vals)
    ent = 0.0
    for c in counts.values():
        p = c / n
        ent -= p * (0 if p <= 0 else (p).bit_length() if False else __import__("math").log2(p))
    return ent


# =========================
# MODEL CONFIG
# =========================
MODS_MAIN = [5, 7, 11, 13, 17, 19, 23, 29]
PRIME_MODS = [5, 7, 11, 13, 17, 19, 23, 29]

BASE_MOD_WEIGHTS: Dict[int, float] = {
    5: 1.12,
    7: 1.18,
    11: 1.28,
    13: 1.28,
    17: 1.34,
    19: 1.38,
    23: 1.48,
    29: 1.58,
}

MODEL_BASE_WEIGHTS: Dict[str, float] = {
    "baseline_sum16": 1.00,
    "full_mod": 1.45,
    "prime_mod": 1.28,
    "head_mod": 1.10,
    "tail_mod": 1.10,
    "slice_consensus": 1.22,
    "xor_mix": 1.25,
    "power_mod": 1.18,
    "dice3": 1.18,
    "rolling_chunk": 1.12,

    "mid_mod": 1.10,
    "quarter_consensus": 1.16,
    "byte_sum_mod": 1.08,
    "reverse_mod": 1.12,
    "alternating_mod": 1.14,
    "mirrored_consensus": 1.16,
    "chunk_majority": 1.20,
    "weighted_position_mod": 1.22,
    "prime_position_mod": 1.18,
    "fib_position_mod": 1.18,
    "nibble_parity_mod": 1.10,
    "entropy_bias_mod": 1.05,
    "pairwise_xor_mod": 1.14,
    "dual_half_consensus": 1.16,
    "center_tail_head": 1.12,
    "repeat_pattern": 1.06,
    "delta_mod": 1.10,
    "alt_reverse_mod": 1.12,
    "double_mix_mod": 1.14,
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
        factor = 1.0 + (skill * 0.02)
        factor = clamp(factor, 0.96, 1.04)
        return base * factor

    def mod_weight(self, m: int) -> float:
        base = BASE_MOD_WEIGHTS.get(m, 1.0)
        skill = self.mod_skill.get(m, 0.0)
        factor = 1.0 + (skill * 0.02)
        factor = clamp(factor, 0.96, 1.04)
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

    def vote_texts(self, preds: List[str]) -> str:
        tai = preds.count("TÀI")
        xiu = preds.count("XỈU")
        return "TÀI" if tai >= xiu else "XỈU"

    # ===== Core models =====
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
        v = int(head, 16) if head else 0
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_05_tail_mod(self, h: str) -> str:
        _, _, tail = slice_hex(h)
        v = int(tail, 16) if tail else 0
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_06_slice_consensus(self, h: str) -> str:
        votes = [
            self.model_04_head_mod(h),
            self.model_05_tail_mod(h),
            self.model_03_prime_mod(h),
        ]
        return self.vote_texts(votes)

    def model_07_xor_mix(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = v ^ (v >> 7) ^ (v << 11)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_08_power_mod(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = (v * v) ^ (v >> 17) ^ (v << 9)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_09_dice3(self, h: str) -> str:
        _, _, _, total = dice3_from_hash(h)
        return "TÀI" if total >= 11 else "XỈU"

    def model_10_rolling_chunk(self, h: str) -> str:
        v = chunk_xor_value(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    # ===== Expanded heuristic models =====
    def model_11_mid_mod(self, h: str) -> str:
        _, mid, _ = slice_hex(h)
        v = int(mid, 16) if mid else 0
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_12_quarter_consensus(self, h: str) -> str:
        n = len(h)
        if n < 4:
            return self.model_02_full_mod(h)
        q = max(1, n // 4)
        parts = [h[i:i + q] for i in range(0, n, q)]
        votes = []
        for p in parts[:4]:
            if p:
                votes.append(self.vote_mod(int(p, 16), MODS_MAIN, bias=0.50))
        return self.vote_texts(votes or [self.model_02_full_mod(h)])

    def model_13_byte_sum_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        total = sum(vals)
        return self.vote_mod(total, MODS_MAIN, bias=0.50)

    def model_14_reverse_mod(self, h: str) -> str:
        v = reverse_hex_int(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_15_alternating_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        a = sum(vals[::2])
        b = sum(vals[1::2])
        mixed = (a * 31) ^ (b * 17) ^ len(vals)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_16_mirrored_consensus(self, h: str) -> str:
        if not h:
            return "XỈU"
        rev = h[::-1]
        votes = [
            self.vote_mod(int(h[:max(1, len(h)//3)] or "0", 16), MODS_MAIN, bias=0.50),
            self.vote_mod(int(rev[:max(1, len(rev)//3)] or "0", 16), MODS_MAIN, bias=0.50),
        ]
        return self.vote_texts(votes)

    def model_17_chunk_majority(self, h: str) -> str:
        chunks = hex_chunks(h, max(2, len(h) // 6 or 2))
        votes = []
        for c in chunks[:8]:
            votes.append(self.vote_mod(int(c, 16), MODS_MAIN, bias=0.50))
        return self.vote_texts(votes or [self.model_02_full_mod(h)])

    def model_18_weighted_position_mod(self, h: str) -> str:
        v = weighted_position_value(h)
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_19_prime_position_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        prime_idx = [1, 2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
        total = 0
        for i, val in enumerate(vals, start=1):
            if i in prime_idx:
                total += val * 2
            else:
                total += val
        return self.vote_mod(total, MODS_MAIN, bias=0.50)

    def model_20_fib_position_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        fib = {1, 2, 3, 5, 8, 13, 21, 34}
        total = 0
        for i, val in enumerate(vals, start=1):
            total += val * (3 if i in fib else 1)
        return self.vote_mod(total, MODS_MAIN, bias=0.50)

    def model_21_nibble_parity_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        even = sum(1 for v in vals if v % 2 == 0)
        odd = len(vals) - even
        mixed = (even * 97) ^ (odd * 53) ^ len(vals)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_22_entropy_bias_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        uniq = len(set(vals))
        total = sum(vals)
        mixed = total + (uniq * 17) + (len(vals) * 3)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_23_pairwise_xor_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if len(vals) < 2:
            return self.model_13_byte_sum_mod(h)
        x = 0
        for i in range(0, len(vals) - 1, 2):
            x ^= ((vals[i] << 4) | vals[i + 1])
        return self.vote_mod(x, MODS_MAIN, bias=0.50)

    def model_24_dual_half_consensus(self, h: str) -> str:
        if not h:
            return "XỈU"
        mid = len(h) // 2
        left = h[:mid]
        right = h[mid:]
        votes = [
            self.vote_mod(int(left or "0", 16), MODS_MAIN, bias=0.50),
            self.vote_mod(int(right or "0", 16), MODS_MAIN, bias=0.50),
        ]
        return self.vote_texts(votes)

    def model_25_center_tail_head(self, h: str) -> str:
        head, mid, tail = slice_hex(h)
        votes = [
            self.vote_mod(int(head or "0", 16), MODS_MAIN, bias=0.50),
            self.vote_mod(int(mid or "0", 16), MODS_MAIN, bias=0.50),
            self.vote_mod(int(tail or "0", 16), MODS_MAIN, bias=0.50),
        ]
        return self.vote_texts(votes)

    def model_26_repeat_pattern(self, h: str) -> str:
        if not h:
            return "XỈU"
        # so xem chuỗi có nhịp lặp nào nổi bật
        chunks = [h[i:i + 2] for i in range(0, len(h), 2)]
        counts: Dict[str, int] = {}
        for c in chunks:
            counts[c] = counts.get(c, 0) + 1
        top = max(counts.values()) if counts else 0
        mixed = (top * 31) + len(counts) + len(chunks)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_27_delta_mod(self, h: str) -> str:
        vals = nibble_values(h)
        if len(vals) < 2:
            return self.model_13_byte_sum_mod(h)
        delta = 0
        for i in range(1, len(vals)):
            delta += abs(vals[i] - vals[i - 1])
        return self.vote_mod(delta, MODS_MAIN, bias=0.50)

    def model_28_alt_reverse_mod(self, h: str) -> str:
        inter = interleave_halves(h)
        v = int(inter[::-1], 16) if inter else 0
        return self.vote_mod(v, MODS_MAIN, bias=0.50)

    def model_29_double_mix_mod(self, h: str) -> str:
        v = hex_to_int(h)
        mixed = (v ^ (v >> 11) ^ (v << 5)) + (v ^ (v >> 3))
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def model_30_weighted_entropy_vote(self, h: str) -> str:
        vals = nibble_values(h)
        if not vals:
            return "XỈU"
        uniq = len(set(vals))
        total = sum(vals)
        mixed = (total * 3) + (uniq * 11) + (len(vals) * 7)
        return self.vote_mod(mixed, MODS_MAIN, bias=0.50)

    def predict(self, h: str) -> Tuple[str, int, int, Dict[str, str]]:
        h = norm_hex(h)

        models: List[Tuple[str, Callable[[str], str]]] = [
            ("baseline_sum16", self.model_01_baseline_sum16),
            ("full_mod", self.model_02_full_mod),
            ("prime_mod", self.model_03_prime_mod),
            ("head_mod", self.model_04_head_mod),
            ("tail_mod", self.model_05_tail_mod),
            ("slice_consensus", self.model_06_slice_consensus),
            ("xor_mix", self.model_07_xor_mix),
            ("power_mod", self.model_08_power_mod),
            ("dice3", self.model_09_dice3),
            ("rolling_chunk", self.model_10_rolling_chunk),

            ("mid_mod", self.model_11_mid_mod),
            ("quarter_consensus", self.model_12_quarter_consensus),
            ("byte_sum_mod", self.model_13_byte_sum_mod),
            ("reverse_mod", self.model_14_reverse_mod),
            ("alternating_mod", self.model_15_alternating_mod),
            ("mirrored_consensus", self.model_16_mirrored_consensus),
            ("chunk_majority", self.model_17_chunk_majority),
            ("weighted_position_mod", self.model_18_weighted_position_mod),
            ("prime_position_mod", self.model_19_prime_position_mod),
            ("fib_position_mod", self.model_20_fib_position_mod),
            ("nibble_parity_mod", self.model_21_nibble_parity_mod),
            ("entropy_bias_mod", self.model_22_entropy_bias_mod),
            ("pairwise_xor_mod", self.model_23_pairwise_xor_mod),
            ("dual_half_consensus", self.model_24_dual_half_consensus),
            ("center_tail_head", self.model_25_center_tail_head),
            ("repeat_pattern", self.model_26_repeat_pattern),
            ("delta_mod", self.model_27_delta_mod),
            ("alt_reverse_mod", self.model_28_alt_reverse_mod),
            ("double_mix_mod", self.model_29_double_mix_mod),
            ("weighted_entropy_vote", self.model_30_weighted_entropy_vote),
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

        v = int(case.hash_text, 16) if case.hash_text else 0
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
        top_models = sorted(self.model_skill.items(), key=lambda x: x[1], reverse=True)[:7]
        top_mods = sorted(self.mod_skill.items(), key=lambda x: x[1], reverse=True)[:7]

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
