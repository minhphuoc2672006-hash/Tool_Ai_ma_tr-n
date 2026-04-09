#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot thống kê Tài/Xỉu theo lịch sử, có 2 lớp:
- History: lịch sử nhập vào, có thể reset riêng
- Brain: bộ nhớ pattern ổn định/nhiễu, giữ lại khi reset history

Bản này:
- Ưu tiên nhận diện pattern và biến thể pattern
- Không dùng xu hướng nền / xu hướng gần
- Trả kết quả ngay
- Reset history không đụng brain
- Reset all mới xóa brain
- Có /report để thống kê nhận diện cầu và brain
- Có fallback nhẹ để không bị "im" khi không bắt được cầu rõ
- Không xuất kèo dự đoán
"""

import os
import re
import sqlite3
import logging
from collections import Counter
from typing import List, Optional, Tuple, Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("TAI_XIU_DB_PATH", "tai_xiu_stats.db")
MIN_HISTORY_FOR_FALLBACK = 15
MAX_INPUT_ITEMS = 100

_admin_raw = os.getenv("ADMIN_USER_ID", "").strip()
try:
    ADMIN_USER_ID = int(_admin_raw) if _admin_raw else 0
except ValueError:
    ADMIN_USER_ID = 0

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tai_xiu_bot")


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def deny_if_not_admin(update: Update):
    if update.message:
        await update.message.reply_text("Bot này chỉ dành cho ADMIN.")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                raw TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('T', 'X'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pattern_memory (
                pattern_key TEXT PRIMARY KEY,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                recent_losses INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def save_outcomes(outcomes: List[str], raw: str) -> int:
    if not outcomes:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO rounds(raw, outcome) VALUES(?, ?)",
            [(raw, o) for o in outcomes],
        )
        conn.commit()
    return len(outcomes)


def reset_history_only():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.commit()


def reset_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.execute("DELETE FROM pattern_memory")
        conn.commit()


def load_history(limit: int = 200) -> List[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT outcome FROM rounds ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["outcome"] for r in reversed(rows)]


def load_rows(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, raw, outcome FROM rounds ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return list(reversed(rows))


def count_saved_rounds() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM rounds").fetchone()
    return int(row["c"]) if row else 0


def reverse_outcome(v: str) -> str:
    return "T" if v == "X" else "X"


def pattern_family(label: str) -> str:
    t = (label or "").strip()
    if t.startswith("Cầu bệt vừa"):
        return "BET_SOFT"
    if t.startswith("Cầu bệt"):
        return "BET"
    if t.startswith("Cầu đảo 1-1"):
        return "DAO_1_1"
    if t.startswith("Cầu chu kỳ đảo"):
        return "CHU_KY_DAO"
    if t.startswith("Cầu chu kỳ"):
        return "CHU_KY"
    if t.startswith("Cầu gần chu kỳ đảo"):
        return "GAN_CHU_KY_DAO"
    if t.startswith("Cầu gần chu kỳ"):
        return "GAN_CHU_KY"
    if t.startswith("Cầu tăng tiến"):
        return "TANG_TIEN"
    if t.startswith("Cầu giảm tiến"):
        return "GIAM_TIEN"
    if t.startswith("Cầu đối xứng"):
        return "DOI_XUNG"
    if t.startswith("Cầu luân phiên"):
        return "LUAN_PHIEN"
    if t.startswith("Cầu nhịp đều"):
        return "NHIP_DEU"
    if t.startswith("Cầu dập"):
        return "DAP"
    if t.startswith("Cầu gãy giả"):
        return "FAKE_BREAK"
    if t.startswith("Cầu hồi"):
        return "REBOUND"
    if t.startswith("Cầu kéo dài yếu"):
        return "WEAK_STREAK"
    if t.startswith("Cầu zigzag"):
        return "ZIGZAG"
    if t.startswith("Cầu hỗn hợp biến thể"):
        return "HON_HOP_BIEN_THE"
    if t.startswith("Cầu hỗn hợp"):
        return "HON_HOP"
    if t.startswith("Bẻ cầu"):
        return "BE_CAU"
    if t.startswith("Chuyển"):
        return "CHUYEN_CAU"
    return "OTHER"


def get_pattern_memory(pattern_key: str) -> Optional[Dict[str, int]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT wins, losses, recent_losses
            FROM pattern_memory
            WHERE pattern_key = ?
            """,
            (pattern_key,),
        ).fetchone()
    if not row:
        return None
    return {
        "wins": int(row["wins"]),
        "losses": int(row["losses"]),
        "recent_losses": int(row["recent_losses"]),
    }


def update_pattern_memory_in_conn(conn: sqlite3.Connection, pattern_key: str, strong: bool):
    row = conn.execute(
        """
        SELECT wins, losses, recent_losses
        FROM pattern_memory
        WHERE pattern_key = ?
        """,
        (pattern_key,),
    ).fetchone()

    if row:
        wins = int(row["wins"])
        losses = int(row["losses"])
        recent_losses = int(row["recent_losses"])
    else:
        wins = 0
        losses = 0
        recent_losses = 0

    if strong:
        wins += 1
        recent_losses = 0
    else:
        losses += 1
        recent_losses += 1

    conn.execute(
        """
        INSERT INTO pattern_memory(pattern_key, wins, losses, recent_losses, updated_at)
        VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(pattern_key) DO UPDATE SET
            wins = excluded.wins,
            losses = excluded.losses,
            recent_losses = excluded.recent_losses,
            updated_at = CURRENT_TIMESTAMP
        """,
        (pattern_key, wins, losses, recent_losses),
    )


def get_brain_rows(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pattern_key, wins, losses, recent_losses, updated_at
            FROM pattern_memory
            ORDER BY (wins + losses) DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows


def count_brain_patterns() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM pattern_memory").fetchone()
    return int(row["c"]) if row else 0


def get_brain_totals() -> Tuple[int, int]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(wins), 0) AS strong_hits,
                COALESCE(SUM(losses), 0) AS weak_hits
            FROM pattern_memory
            """
        ).fetchone()
    strong_hits = int(row["strong_hits"]) if row else 0
    weak_hits = int(row["weak_hits"]) if row else 0
    return strong_hits, weak_hits


TOKEN_RE = re.compile(r"\b(?:TÀI|TAI|XỈU|XIU|T|X|\d+)\b", re.UNICODE)


def normalize_token(token: str) -> Optional[str]:
    t = token.strip().upper()
    if t in {"T", "TAI", "TÀI"}:
        return "T"
    if t in {"X", "XIU", "XỈU"}:
        return "X"
    if t.isdigit() and 1 <= len(t) <= 2:
        n = int(t)
        if 3 <= n <= 10:
            return "X"
        if 11 <= n <= 18:
            return "T"
        return None
    return None


def extract_outcomes(text: str) -> List[str]:
    if not text:
        return []
    tokens = TOKEN_RE.findall(text.upper())
    results = []
    for tok in tokens:
        mapped = normalize_token(tok)
        if mapped in {"T", "X"}:
            results.append(mapped)
    return results


def fmt_outcome(v: str) -> str:
    return "Tài" if v == "T" else "Xỉu"


def rle(seq: List[str]) -> List[Tuple[str, int]]:
    if not seq:
        return []
    out = []
    cur = seq[0]
    cnt = 1
    for x in seq[1:]:
        if x == cur:
            cnt += 1
        else:
            out.append((cur, cnt))
            cur = x
            cnt = 1
    out.append((cur, cnt))
    return out


def current_streak(seq: List[str]) -> Tuple[str, int]:
    if not seq:
        return ("", 0)
    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return (last, count)


def detect_soft_streak(seq: List[str]) -> Optional[int]:
    if len(seq) < 3:
        return None
    count = 1
    last = seq[-1]
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return count if 3 <= count < 5 else None


def detect_alternating_pro(seq: List[str]) -> Optional[int]:
    if len(seq) < 6:
        return None
    tail = seq[-10:]
    if len(tail) < 6:
        return None
    mismatches = sum(1 for i in range(len(tail) - 1) if tail[i] == tail[i + 1])
    noise_ratio = mismatches / max(1, len(tail) - 1)
    if noise_ratio <= 0.20:
        length = len(tail)
        i = len(seq) - len(tail) - 1
        while i >= 0 and seq[i] != seq[i + 1]:
            length += 1
            i -= 1
        return length
    return None


def detect_exact_periodic_pro(
    seq: List[str],
    min_period: int = 2,
    max_period: int = 6,
    min_repeats: int = 3,
):
    n = len(seq)
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(10, n // period) + 1):
            need = period * repeats
            if n < need:
                continue
            tail = seq[-need:]
            motif = tail[:period]
            if all(tail[i] == motif[i % period] for i in range(need)):
                return motif, repeats
    return None, 0


def detect_approx_periodic_pro(
    seq: List[str],
    min_period: int = 2,
    max_period: int = 6,
    min_repeats: int = 3,
    max_mismatches: int = 2,
):
    n = len(seq)
    best_motif = None
    best_repeats = 0
    best_score = -10**9
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(10, n // period) + 1):
            need = period * repeats
            if n < need:
                continue
            tail = seq[-need:]
            motif = tail[:period]
            mismatches = 0
            for i, x in enumerate(tail):
                if x != motif[i % period]:
                    mismatches += 1
            if mismatches <= max_mismatches:
                score = repeats * 10 - period * 2 - mismatches * 5
                if score > best_score:
                    best_score = score
                    best_motif = motif
                    best_repeats = repeats
    return best_motif, best_repeats


def detect_length_signature_pro(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None
    tail = [s[1] for s in segments[-8:]]
    if len(tail) >= 4 and all(tail[i] < tail[i + 1] for i in range(len(tail) - 1)):
        return f"Cầu tăng tiến {'-'.join(map(str, tail))}"
    if len(tail) >= 4 and all(tail[i] > tail[i + 1] for i in range(len(tail) - 1)):
        return f"Cầu giảm tiến {'-'.join(map(str, tail))}"
    if len(tail) >= 5 and tail == tail[::-1]:
        return f"Cầu đối xứng {'-'.join(map(str, tail))}"
    if len(tail) >= 4 and tail[0] == tail[2] and tail[1] == tail[3]:
        return f"Cầu luân phiên {'-'.join(map(str, tail))}"
    if len(set(tail)) == 1:
        return f"Cầu nhịp đều {'-'.join(map(str, tail))}"
    if len(set(tail)) >= 5:
        return f"Cầu hỗn hợp {'-'.join(map(str, tail))}"
    return None


def detect_mixed_pattern_pro(seq: List[str], segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(seq) < 10 or len(segments) < 5:
        return None
    tail_lengths = [s[1] for s in segments[-8:]]
    if len(tail_lengths) < 5:
        return None
    unique_count = len(set(tail_lengths))
    variance = max(tail_lengths) - min(tail_lengths)
    if unique_count >= 3 and variance >= 2:
        return f"Cầu hỗn hợp biến thể {'-'.join(map(str, tail_lengths))}"
    if unique_count >= 2:
        return f"Cầu hỗn hợp {'-'.join(map(str, tail_lengths))}"
    return None


def detect_break_pro(seq: List[str], segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(seq) < 5 or len(segments) < 2:
        return None
    prev_val, prev_len = segments[-2]
    cur_val, cur_len = segments[-1]
    if prev_len >= 5 and cur_len == 1:
        return f"Bẻ cầu yếu sau bệt {prev_val} x{prev_len}"
    if prev_len >= 5 and cur_len == 2:
        return f"Bẻ cầu mạnh sau bệt {prev_val} x{prev_len}"
    if prev_len >= 4 and cur_len == 1:
        return f"Bẻ cầu sau bệt {prev_val} x{prev_len}"
    return None


def detect_transition_pro(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None
    a, b, c = segments[-3:]
    if a[1] >= 4 and b[1] == 1 and c[1] == 1:
        return f"Chuyển bệt -> đảo ({a[0]} -> {c[0]})"
    if a[1] == 1 and b[1] == 1 and c[1] >= 3:
        return f"Chuyển đảo -> bệt ({a[0]} -> {c[0]})"
    if a[1] >= 4 and b[1] >= 2 and c[1] >= 2:
        return f"Chuyển bệt -> chu kỳ ({a[0]} -> {c[0]})"
    return None


def detect_wave_pattern(seq: List[str]) -> Optional[str]:
    if len(seq) < 8:
        return None
    segs = rle(seq)
    lens = [s[1] for s in segs[-6:]]
    if lens in ([2, 1, 2, 1, 2, 1], [1, 2, 1, 2, 1, 2]):
        return f"Cầu dập {'-'.join(map(str, lens))}"
    return None


def detect_fake_break(seq: List[str]) -> Optional[str]:
    if len(seq) < 6:
        return None
    segs = rle(seq)
    if len(segs) >= 3:
        a, b, c = segs[-3:]
        if a[1] >= 4 and b[1] == 1 and c[1] >= 2:
            return f"Cầu gãy giả {a[0]} x{a[1]}"
    return None


def detect_rebound(seq: List[str]) -> Optional[str]:
    if len(seq) < 6:
        return None
    if seq[-1] == seq[-3] and seq[-2] != seq[-1]:
        return "Cầu hồi"
    return None


def detect_weak_streak(seq: List[str]) -> Optional[str]:
    if len(seq) < 6:
        return None
    segs = rle(seq)
    if len(segs) >= 3:
        tail = [s[1] for s in segs[-4:]]
        if len(set(tail)) <= 2 and max(tail) - min(tail) <= 1:
            return f"Cầu kéo dài yếu {'-'.join(map(str, tail))}"
    return None


def detect_zigzag(seq: List[str]) -> Optional[str]:
    if len(seq) < 7:
        return None
    tail = seq[-7:]
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    if changes >= 4:
        return "Cầu zigzag"
    return None


def classify_pattern_pro(seq: List[str]) -> Dict[str, Any]:
    result = {
        "pattern_label": "Không nhận diện được cầu",
        "confidence": 0,
        "recognized": False,
        "source": "",
        "detail": "",
        "family": "OTHER",
    }

    if len(seq) < 5:
        return result

    window = seq[-60:]
    segments = rle(window)
    last_val, streak_len = current_streak(window)

    changes = sum(1 for i in range(1, len(window)) if window[i] != window[i - 1])
    noise_ratio = changes / max(1, len(window) - 1)

    candidates: List[Dict[str, Any]] = []

    def add_candidate(label: str, conf: int, source: str, detail: str):
        adjusted_conf = conf
        if noise_ratio > 0.60:
            adjusted_conf -= 25
        elif noise_ratio > 0.45:
            adjusted_conf -= 15
        elif noise_ratio > 0.30:
            adjusted_conf -= 8

        if len(window) < 8:
            adjusted_conf -= 8

        adjusted_conf = max(0, min(100, adjusted_conf))
        if adjusted_conf < 45:
            return

        candidates.append(
            {
                "pattern_label": label,
                "confidence": adjusted_conf,
                "recognized": True,
                "source": source,
                "detail": detail,
                "family": pattern_family(label),
            }
        )

    soft_streak = detect_soft_streak(window)
    if soft_streak and soft_streak >= 3:
        add_candidate(
            f"Cầu bệt vừa {last_val} x{soft_streak}",
            78 + min(4, soft_streak),
            "SOFT_STREAK",
            f"soft_streak={soft_streak}",
        )

    if streak_len >= 5:
        add_candidate(
            f"Cầu bệt {last_val} x{streak_len}",
            min(95, 80 + streak_len),
            "STREAK",
            f"streak_len={streak_len}",
        )

    alt_len = detect_alternating_pro(window)
    if alt_len and alt_len >= 6:
        add_candidate(
            f"Cầu đảo 1-1 x{alt_len}",
            max(72, 92 - int(noise_ratio * 100)),
            "ALTERNATING",
            f"noise_ratio={noise_ratio:.2f}",
        )

    motif, rep = detect_exact_periodic_pro(window, min_period=2, max_period=6, min_repeats=3)
    if motif:
        motif_text = "-".join(motif)
        if len(motif) == 2 and motif[0] != motif[1]:
            add_candidate(
                f"Cầu chu kỳ đảo {motif_text} x{rep}",
                84,
                "PERIODIC_FLIP",
                "exact_periodic_flip",
            )
        else:
            add_candidate(
                f"Cầu chu kỳ {motif_text} x{rep}",
                82,
                "PERIODIC",
                "exact_periodic",
            )

    approx_motif, approx_rep = detect_approx_periodic_pro(window, min_period=2, max_period=6, min_repeats=3, max_mismatches=2)
    if approx_motif:
        motif_text = "-".join(approx_motif)
        if len(approx_motif) == 2 and approx_motif[0] != approx_motif[1]:
            add_candidate(
                f"Cầu gần chu kỳ đảo {motif_text} x{approx_rep}",
                78,
                "NEAR_PERIODIC_FLIP",
                "approx_periodic_flip",
            )
        else:
            add_candidate(
                f"Cầu gần chu kỳ {motif_text} x{approx_rep}",
                76,
                "NEAR_PERIODIC",
                "approx_periodic",
            )

    length_sig = detect_length_signature_pro(segments)
    if length_sig:
        if "đối xứng" in length_sig:
            conf = 80
        elif "luân phiên" in length_sig:
            conf = 77
        elif "tăng tiến" in length_sig:
            conf = 72
        elif "giảm tiến" in length_sig:
            conf = 72
        elif "nhịp đều" in length_sig:
            conf = 74
        elif "hỗn hợp" in length_sig:
            conf = 66
        else:
            conf = 60
        add_candidate(length_sig, conf, "LENGTH_SIGNATURE", "length_signature")

    wave = detect_wave_pattern(window)
    if wave:
        add_candidate(wave, 75, "WAVE", "wave_pattern")

    fake_break = detect_fake_break(window)
    if fake_break:
        add_candidate(fake_break, 70, "FAKE_BREAK", "fake_break")

    rebound = detect_rebound(window)
    if rebound:
        add_candidate(rebound, 72, "REBOUND", "rebound")

    weak = detect_weak_streak(window)
    if weak:
        add_candidate(weak, 58, "WEAK_STREAK", "weak_streak")

    zigzag = detect_zigzag(window)
    if zigzag:
        add_candidate(zigzag, 61, "ZIGZAG", "zigzag")

    mixed = detect_mixed_pattern_pro(window, segments)
    if mixed:
        add_candidate(mixed, 63, "MIXED", "mixed_pattern")

    break_type = detect_break_pro(window, segments)
    if break_type:
        add_candidate(break_type, 73, "BREAK", "break_pattern")

    transition = detect_transition_pro(segments)
    if transition:
        add_candidate(transition, 70, "TRANSITION", "transition_pattern")

    if candidates:
        best = max(candidates, key=lambda x: x["confidence"])
        best["prediction"] = None
        best["flipped"] = False
        best["flip_reason"] = ""
        best["follow_pct"] = best["confidence"]
        best["reverse_pct"] = max(0, 100 - best["confidence"])
        return {**result, **best}

    if len(seq) >= MIN_HISTORY_FOR_FALLBACK:
        fallback_conf = max(45, 55 - int(noise_ratio * 20))
        return {
            "pattern_label": "Fallback nhẹ / nhiễu",
            "confidence": fallback_conf,
            "recognized": True,
            "prediction": None,
            "source": "FALLBACK",
            "detail": f"noise_ratio={noise_ratio:.2f}",
            "family": "OTHER",
            "flipped": False,
            "flip_reason": "",
            "follow_pct": fallback_conf,
            "reverse_pct": min(55, 100 - fallback_conf),
        }

    return result


def update_brain_from_analysis(analysis: Dict[str, Any]):
    if not analysis.get("recognized"):
        return
    label = analysis.get("pattern_label", "")
    if not label:
        return
    strong = int(analysis.get("confidence", 0)) >= 75
    key = pattern_family(label)
    with get_conn() as conn:
        update_pattern_memory_in_conn(conn, key, strong)
        conn.commit()


def format_pattern_summary(analysis: Dict[str, Any]) -> str:
    if not analysis["recognized"]:
        return "Cầu: Không nhận diện được cầu"
    return (
        f"Cầu: {analysis['pattern_label']}\n"
        f"Nhóm: {analysis['family']}\n"
        f"Nguồn: {analysis['source']}\n"
        f"Chi tiết: {analysis['detail']}\n"
        f"Độ khớp: {analysis['confidence']}%\n"
        f"Tỷ lệ khớp: {analysis['follow_pct']}% | Nhiễu: {analysis['reverse_pct']}%"
    )


def build_live_reply(
    inserted_count: int,
    total_saved: int,
    analysis: Dict[str, Any],
    brain_strong: int,
    brain_weak: int,
) -> str:
    return (
        f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}\n"
        f"Brain: Ổn định {brain_strong} | Nhiễu {brain_weak}\n"
        f"{format_pattern_summary(analysis)}"
    )


def build_import_reply(
    inserted_count: int,
    total_saved: int,
    analysis: Dict[str, Any],
) -> str:
    return (
        f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}\n"
        f"{format_pattern_summary(analysis)}"
    )


def build_report_reply(seq: List[str], analysis: Dict[str, Any]) -> str:
    count = Counter(seq)
    total = len(seq)

    brain_rows = get_brain_rows(8)
    brain_lines = []
    if brain_rows:
        for r in brain_rows:
            total_p = int(r["wins"]) + int(r["losses"])
            stable_rate = (int(r["wins"]) / total_p * 100) if total_p else 0.0
            brain_lines.append(
                f"- {r['pattern_key']} | Ổn định:{r['wins']} Nhiễu:{r['losses']} | "
                f"LR:{r['recent_losses']} | {stable_rate:.1f}%"
            )
    else:
        brain_lines.append("- Chưa có dữ liệu brain.")

    return (
        f"Thống kê tổng: {total} mẫu\n"
        f"- Tài: {count['T']}\n"
        f"- Xỉu: {count['X']}\n"
        f"\nCầu hiện tại:\n"
        f"{format_pattern_summary(analysis)}\n"
        f"\nNão (top pattern):\n"
        + "\n".join(brain_lines)
    )


WELCOME = (
    "Bot thống kê Tài/Xỉu đã sẵn sàng.\n\n"
    "Lệnh dùng:\n"
    "/add <dữ liệu>          - thêm 1 hoặc nhiều kết quả (live)\n"
    "/import <dữ liệu>       - dán lịch sử dài (chỉ lưu)\n"
    "/history [n]            - xem n kết quả gần nhất\n"
    "/stats [n]              - thống kê tần suất\n"
    "/scan [n]               - phân tích lịch sử\n"
    "/patterns [n]           - xem nhận diện cầu\n"
    "/brain [n]              - xem bộ nhớ não\n"
    "/report [n]             - thống kê nhận diện cầu\n"
    "/clear, /reset_history  - xóa lịch sử\n"
    "/reset_all              - xóa toàn bộ, gồm cả não\n\n"
    "Chỉ ADMIN mới dùng được."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if update.message:
        await update.message.reply_text(WELCOME)


async def process_live_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        if not update.message:
            return

        items = extract_outcomes(text)
        if not items:
            await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
            return

        if len(items) > MAX_INPUT_ITEMS:
            await update.message.reply_text(f"Dữ liệu quá nhiều. Tối đa {MAX_INPUT_ITEMS} kết quả mỗi lần.")
            return

        inserted = save_outcomes(items, text)
        seq = load_history(200)
        analysis = classify_pattern_pro(seq)
        update_brain_from_analysis(analysis)
        total_saved = count_saved_rounds()
        brain_strong, brain_weak = get_brain_totals()

        reply = build_live_reply(
            inserted_count=inserted,
            total_saved=total_saved,
            analysis=analysis,
            brain_strong=brain_strong,
            brain_weak=brain_weak,
        )
        await update.message.reply_text(reply)

    except Exception as e:
        logger.exception("Error in process_live_input")
        if update.message:
            await update.message.reply_text(f"Bị lỗi khi xử lý: {e}")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Dùng: /add T X T 11 8 14")
        return
    await process_live_input(update, context, text)


async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Dùng: /import T X X T 11 8 14 6 ...")
        return

    items = extract_outcomes(text)
    if not items:
        await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
        return

    if len(items) > MAX_INPUT_ITEMS:
        await update.message.reply_text(f"Dữ liệu quá nhiều. Tối đa {MAX_INPUT_ITEMS} kết quả mỗi lần.")
        return

    inserted = save_outcomes(items, text)
    seq = load_history(200)
    analysis = classify_pattern_pro(seq)
    update_brain_from_analysis(analysis)
    total_saved = count_saved_rounds()

    reply = build_import_reply(
        inserted_count=inserted,
        total_saved=total_saved,
        analysis=analysis,
    )
    await update.message.reply_text(reply)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 20
    if context.args and context.args[0].isdigit():
        n = max(1, min(200, int(context.args[0])))

    rows = load_rows(n)
    if not rows:
        await update.message.reply_text("Chưa có lịch sử.")
        return

    lines = ["Lịch sử gần nhất:"]
    for r in rows:
        lines.append(f"#{r['id']} | {r['outcome']} | {r['created_at']}")
    await update.message.reply_text("\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(1, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    count = Counter(seq)
    total = len(seq)
    msg = (
        f"Thống kê {total} mẫu gần nhất:\n"
        f"- Tài: {count['T']} ({count['T'] / total * 100:.1f}%)\n"
        f"- Xỉu: {count['X']} ({count['X'] / total * 100:.1f}%)"
    )
    await update.message.reply_text(msg)


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(1, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu để quét.")
        return

    analysis = classify_pattern_pro(seq)
    msg = format_pattern_summary(analysis)
    await update.message.reply_text(msg)


async def patterns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 160
    if context.args and context.args[0].isdigit():
        n = max(20, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    analysis = classify_pattern_pro(seq)
    count = Counter(seq)
    segs = rle(seq[-n:])
    seg_text = " ".join(f"{v}{k}" for v, k in segs[-12:])

    reply = (
        f"{format_pattern_summary(analysis)}\n"
        f"Chuỗi segment: {seg_text}\n"
        f"Tài: {count['T']} | Xỉu: {count['X']}"
    )
    await update.message.reply_text(reply)


async def brain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 10
    if context.args and context.args[0].isdigit():
        n = max(1, min(50, int(context.args[0])))

    rows = get_brain_rows(n)
    if not rows:
        await update.message.reply_text("Não chưa có dữ liệu.")
        return

    strong_total, weak_total = get_brain_totals()

    lines = [
        f"Bộ nhớ não: {count_brain_patterns()} pattern",
        f"Tổng ổn định: {strong_total} | Tổng nhiễu: {weak_total}",
        "Top pattern:",
    ]
    for r in rows:
        total = int(r["wins"]) + int(r["losses"])
        stable_rate = (int(r["wins"]) / total * 100) if total else 0.0
        lines.append(
            f"- {r['pattern_key']} | Ổn định:{r['wins']} Nhiễu:{r['losses']} "
            f"| LoseStreak:{r['recent_losses']} | Rate:{stable_rate:.1f}%"
        )

    await update.message.reply_text("\n".join(lines))


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(20, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    analysis = classify_pattern_pro(seq)
    reply = build_report_reply(seq, analysis)
    await update.message.reply_text(reply)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_history_only()
    await update.message.reply_text("Đã xóa toàn bộ lịch sử.")


async def reset_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_history_only()
    await update.message.reply_text("Đã reset lịch sử.")


async def reset_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_all()
    await update.message.reply_text("Đã xóa toàn bộ: lịch sử và não.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not update.message:
        return
    text = update.message.text or ""
    await process_live_input(update, context, text)


def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong biến môi trường.")
    if not ADMIN_USER_ID:
        raise RuntimeError("Thiếu ADMIN_USER_ID trong biến môi trường hoặc giá trị không hợp lệ.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("patterns", patterns_cmd))
    app.add_handler(CommandHandler("brain", brain_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("reset_history", reset_history_cmd))
    app.add_handler(CommandHandler("reset_all", reset_all_cmd))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
