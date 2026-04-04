import os
import re
import json
import math
import sqlite3
import asyncio
import logging
import time
from io import BytesIO
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    from PIL import Image, ImageEnhance, ImageFilter, ImageDraw
    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageEnhance = None
    ImageFilter = None
    ImageDraw = None
    PIL_AVAILABLE = False

from telegram import Update
from telegram.error import Conflict, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "ai_state.db")

THRESHOLD = int(os.getenv("THRESHOLD", "11"))
LOW_LABEL = os.getenv("LOW_LABEL", "Xỉu")
HIGH_LABEL = os.getenv("HIGH_LABEL", "Tài")

RECENT_CACHE = int(os.getenv("RECENT_CACHE", "500"))
MAX_KEEP_HISTORY = int(os.getenv("MAX_KEEP_HISTORY", "0"))
MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))
MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "6"))
HISTORY_ANALYSIS_LIMIT = int(os.getenv("HISTORY_ANALYSIS_LIMIT", "0"))

MIN_PREDICTION_DATA = int(os.getenv("MIN_PREDICTION_DATA", "20"))
CLEAR_PATTERN_MIN_SCORE = int(os.getenv("CLEAR_PATTERN_MIN_SCORE", "80"))
LOSS_STREAK_LIMIT = int(os.getenv("LOSS_STREAK_LIMIT", "5"))

CONFIRM_NEW_PATTERN_MIN_SCORE = int(os.getenv("CONFIRM_NEW_PATTERN_MIN_SCORE", "88"))
CONFIRM_PATTERN_MIN_SCORE = CONFIRM_NEW_PATTERN_MIN_SCORE

ROBOT_ANALYZE_DELAY = float(os.getenv("ROBOT_ANALYZE_DELAY", "5"))
ROBOT_SOURCE_PATH = os.getenv("ROBOT_SOURCE_PATH", "robot.jpg")
ROBOT_IMAGE_PATH = os.getenv("ROBOT_IMAGE_PATH", "robot.jpg")
ROBOT_ANIM_PATH = os.getenv("ROBOT_ANIM_PATH", "robot_anim.gif")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))  # điền Telegram user ID của bạn vào biến môi trường

def is_admin(update: Update) -> bool:
    user = getattr(update, "effective_user", None)
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)

async def admin_only(update: Update) -> bool:
    if is_admin(update):
        return True
    if getattr(update, "message", None):
        await update.message.reply_text("⛔ Bot này chỉ dành cho admin.")
    return False

if not TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
users: Dict[int, Dict[str, Any]] = {}


def ensure_robot_asset() -> str:
    if os.path.exists(ROBOT_IMAGE_PATH):
        return ROBOT_IMAGE_PATH

    try:
        fig, ax = plt.subplots(figsize=(5, 3), dpi=150)
        fig.patch.set_facecolor("#0f1117")
        ax.set_facecolor("#11151c")
        ax.axis("off")
        ax.text(0.5, 0.60, "ROBOT", ha="center", va="center", fontsize=28, fontweight="bold", color="white")
        ax.text(0.5, 0.35, "ĐANG PHÂN TÍCH...", ha="center", va="center", fontsize=14, color="white")
        fig.savefig(ROBOT_IMAGE_PATH, format="jpg", bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
    except Exception as e:
        logger.exception("Không tạo được file robot: %s", e)
    return ROBOT_IMAGE_PATH


def _load_robot_source() -> str:
    if os.path.exists(ROBOT_SOURCE_PATH):
        return ROBOT_SOURCE_PATH
    if os.path.exists(ROBOT_IMAGE_PATH):
        return ROBOT_IMAGE_PATH
    return ensure_robot_asset()


def ensure_robot_animation() -> str:
    if os.path.exists(ROBOT_ANIM_PATH):
        return ROBOT_ANIM_PATH

    if not PIL_AVAILABLE:
        return ensure_robot_asset()

    base_path = _load_robot_source()
    try:
        src = Image.open(base_path).convert("RGBA")
        w, h = src.size
        frames = []

        for i in range(24):
            phase = i / 24.0

            frame = src.copy()
            scale = 1.0 + 0.015 * math.sin(2 * math.pi * phase)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            scaled = frame.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", (w, h), (255, 255, 255, 255))
            x = (w - nw) // 2
            y = (h - nh) // 2 + int(5 * math.sin(2 * math.pi * phase))
            canvas.alpha_composite(scaled, (x, y))
            draw = ImageDraw.Draw(canvas)

            cx, cy = int(w * 0.48), int(h * 0.64)
            orbit_r = int(min(w, h) * 0.17)
            for j, off in enumerate([0.0, 2.2, 4.1]):
                ang = 2 * math.pi * phase + off
                px = cx + int(orbit_r * math.cos(ang))
                py = cy + int(orbit_r * 0.6 * math.sin(ang))
                rad = 4 + j
                alpha = int(70 + 90 * (0.5 + 0.5 * math.sin(2 * math.pi * phase + off)))
                draw.ellipse((px - rad, py - rad, px + rad, py + rad), fill=(110, 210, 255, alpha))

            blink_wave = max(0.08, (math.sin(2 * math.pi * phase * 2) + 1) / 2)
            eye_h = int(92 * (0.18 + 0.82 * blink_wave))
            eye_w = 28
            eye_y = int(h * 0.58)
            eye1_x = int(w * 0.41)
            eye2_x = int(w * 0.51)
            for ex in (eye1_x, eye2_x):
                top = eye_y + (92 - eye_h) // 2
                draw.rounded_rectangle((ex - eye_w // 2, top, ex + eye_w // 2, top + eye_h),
                                       radius=12, fill=(52, 225, 255, 220))

            arm_y = int(h * 0.71)
            swing = int(12 * math.sin(2 * math.pi * phase + math.pi / 2))
            draw.line((int(w * 0.34), arm_y, int(w * 0.30) + swing, arm_y + 50),
                      fill=(110, 210, 255, 90), width=10)
            draw.line((int(w * 0.62), arm_y, int(w * 0.66) - swing, arm_y + 50),
                      fill=(110, 210, 255, 90), width=10)

            ring_y = int(h * 0.90)
            glow_w = int(w * 0.08 + 10 * math.sin(2 * math.pi * phase))
            glow_h = int(h * 0.01 + 3 * math.sin(2 * math.pi * phase))
            alpha = int(120 + 60 * (0.5 + 0.5 * math.sin(2 * math.pi * phase)))
            draw.ellipse((int(w * 0.43) - glow_w, ring_y - glow_h, int(w * 0.43) + glow_w, ring_y + glow_h),
                          fill=(65, 220, 255, alpha))

            canvas = ImageEnhance.Brightness(canvas).enhance(0.98 + 0.05 * (0.5 + 0.5 * math.sin(2 * math.pi * phase)))
            frames.append(canvas.convert("P", palette=Image.Palette.ADAPTIVE))

        frames[0].save(
            ROBOT_ANIM_PATH,
            save_all=True,
            append_images=frames[1:],
            duration=90,
            loop=0,
            optimize=False,
        )
    except Exception as e:
        logger.exception("Không tạo được robot animation: %s", e)
        return ensure_robot_asset()

    return ROBOT_ANIM_PATH


def rotate_broken_db() -> None:
    if not os.path.exists(DB_FILE):
        return
    broken_name = f"{DB_FILE}.broken.{int(time.time())}"
    try:
        os.replace(DB_FILE, broken_name)
        logger.warning("DB hỏng, đã đổi tên sang %s", broken_name)
    except Exception:
        try:
            os.remove(DB_FILE)
        except Exception:
            pass


def db_connect() -> sqlite3.Connection:
    last_err: Optional[Exception] = None

    for attempt in range(2):
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA foreign_keys=ON;")
            return conn
        except sqlite3.DatabaseError as e:
            last_err = e
            logger.exception("DB connect failed: %s", e)
            if attempt == 0:
                rotate_broken_db()
                continue

    raise RuntimeError(f"Không thể mở DB: {last_err}")


async def run_db_work(fn):
    return await asyncio.to_thread(fn)


def init_db() -> None:
    try:
        with db_connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    raw_value INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);
                """
            )
            conn.commit()
    except sqlite3.DatabaseError as e:
        logger.exception("init_db failed, recreating DB: %s", e)
        rotate_broken_db()
        with db_connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_state (
                    chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    raw_value INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);
                """
            )
            conn.commit()


def prune_history(conn: sqlite3.Connection, chat_id: int, keep_limit: int) -> None:
    if keep_limit <= 0:
        return
    row = conn.execute(
        "SELECT id FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (chat_id, max(0, keep_limit - 1)),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM history WHERE chat_id = ? AND id < ?", (chat_id, int(row["id"])))


async def append_history(chat_id: int, items: List[Tuple[int, str]]) -> None:
    if not items:
        return

    def _work():
        try:
            with db_connect() as conn:
                for raw_value, label in items:
                    conn.execute(
                        "INSERT INTO history (chat_id, raw_value, label) VALUES (?, ?, ?)",
                        (chat_id, int(raw_value), str(label)),
                    )
                prune_history(conn, chat_id, MAX_KEEP_HISTORY)
                conn.commit()
        except Exception as e:
            logger.exception("append_history failed: %s", e)

    async with DB_LOCK:
        await run_db_work(_work)


async def load_history_rows(chat_id: int, limit: int = HISTORY_ANALYSIS_LIMIT) -> List[Tuple[int, str]]:
    def _work():
        try:
            with db_connect() as conn:
                if limit and limit > 0:
                    rows = conn.execute(
                        "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                        (chat_id, limit),
                    ).fetchall()
                    rows = list(reversed(rows))
                else:
                    rows = conn.execute(
                        "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id ASC",
                        (chat_id,),
                    ).fetchall()
                return [(int(r["raw_value"]), str(r["label"])) for r in rows]
        except Exception as e:
            logger.exception("load_history_rows failed: %s", e)
            return []

    async with DB_LOCK:
        return await run_db_work(_work)


def new_state() -> Dict[str, Any]:
    return {
        "values": [],
        "labels": [],
        "total": 0,
        "low_count": 0,
        "high_count": 0,
        "last_prediction_label": None,
        "last_prediction_conf": 0,
        "last_prediction_result": "CHƯA RÕ",
        "prediction_total": 0,
        "prediction_hits": 0,
        "prediction_misses": 0,
        "current_correct_streak": 0,
        "current_wrong_streak": 0,
        "max_correct_streak": 0,
        "max_wrong_streak": 0,
        "model_accuracy": {"pattern": 50, "structure": 50},
        "last_note": "",
        "last_structure": "CHƯA ĐỦ DỮ LIỆU",
        "last_mode": "NORMAL",
        "last_model_predictions": {},
        "last_gate_status": "CHỜ",
        "last_gate_reason": "Chưa kiểm tra",
        "last_detected_pattern": "",
        "last_detected_hint": None,
        "last_chart_label": "",
        "last_chart_conf": 0,
        "cooldown_active": False,
        "cooldown_reason": "",
        "last_relearn_note": "",
        "last_resume_note": "",
        "last_relearn_snapshot": "",
        "last_relearn_total": 0,
        "pattern_confirm_sig": "",
        "pattern_confirm_count": 0,
    }


def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    return list(seq[-limit:]) if limit > 0 and len(seq) > limit else list(seq)


def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), RECENT_CACHE)
    d["labels"] = _safe_tail(d.get("labels", []), RECENT_CACHE)


def rebuild_counters_from_labels(d: Dict[str, Any], labels: List[str]) -> None:
    d["low_count"] = labels.count(LOW_LABEL)
    d["high_count"] = labels.count(HIGH_LABEL)
    d["total"] = len(labels)


def repair_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        d = new_state()

    for k in ("values", "labels"):
        if not isinstance(d.get(k), list):
            d[k] = []

    if not isinstance(d.get("model_accuracy"), dict):
        d["model_accuracy"] = {"pattern": 50, "structure": 50}

    defaults = new_state()
    for k, v in defaults.items():
        d.setdefault(k, v)

    n = min(len(d["values"]), len(d["labels"]))
    d["values"] = d["values"][-n:] if n else []
    d["labels"] = d["labels"][-n:] if n else []

    trim_state_memory(d)
    rebuild_counters_from_labels(d, d.get("labels", []))
    return d


def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)


def map_value(n: int) -> str:
    return HIGH_LABEL if n >= THRESHOLD else LOW_LABEL


def opposite_label(label: str) -> str:
    return HIGH_LABEL if label == LOW_LABEL else LOW_LABEL


def get_key(update: Update) -> int:
    return update.effective_chat.id


def parse_input(text: str) -> List[int]:
    nums: List[int] = []
    text = (text or "").strip()
    for x in re.findall(r"\d+", text):
        try:
            n = int(x)
            if n >= 0:
                nums.append(n)
        except Exception:
            continue
    return nums[:MAX_INPUT_NUMS]


async def load_state(chat_id: int, force_reload: bool = False) -> Dict[str, Any]:
    if not force_reload and chat_id in users:
        return repair_state(users[chat_id])

    def _work():
        try:
            with db_connect() as conn:
                return conn.execute(
                    "SELECT state_json FROM chat_state WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
        except Exception as e:
            logger.exception("load_state DB failed: %s", e)
            return None

    async with DB_LOCK:
        row = await run_db_work(_work)

    state = new_state()
    if row:
        try:
            loaded = json.loads(row["state_json"])
            if isinstance(loaded, dict):
                state.update(loaded)
        except Exception as e:
            logger.exception("load_state JSON failed: %s", e)

    state = repair_state(state)
    users[chat_id] = state
    trim_cache()
    return state


async def save_state(chat_id: int, state: Dict[str, Any]) -> None:
    state = repair_state(state)

    def _work():
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_state (chat_id, state_json, updated_at)
                    VALUES (?, ?, unixepoch())
                    ON CONFLICT(chat_id) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (chat_id, json.dumps(state, ensure_ascii=False, default=str)),
                )
                prune_history(conn, chat_id, MAX_KEEP_HISTORY)
                conn.commit()
        except Exception as e:
            logger.exception("save_state failed: %s", e)

    async with DB_LOCK:
        await run_db_work(_work)


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def current_streak(labels: List[str]) -> Tuple[Optional[str], int]:
    if not labels:
        return None, 0
    last = labels[-1]
    streak = 1
    for i in range(len(labels) - 2, -1, -1):
        if labels[i] == last:
            streak += 1
        else:
            break
    return last, streak


def run_length_encode(labels: List[str]) -> List[Tuple[str, int]]:
    if not labels:
        return []
    out: List[Tuple[str, int]] = []
    cur = labels[0]
    count = 1
    for x in labels[1:]:
        if x == cur:
            count += 1
        else:
            out.append((cur, count))
            cur = x
            count = 1
    out.append((cur, count))
    return out


def recent_ratio(labels: List[str], window: int) -> Dict[str, float]:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if not tail:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(tail)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: c[LOW_LABEL] / total, HIGH_LABEL: c[HIGH_LABEL] / total}


def alternating_tail(labels: List[str], window: int = 6) -> Tuple[bool, float]:
    tail = labels[-window:] if len(labels) >= window else labels[:]
    if len(tail) < 4:
        return False, 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    ratio = changes / (len(tail) - 1)
    return all(tail[i] != tail[i - 1] for i in range(1, len(tail))), ratio


def entropy_score(labels: List[str], window: int = 20) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    c = Counter(tail)
    total = len(tail)
    ent = 0.0
    for v in c.values():
        p = v / total
        ent -= p * math.log2(p)
    return ent


def volatility_score(labels: List[str], window: int = 12) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    return changes / (len(tail) - 1)


def detect_repeat_block(labels: List[str], max_block: int = 3) -> Optional[Dict[str, Any]]:
    tail = labels[-12:] if len(labels) > 12 else labels[:]
    for block in range(1, max_block + 1):
        if len(tail) >= block * 2:
            a = tail[-(block * 2):-block]
            b = tail[-block:]
            if a == b:
                return {"name": f"LẶP KHỐI {block}", "detail": f"Khối {block} mẫu gần nhất đang lặp", "score": min(78 + block * 4, 92), "hint": a[0] if a else None}
    return None


def detect_motif_repeat(labels: List[str], max_motif: int = 8) -> Optional[Dict[str, Any]]:
    tail = labels[-40:] if len(labels) > 40 else labels[:]
    if len(tail) < 4:
        return None

    best: Optional[Dict[str, Any]] = None
    for m in range(1, min(max_motif, len(tail) // 2) + 1):
        motif = tail[-m:]
        reps = 1
        idx = len(tail) - m
        while idx - m >= 0 and tail[idx - m:idx] == motif:
            reps += 1
            idx -= m

        if reps >= 2:
            if m == 1:
                name = "BỆT"
                hint = motif[0]
                score = min(70 + reps * 6, 95)
            elif m == 2 and motif[0] != motif[1]:
                name = "XEN KẼ"
                hint = opposite_label(tail[-1])
                score = min(76 + reps * 4, 94)
            else:
                name = f"LẶP MẪU {m}"
                hint = motif[0]
                score = min(72 + reps * 5, 94)

            cand = {"name": name, "detail": f"Mẫu {m} lặp {reps} lần", "score": score, "hint": hint}
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def detect_explicit_pair_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if len(labels) < 4:
        return out
    a, b, c, d = labels[-4], labels[-3], labels[-2], labels[-1]
    if a != b and a == c and b == d:
        out.append({"name": "1-1", "detail": "Mẫu luân phiên 1-1", "score": 90, "hint": a})
    if a == b and c == d and a != c:
        out.append({"name": "2-2", "detail": "Mẫu chia cặp 2-2", "score": 86, "hint": a})
    if len(labels) >= 6:
        t = labels[-6:]
        if t[0] == t[2] == t[4] and t[1] == t[3] == t[5] and t[0] != t[1]:
            out.append({"name": "XEN KẼ SÂU", "detail": "Luân phiên đều trong 6 mẫu gần nhất", "score": 94, "hint": t[0]})
    return out


def detect_run_cycle(labels: List[str]) -> Optional[Dict[str, Any]]:
    runs = run_length_encode(labels)
    if len(runs) < 4:
        return None
    a1, l1 = runs[-4]
    b1, m1 = runs[-3]
    a2, l2 = runs[-2]
    b2, m2 = runs[-1]
    if a1 == a2 and b1 == b2 and l1 == l2 and m1 == m2 and a1 != b1:
        return {"name": f"CẦU {l1}-{m1}", "detail": f"Chu kỳ 2 khối lặp: {l1}-{m1}", "score": min(82 + (l1 + m1) * 2, 95), "hint": a2}
    return None


def detect_bias(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 12:
        return None
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)
    gap6 = abs(r6[LOW_LABEL] - r6[HIGH_LABEL])
    gap12 = abs(r12[LOW_LABEL] - r12[HIGH_LABEL])
    gap24 = abs(r24[LOW_LABEL] - r24[HIGH_LABEL])

    if gap6 >= 0.50:
        winner = LOW_LABEL if r6[LOW_LABEL] > r6[HIGH_LABEL] else HIGH_LABEL
        return {"name": "NGHIÊNG NHẸ", "detail": f"Đuôi 6 nghiêng về {winner}", "score": 72, "hint": winner}
    if gap12 >= 0.35:
        winner = LOW_LABEL if r12[LOW_LABEL] > r12[HIGH_LABEL] else HIGH_LABEL
        return {"name": "NGHIÊNG", "detail": f"Đuôi 12 nghiêng về {winner}", "score": 78, "hint": winner}
    if gap24 >= 0.25:
        winner = LOW_LABEL if r24[LOW_LABEL] > r24[HIGH_LABEL] else HIGH_LABEL
        return {"name": "XU HƯỚNG", "detail": f"24 mẫu gần đây nghiêng về {winner}", "score": 80, "hint": winner}
    if gap24 < 0.10:
        return {"name": "CÂN BẰNG", "detail": "Hai phía gần như ngang nhau", "score": 65, "hint": None}
    return None


def detect_all_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    patterns: List[Dict[str, Any]] = []
    for item in (
        detect_motif_repeat(labels, 8),
        *detect_explicit_pair_patterns(labels),
        detect_run_cycle(labels),
        detect_repeat_block(labels, 3),
        detect_reversal(labels),
        detect_bias(labels),
    ):
        if item:
            patterns.append(item)

    last, streak = current_streak(labels)
    if last in (LOW_LABEL, HIGH_LABEL) and streak >= 3:
        patterns.append({"name": "BỆT", "detail": f"{last} x{streak}", "score": min(68 + streak * 6, 95), "hint": last})

    alt, alt_ratio = alternating_tail(labels, 6)
    if alt and alt_ratio >= 0.80 and len(labels) >= 6:
        patterns.append({"name": "XEN KẼ", "detail": "Chuỗi đổi liên tục", "score": 88, "hint": opposite_label(labels[-1])})

    return sorted(patterns, key=lambda x: x.get("score", 0), reverse=True)[:12]


def build_report(labels: List[str]) -> Dict[str, Any]:
    c = Counter(labels)
    last, streak = current_streak(labels)
    alt, alt_ratio = alternating_tail(labels, 6)
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)
    ent = entropy_score(labels, 20)
    vol = volatility_score(labels, 12)
    patterns = detect_all_patterns(labels)

    if len(labels) < 4:
        structure = "CHƯA ĐỦ DỮ LIỆU"
        detail = "Cần thêm kết quả"
    elif patterns:
        structure = patterns[0]["name"]
        detail = patterns[0]["detail"]
    else:
        if alt and alt_ratio >= 0.80:
            structure = "XEN KẼ"
            detail = "Chuỗi đổi liên tục"
        elif streak >= 4 and last in (LOW_LABEL, HIGH_LABEL):
            structure = "BỆT"
            detail = f"{last} x{streak}"
        elif vol >= 0.65:
            structure = "CHUYỂN PHA"
            detail = "Nhịp đang đổi nhanh"
        elif ent <= 0.85:
            structure = "ỔN ĐỊNH"
            detail = "Mẫu gần đây khá đều"
        else:
            structure = "TRUNG TÍNH"
            detail = "Chưa có tín hiệu quá rõ"

    return {
        "total": len(labels),
        "low": c.get(LOW_LABEL, 0),
        "high": c.get(HIGH_LABEL, 0),
        "labels": labels,
        "last_label": last,
        "streak": streak,
        "alternating": alt,
        "alt_ratio": alt_ratio,
        "structure": structure,
        "detail": detail,
        "recent_6": r6,
        "recent_12": r12,
        "recent_24": r24,
        "entropy": ent,
        "volatility": vol,
        "patterns": patterns,
    }


def advanced_metrics(labels: List[str]) -> Dict[str, Any]:
    if not labels:
        return {"max_streak": 0, "r10_high": 0.5, "r20_high": 0.5, "momentum": 0.0, "noise": 0.0, "reversal": 0.0}
    max_streak = 1
    cur = 1
    for i in range(1, len(labels)):
        if labels[i] == labels[i - 1]:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1
    last10 = labels[-10:]
    last20 = labels[-20:]
    r10 = safe_div(last10.count(HIGH_LABEL), len(last10)) if last10 else 0.5
    r20 = safe_div(last20.count(HIGH_LABEL), len(last20)) if last20 else 0.5
    momentum = r10 - r20
    changes = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    noise = safe_div(changes, len(labels))
    reversal = abs(momentum) * 100
    return {"max_streak": max_streak, "r10_high": r10, "r20_high": r20, "momentum": momentum, "noise": noise, "reversal": reversal}


def extract_chart_features(labels: List[str]) -> Dict[str, Any]:
    if not labels:
        return {"trend": 0.0, "trend_label": "TRUNG TÍNH", "reversal_rate": 0.0, "smoothness": 0.0, "max_streak": 0, "last_value": None, "last_5_high": 0.5, "prev_5_high": 0.5}

    ys = [1 if x == HIGH_LABEL else 0 for x in labels]
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = 0.0
    if denom:
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom

    trend = slope * 100.0
    trend_label = f"NGHIÊNG {HIGH_LABEL}" if trend > 0.08 else f"NGHIÊNG {LOW_LABEL}" if trend < -0.08 else "TRUNG TÍNH"
    reversals = sum(1 for i in range(1, n) if ys[i] != ys[i - 1])
    reversal_rate = reversals / max(1, n - 1)
    smoothness = 1.0 - reversal_rate
    max_streak = 1
    cur = 1
    for i in range(1, n):
        if ys[i] == ys[i - 1]:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1
    last_5 = ys[-5:] if n >= 5 else ys[:]
    prev_5 = ys[-10:-5] if n >= 10 else ys[:-5] if n > 5 else []
    return {
        "trend": trend,
        "trend_label": trend_label,
        "reversal_rate": reversal_rate,
        "smoothness": smoothness,
        "max_streak": max_streak,
        "last_value": ys[-1],
        "last_5_high": safe_div(sum(last_5), len(last_5)) if last_5 else 0.5,
        "prev_5_high": safe_div(sum(prev_5), len(prev_5)) if prev_5 else 0.5,
    }


def build_relearn_snapshot(report: Dict[str, Any], adv: Dict[str, Any]) -> str:
    patterns = report.get("patterns", [])[:3]
    top = " / ".join(p.get("name", "") for p in patterns) if patterns else "TRUNG TÍNH"
    return (
        f"Cầu: {top} | Cấu trúc: {report.get('structure', '-')} | "
        f"Chi tiết: {report.get('detail', '-')} | Tổng: {report.get('total', 0)} | "
        f"Mượt: {adv.get('smoothness', 0.0):.2f} | Nhiễu: {report.get('volatility', 0.0):.2f}"
    )


def enter_loss_cooldown(state: Dict[str, Any], report: Dict[str, Any], adv: Dict[str, Any]) -> bool:
    if state.get("cooldown_active"):
        return False
    state["cooldown_active"] = True
    state["cooldown_reason"] = f"Thua chuỗi {LOSS_STREAK_LIMIT} liên tiếp"
    state["last_relearn_note"] = f"Tự học lại từ đầu bộ lịch sử sau chuỗi thua {LOSS_STREAK_LIMIT}"
    state["last_note"] = state["last_relearn_note"]
    state["last_relearn_snapshot"] = build_relearn_snapshot(report, adv)
    state["last_relearn_total"] = int(report.get("total", 0))
    state["model_accuracy"] = {"pattern": 50, "structure": 50}
    state["last_model_predictions"] = {}
    state["last_gate_status"] = "TẠM DỪNG"
    state["last_gate_reason"] = state["cooldown_reason"]
    return True


def clear_loss_cooldown(state: Dict[str, Any]) -> bool:
    if not state.get("cooldown_active"):
        return False
    if int(state.get("current_wrong_streak", 0)) != 0:
        return False
    state["cooldown_active"] = False
    state["cooldown_reason"] = ""
    state["last_resume_note"] = "Đã có tín hiệu đúng, mở lại phân tích"
    state["last_note"] = state["last_resume_note"]
    return True


def pattern_signature(pattern: Dict[str, Any]) -> str:
    return f"{pattern.get('name','')}|{pattern.get('hint','')}|{int(pattern.get('score',0))}"


def update_confirm_state(state: Dict[str, Any], report: Dict[str, Any]) -> Tuple[bool, str]:
    patterns = report.get("patterns", [])
    if not patterns:
        state["pattern_confirm_sig"] = ""
        state["pattern_confirm_count"] = 0
        return False, "Chưa có cầu rõ ràng để xác nhận"

    top = patterns[0]
    sig = pattern_signature(top)
    score = int(top.get("score", 0))
    total = int(report.get("total", 0))

    need_wait = False
    reason = ""

    if score < CONFIRM_NEW_PATTERN_MIN_SCORE and total >= 8:
        need_wait = True
        if state.get("pattern_confirm_sig") != sig:
            state["pattern_confirm_sig"] = sig
            state["pattern_confirm_count"] = 1
        else:
            state["pattern_confirm_count"] = int(state.get("pattern_confirm_count", 0)) + 1

        if state["pattern_confirm_count"] < 2:
            reason = f"Đang bắt nhịp mới: {top.get('name','-')} ({score}%), đợi thêm 1 mẫu"
        else:
            need_wait = False
            reason = f"Đã xác nhận nhịp mới: {top.get('name','-')} ({score}%)"
    else:
        if state.get("pattern_confirm_sig") != sig:
            state["pattern_confirm_sig"] = sig
            state["pattern_confirm_count"] = 1
        else:
            state["pattern_confirm_count"] = int(state.get("pattern_confirm_count", 0)) + 1
        reason = f"Cầu rõ: {top.get('name','-')} ({score}%)"

    return need_wait, reason


def prediction_gate(labels: List[str], report: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    if state and state.get("cooldown_active"):
        if int(state.get("current_wrong_streak", 0)) == 0:
            return True, "Đã thoát chế độ học lại"
        return False, str(state.get("cooldown_reason") or f"Đang tự học lại sau chuỗi thua {LOSS_STREAK_LIMIT}")

    total = int(report.get("total", 0))
    patterns = report.get("patterns", [])
    top_name = str(patterns[0].get("name", "")) if patterns else ""
    top_score = int(patterns[0].get("score", 0)) if patterns else 0

    if state is not None:
        need_wait, wait_reason = update_confirm_state(state, report)
        if need_wait:
            return False, wait_reason

    if total < MIN_PREDICTION_DATA:
        if total >= 8 and top_score >= CLEAR_PATTERN_MIN_SCORE and top_name:
            return True, f"Cầu sớm: {top_name} ({top_score}%)"
        return False, f"Chưa đủ {MIN_PREDICTION_DATA} dữ liệu"

    if not patterns:
        return False, "Không có cầu rõ ràng để phân tích"

    if top_score < CLEAR_PATTERN_MIN_SCORE:
        if total >= 10 and top_score >= max(72, CLEAR_PATTERN_MIN_SCORE - 10):
            return True, f"Cầu sớm: {top_name} ({top_score}%)"
        return False, f"Cầu chưa đủ rõ: {top_name} ({top_score}%)"

    return True, f"Cầu rõ: {top_name} ({top_score}%)"


def predict_pattern(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        primary = patterns[0]
        hint = primary.get("hint")
        name = primary.get("name", "")
        if hint in (LOW_LABEL, HIGH_LABEL) and name in {
            "BỆT", "XEN KẼ", "1-1", "2-2", "XEN KẼ SÂU",
            "LẶP KHỐI 1", "LẶP KHỐI 2", "LẶP KHỐI 3",
            "LẶP MẪU 2", "LẶP MẪU 3", "LẶP MẪU 4", "LẶP MẪU 5", "LẶP MẪU 6", "LẶP MẪU 7", "LẶP MẪU 8",
            "CẦU 1-1", "CẦU 2-1", "CẦU 1-2", "CẦU 2-2", "CẦU 3-1", "CẦU 1-3", "CẦU 3-2", "CẦU 2-3", "CẦU 3-3",
            "NGHIÊNG", "NGHIÊNG NHẸ", "XU HƯỚNG", "ĐẢO CHIỀU"
        }:
            return {"label": hint, "confidence": min(95, int(primary.get("score", 60)) + 2), "source": f"pattern:{name}"}

    if len(labels) < 4:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "pattern"}
    if labels[-1] == labels[-2] == labels[-3]:
        return {"label": labels[-1], "confidence": 66, "source": "pattern"}
    if labels[-1] != labels[-2]:
        return {"label": labels[-2], "confidence": 56, "source": "pattern"}
    return {"label": labels[-1], "confidence": 54, "source": "pattern"}


def predict_structure(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        top = patterns[0]
        hint = top.get("hint")
        name = top.get("name", "")
        strong_same = {"BỆT", "BỆT SỚM", "LẶP MẪU 1", "LẶP KHỐI 1", "XEN KẼ SỚM", "XEN KẼ", "XEN KẼ SÂU", "1-1", "2-2"}
        if hint in (LOW_LABEL, HIGH_LABEL) and name in strong_same:
            return {"label": hint, "confidence": min(95, int(top.get("score", 60)) + 1), "source": f"structure:{name}"}
        if name.startswith("CẦU ") or name.startswith("LẶP MẪU") or name.startswith("LẶP KHỐI"):
            return {
                "label": hint if hint in (LOW_LABEL, HIGH_LABEL) else (labels[-1] if labels else LOW_LABEL),
                "confidence": min(92, int(top.get("score", 60)) + 1),
                "source": f"structure:{name}",
            }
        if name == "CÂN BẰNG SỚM":
            return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 52, "source": "structure:balance"}

    if len(labels) < 2:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "structure"}
    return {"label": labels[-1], "confidence": 54, "source": "structure"}


def predict_chart(labels: List[str], report: Dict[str, Any], adv: Dict[str, Any]) -> Dict[str, Any]:
    if not labels:
        return {"label": LOW_LABEL, "confidence": 50, "source": "chart"}

    trend = float(adv.get("trend", 0.0))
    trend_label = str(adv.get("trend_label", "TRUNG TÍNH"))
    smoothness = float(adv.get("smoothness", 0.0))
    reversal_rate = float(adv.get("reversal_rate", 0.0))
    last_5_high = float(adv.get("last_5_high", 0.5))
    prev_5_high = float(adv.get("prev_5_high", 0.5))
    momentum = float(adv.get("momentum", 0.0))
    last_label = labels[-1]

    if trend > 0.10 or (last_5_high - prev_5_high) >= 0.15 or momentum > 0.08:
        label = HIGH_LABEL
        base = 62
        if trend > 0.20:
            base += 10
        if last_5_high > 0.60:
            base += 8
        if smoothness >= 0.60:
            base += 4
        if reversal_rate < 0.35:
            base += 4
    elif trend < -0.10 or (prev_5_high - last_5_high) >= 0.15 or momentum < -0.08:
        label = LOW_LABEL
        base = 62
        if trend < -0.20:
            base += 10
        if last_5_high < 0.40:
            base += 8
        if smoothness >= 0.60:
            base += 4
        if reversal_rate < 0.35:
            base += 4
    else:
        label = last_label
        base = 52
        if trend_label != "TRUNG TÍNH":
            base += 3
        if 0.40 <= last_5_high <= 0.60:
            base += 2

    if trend_label == f"NGHIÊNG {HIGH_LABEL}" and label == HIGH_LABEL:
        base += 4
    if trend_label == f"NGHIÊNG {LOW_LABEL}" and label == LOW_LABEL:
        base += 4

    confidence = max(50, min(95, int(base)))
    return {"label": label, "confidence": confidence, "source": "chart"}


def update_model_accuracy(state: Dict[str, Any], predictions: Dict[str, Dict[str, Any]], actual: str) -> None:
    state.setdefault("model_accuracy", {"pattern": 50, "structure": 50})
    for name, pred in predictions.items():
        old = int(state["model_accuracy"].get(name, 50))
        old = old + 1 if pred.get("label") == actual else old - 1
        state["model_accuracy"][name] = max(1, min(99, old))


def update_prediction_feedback(state: Dict[str, Any], actual_label: str) -> None:
    pred = state.get("last_prediction_label")
    if pred not in (LOW_LABEL, HIGH_LABEL):
        return

    state["prediction_total"] = int(state.get("prediction_total", 0)) + 1
    if pred == actual_label:
        state["prediction_hits"] = int(state.get("prediction_hits", 0)) + 1
        state["last_prediction_result"] = "ĐÚNG"
        state["current_correct_streak"] = int(state.get("current_correct_streak", 0)) + 1
        state["current_wrong_streak"] = 0
        state["max_correct_streak"] = max(int(state.get("max_correct_streak", 0)), int(state["current_correct_streak"]))
    else:
        state["prediction_misses"] = int(state.get("prediction_misses", 0)) + 1
        state["last_prediction_result"] = "SAI"
        state["current_wrong_streak"] = int(state.get("current_wrong_streak", 0)) + 1
        state["current_correct_streak"] = 0
        state["max_wrong_streak"] = max(int(state.get("max_wrong_streak", 0)), int(state["current_wrong_streak"]))


def meta_decision(predictions: Dict[str, Dict[str, Any]], state: Dict[str, Any], report: Dict[str, Any], adv: Dict[str, Any]) -> Dict[str, Any]:
    model_acc = state.get("model_accuracy", {})
    vote: Dict[str, float] = defaultdict(float)
    model_scores: Dict[str, float] = {}

    strong_patterns = {
        "BỆT", "BỆT SỚM", "XEN KẼ", "XEN KẼ SỚM", "XEN KẼ SÂU",
        "1-1", "2-2", "LẶP KHỐI 1", "LẶP MẪU 2",
        "CẦU 1-1", "CẦU 2-1", "CẦU 1-2", "CẦU 2-2", "CẦU 3-1",
        "CẦU 1-3", "CẦU 2-3", "CẦU 3-2", "CẦU 3-3",
        "NGHIÊNG", "NGHIÊNG NHẸ", "XU HƯỚNG", "ĐẢO CHIỀU",
    }

    volatility = float(report.get("volatility", 0.0))
    smoothness = float(adv.get("smoothness", 0.0))
    reversal_rate = float(adv.get("reversal_rate", 0.0))
    entropy = float(report.get("entropy", 0.0))
    trend = float(adv.get("trend", 0.0))
    last_5_high = float(adv.get("last_5_high", 0.5))
    prev_5_high = float(adv.get("prev_5_high", 0.5))

    for name, pred in predictions.items():
        label = pred.get("label")
        conf = float(pred.get("confidence", 50))
        acc = float(model_acc.get(name, 50))
        score = conf * (0.85 + acc / 120.0)
        score *= 1.05 if name == "pattern" else 1.02 if name == "structure" else 1.0
        if name == "chart":
            if abs(trend) >= 0.18:
                score *= 1.10
            elif abs(trend) >= 0.10:
                score *= 1.05
            if abs(last_5_high - prev_5_high) >= 0.15:
                score *= 1.07
        if report.get("structure") in strong_patterns:
            score *= 1.08
        if smoothness >= 0.70:
            score *= 1.04
        elif reversal_rate >= 0.45:
            score *= 0.92
        if volatility > 0.80:
            score *= 0.90
        elif entropy < 1.0:
            score *= 1.03
        model_scores[name] = score
        vote[label] += score

    if not vote:
        return {"model": "none", "final_label": LOW_LABEL, "confidence": 50, "scores": {}}

    best_label = max(vote, key=vote.get)
    total = sum(vote.values())
    top = vote[best_label]
    top_ratio = top / total if total else 0.5
    agreement = sum(1 for p in predictions.values() if p.get("label") == best_label)
    strongest_conf = max((int(p.get("confidence", 50)) for p in predictions.values()), default=50)

    confidence = int(46 + top_ratio * 40 + (agreement - 1) * 4 + (strongest_conf - 50) * 0.15)
    if report.get("structure") in {"CÂN BẰNG", "TRUNG TÍNH", "CHƯA ĐỦ DỮ LIỆU"}:
        confidence -= 6
    if abs(trend) >= 0.18:
        confidence += 2

    confidence = max(0, min(confidence, 95))
    best_model = max(model_scores, key=model_scores.get)
    return {"model": best_model, "final_label": best_label, "confidence": confidence, "scores": dict(vote)}


def analyze_state_from_labels(state: Dict[str, Any], labels: List[str]) -> Dict[str, Any]:
    report = build_report(labels)
    adv = advanced_metrics(labels)
    chart_features = extract_chart_features(labels)
    adv.update(chart_features)

    resumed = False
    if state.get("cooldown_active") and int(state.get("current_wrong_streak", 0)) == 0:
        resumed = clear_loss_cooldown(state)

    relearned = False
    if int(state.get("current_wrong_streak", 0)) >= LOSS_STREAK_LIMIT and not state.get("cooldown_active", False):
        relearned = enter_loss_cooldown(state, report, adv)

    allowed, reason = prediction_gate(labels, report, state)
    state["last_gate_status"] = "CHO PHÉP" if allowed else "TẠM DỪNG"
    state["last_gate_reason"] = reason
    state["last_structure"] = report["structure"]
    state["last_detected_pattern"] = report["patterns"][0]["name"] if report.get("patterns") else ""
    state["last_detected_hint"] = report["patterns"][0].get("hint") if report.get("patterns") else None

    if not allowed:
        state["last_prediction_label"] = None
        state["last_prediction_conf"] = 0
        state["last_prediction_result"] = "TẠM DỪNG"
        state["last_note"] = reason
        state["last_model_predictions"] = {}
        return {"report": report, "adv": adv, "chart_features": chart_features,
                "predictions": {}, "meta": {}, "allowed": False, "reason": reason,
                "relearned": relearned, "resumed": resumed}

    predictions = {
        "pattern": predict_pattern(labels, report),
        "structure": predict_structure(labels, report),
        "chart": predict_chart(labels, report, adv),
    }
    meta = meta_decision(predictions, state, report, adv)
    state["last_prediction_label"] = meta["final_label"]
    state["last_prediction_conf"] = meta["confidence"]
    state["last_note"] = f"Model: {meta['model']}"
    state["last_structure"] = report["structure"]
    state["last_chart_label"] = predictions.get("chart", {}).get("label", "")
    state["last_chart_conf"] = int(predictions.get("chart", {}).get("confidence", 0))
    state["last_mode"] = "READY" if len(labels) >= MIN_ANALYSIS_LEN else "NORMAL"
    state["last_model_predictions"] = predictions
    state["last_prediction_result"] = "CHỜ KẾT QUẢ"

    return {"report": report, "adv": adv, "chart_features": chart_features, "predictions": predictions,
            "meta": meta, "allowed": True, "reason": reason, "relearned": relearned, "resumed": resumed}


def build_chart_summary(report: Dict[str, Any], adv: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> str:
    cooldown_text = "ĐANG HỌC LẠI" if state and state.get("cooldown_active") else "BÌNH THƯỜNG"
    return (
        f"Tổng: {report.get('total',0)} | {LOW_LABEL}: {report.get('low',0)} | {HIGH_LABEL}: {report.get('high',0)}\n"
        f"Cấu trúc: {report.get('structure','-')}\n"
        f"Chi tiết: {report.get('detail','-')}\n"
        f"Bệt max: {adv.get('max_streak',0)} | 10 gần: {adv.get('r10_high',0.5)*100:.1f}% {HIGH_LABEL} | "
        f"20 gần: {adv.get('r20_high',0.5)*100:.1f}% {HIGH_LABEL}\n"
        f"Momentum: {adv.get('momentum',0.0):.2f} | Trend: {adv.get('trend_label','TRUNG TÍNH')} ({adv.get('trend',0.0):.2f})\n"
        f"Mượt: {adv.get('smoothness',0.0):.2f} | Đảo chiều: {adv.get('reversal',0.0):.1f}%\n"
        f"Entropy: {report.get('entropy',0.0):.2f} | Volatility: {report.get('volatility',0.0):.2f}\n"
        f"Chế độ: {cooldown_text}"
    )


def _point_color(label: str) -> str:
    return "gold" if label == HIGH_LABEL else "white"


def _point_edge(label: str) -> str:
    return "#3a2b00" if label == HIGH_LABEL else "#444444"


def build_bridge_chart_image(labels: List[str], report: Dict[str, Any], adv: Dict[str, Any], state: Optional[Dict[str, Any]] = None, limit: int = 0) -> Optional[BytesIO]:
    tail = labels[-limit:] if limit and len(labels) > limit else labels[:]
    if not tail:
        return None
    xs = list(range(1, len(tail) + 1))
    ys = [1 if x == HIGH_LABEL else 0 for x in tail]

    fig_w = max(14.0, min(38.0, 0.25 * len(tail) + 10.0))
    fig_h = 7.8 if len(tail) < 120 else 8.4
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=180)
    fig.patch.set_facecolor("#0f1117")
    ax.set_facecolor("#11151c")

    for i in range(1, len(xs) + 1):
        ax.axvline(i, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhline(0, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhline(0.5, color="white", alpha=0.22, linewidth=1.2, linestyle="--", zorder=0)
    ax.axhline(1, color="white", alpha=0.05, linewidth=0.8, zorder=0)
    ax.axhspan(-0.02, 0.5, alpha=0.05, color="#ffffff", zorder=0)
    ax.axhspan(0.5, 1.02, alpha=0.05, color="#ffd700", zorder=0)

    ax.step(xs, ys, where="mid", linewidth=4.5, alpha=0.10, color="white", zorder=2)
    ax.step(xs, ys, where="mid", linewidth=2.8, alpha=0.18, color="#7db7ff", zorder=3)
    ax.step(xs, ys, where="mid", linewidth=2.0, alpha=0.95, color="#cfd8dc", zorder=4)
    ax.scatter(xs, ys, s=180, c=[_point_color(t) for t in tail], edgecolors=[_point_edge(t) for t in tail], linewidths=1.4, zorder=5)
    ax.scatter(xs, ys, s=80, c="none", edgecolors="black", linewidths=0.4, alpha=0.45, zorder=4)

    for x, y, t in zip(xs, ys, tail):
        txt_color = "#111111" if t == HIGH_LABEL else "#222222"
        ax.text(x, y, t, ha="center", va="center", fontsize=9, fontweight="bold", color=txt_color, zorder=6)

    if xs:
        ax.scatter([xs[-1]], [ys[-1]], s=320, c=["#ff4d4d"], edgecolors="white", linewidths=1.8, zorder=7)
        ax.text(xs[-1], ys[-1] + (0.11 if ys[-1] == 1 else -0.11), tail[-1], ha="center", va="center",
                fontsize=11, fontweight="bold", color="white", zorder=8)

    if len(ys) >= 5:
        for w, style, alpha, color in [(5, "--", 0.8, "#7db7ff"), (12, ":", 0.65, "#ffb347")]:
            if len(ys) >= w:
                ma = []
                for i in range(len(ys)):
                    start = max(0, i - w + 1)
                    seg = ys[start:i + 1]
                    ma.append(sum(seg) / len(seg))
                ax.plot(xs, ma, linewidth=1.5 if w == 5 else 1.8, alpha=alpha, linestyle=style, color=color, zorder=4)

    ax.set_ylim(-0.15, 1.15)
    ax.set_yticks([0, 1])
    ax.set_yticklabels([LOW_LABEL, HIGH_LABEL], fontsize=11, color="white")
    ax.set_xlabel("Mẫu gần nhất", fontsize=10, color="white")
    ax.set_ylabel("Trạng thái", fontsize=10, color="white")
    ax.set_title("BIỂU ĐỒ CẦU PHÂN TÍCH", fontsize=13, fontweight="bold", color="white")

    if len(xs) <= 14:
        ax.set_xticks(xs)
        ax.set_xticklabels([str(i) for i in xs], fontsize=9, color="white")
    else:
        step = max(1, len(xs) // 12)
        ticks = xs[::step]
        ax.set_xticks(ticks)
        ax.set_xticklabels([str(i) for i in ticks], fontsize=9, color="white")

    for spine in ax.spines.values():
        spine.set_color("#6b7280")
        spine.set_alpha(0.35)
    ax.tick_params(colors="white")
    ax.grid(True, axis="y", alpha=0.14, linestyle="-")

    ax.text(0.02, 0.98, build_chart_summary(report, adv, state), transform=ax.transAxes, va="top", ha="left",
            fontsize=9, color="white",
            bbox=dict(boxstyle="round,pad=0.55", facecolor="#121826", alpha=0.92, edgecolor="#44506a"))

    top_patterns = report.get("patterns", [])[:4]
    quick = " / ".join(p.get("name", "") for p in top_patterns) if top_patterns else "TRUNG TÍNH"
    ax.text(0.98, 0.02, f"Cầu: {quick}", transform=ax.transAxes, va="bottom", ha="right", fontsize=9, color="white",
            bbox=dict(boxstyle="round,pad=0.35", facecolor="#1f2937", alpha=0.9, edgecolor="#6b7280"))

    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


async def send_bridge_chart(update: Update, labels: List[str], report: Dict[str, Any], adv: Dict[str, Any], title: str, state: Optional[Dict[str, Any]] = None) -> None:
    if not update.message:
        return
    chart = build_bridge_chart_image(labels, report, adv, state=state)
    if chart is None:
        await update.message.reply_text("📉 Chưa đủ dữ liệu để vẽ biểu đồ cầu.")
        return
    try:
        chart.seek(0)
        await update.message.reply_photo(photo=chart, caption=f"{title}")
    except Exception as e:
        logger.exception("send_bridge_chart failed: %s", e)
        await update.message.reply_text("📉 Không thể gửi biểu đồ lúc này.")


def build_stats_message(report: Dict[str, Any], state: Dict[str, Any], adv: Dict[str, Any]) -> str:
    total = report["total"]
    low_p = safe_div(report["low"] * 100.0, total)
    high_p = safe_div(report["high"] * 100.0, total)

    patterns = report.get("patterns", [])[:4]
    pattern_lines = "\n".join(f"║ • {p['name']}: {p['detail']}" for p in patterns) if patterns else "║ • Chưa có cầu nổi bật"

    model_acc = state.get("model_accuracy", {})
    model_acc_line = f"P:{int(model_acc.get('pattern', 50))}% S:{int(model_acc.get('structure', 50))}%"
    cooldown_line = "ĐANG HỌC LẠI" if state.get("cooldown_active") else "BÌNH THƯỜNG"

    return (
        "╔════════════════════════════╗\n"
        "║      ✅ BẢNG THỐNG KÊ      ║\n"
        "╠════════════════════════════╣\n"
        f"║ Tổng    : {total}\n"
        f"║ {LOW_LABEL:<6}: {report['low']} ({low_p:.1f}%)\n"
        f"║ {HIGH_LABEL:<6}: {report['high']} ({high_p:.1f}%)\n"
        f"║ Cấu trúc: {report['structure']}\n"
        f"║ Chi tiết : {report['detail']}\n"
        f"║ Bệt max  : {adv.get('max_streak', 0)}\n"
        f"║ 10 gần   : {adv.get('r10_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"║ 20 gần   : {adv.get('r20_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"║ Momentum : {adv.get('momentum', 0.0):.2f}\n"
        f"║ Trend    : {adv.get('trend_label', 'TRUNG TÍNH')} ({adv.get('trend', 0.0):.2f})\n"
        f"║ Mượt     : {adv.get('smoothness', 0.0):.2f}\n"
        f"║ Nhiễu    : {adv.get('noise', 0.0):.2f}\n"
        f"║ Đảo chiều: {adv.get('reversal', 0.0):.1f}%\n"
        f"║ Entropy  : {report.get('entropy', 0.0):.2f}\n"
        f"║ Volatility: {report.get('volatility', 0.0):.2f}\n"
        f"║ Chính xác: {safe_div(state.get('prediction_hits', 0) * 100.0, state.get('prediction_total', 0)):.1f}%\n"
        f"║ Thắng    : {state.get('prediction_hits', 0)}\n"
        f"║ Thua     : {state.get('prediction_misses', 0)}\n"
        f"║ Tổng chốt: {state.get('prediction_total', 0)}\n"
        f"║ Chuỗi thắng max: {state.get('max_correct_streak', 0)}\n"
        f"║ Chuỗi thua  max: {state.get('max_wrong_streak', 0)}\n"
        f"║ M.Acc    : {model_acc_line}\n"
        f"║ Cửa gác  : {state.get('last_gate_status', 'CHỜ')}\n"
        f"║ Lý do    : {state.get('last_gate_reason', 'Chưa kiểm tra')}\n"
        f"║ Học lại  : {cooldown_line}\n"
        f"║ Lý do HL : {state.get('cooldown_reason') or '-'}\n"
        f"║ Ghi chú  : {state.get('last_relearn_note') or state.get('last_resume_note') or '-'}\n"
        f"║ Cầu hiện tại: {state.get('last_detected_pattern') or '-'}\n"
        f"║ Hướng    : {state.get('last_detected_hint') or '-'}\n"
        f"{pattern_lines}\n"
        "╚════════════════════════════╝"
    )


def build_final_message(meta: Dict[str, Any], state: Dict[str, Any]) -> str:
    return (
        "✅ ROBOT ĐÃ PHÂN TÍCH XONG\n"
        f"🔚 CHỐT CUỐI: {meta.get('final_label', '-')}\n"
        f"📊 Tỷ lệ: {meta.get('confidence', 0)}%\n"
        f"🤖 Trạng thái: ĐÃ PHÂN TÍCH XONG"
    )


async def send_robot_status(update: Update, caption: str) -> Optional[Any]:
    if not update.message:
        return None

    anim_path = ensure_robot_animation()
    try:
        with open(anim_path, "rb") as f:
            return await update.message.reply_animation(animation=f, caption=caption)
    except Exception as e:
        logger.exception("send_robot_status failed: %s", e)
        try:
            with open(ensure_robot_asset(), "rb") as f:
                return await update.message.reply_photo(photo=f, caption=caption)
        except Exception:
            await update.message.reply_text(caption)
            return None


async def send_robot_analysis_sequence(update: Update, meta: Dict[str, Any], state: Dict[str, Any]) -> None:
    msg = await send_robot_status(update, "🤖 ĐANG PHÂN TÍCH...")
    await asyncio.sleep(ROBOT_ANALYZE_DELAY)

    final_caption = build_final_message(meta, state)

    if msg:
        try:
            await msg.edit_caption(caption=final_caption)
        except Exception as e:
            logger.exception("edit_caption failed: %s", e)
            try:
                await update.message.reply_text(final_caption)
            except Exception:
                pass
    else:
        try:
            await update.message.reply_text(final_caption)
        except Exception:
            pass


async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, nums: Optional[List[int]] = None) -> None:
    if not await admin_only(update):
        return
    if not update.message:
        return

    chat_id = get_key(update)

    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id))
        entries: List[Tuple[int, str]] = []

        if nums:
            for n in nums:
                entries.append((n, map_value(n)))

        if entries:
            await append_history(chat_id, entries)

        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        full_values = [r[0] for r in rows]
        full_labels = [r[1] for r in rows]

        state["values"] = _safe_tail(full_values, RECENT_CACHE)
        state["labels"] = _safe_tail(full_labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, full_labels)

        if entries:
            latest_actual = entries[-1][1]
            update_prediction_feedback(state, latest_actual)
            prev_predictions = state.get("last_model_predictions", {})
            if isinstance(prev_predictions, dict) and prev_predictions:
                update_model_accuracy(state, prev_predictions, latest_actual)
            clear_loss_cooldown(state)

        result = analyze_state_from_labels(state, full_labels)
        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()

    report = result["report"]
    adv = result["adv"]

    await send_bridge_chart(update, full_labels, report, adv, "📈 BIỂU ĐỒ CẦU - CẬP NHẬT MỚI", state)

    if not result.get("allowed", False):
        await send_robot_status(
            update,
            f"⏸ BOT TẠM DỪNG\n"
            f"Lý do: {result.get('reason', 'Cầu chưa rõ')}"
        )
        return

    meta = result["meta"]
    await send_robot_analysis_sequence(update, meta, state)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    await update.message.reply_text(
        "📘 TRỢ GIÚP\n"
        f"/stats - xem bảng thống kê\n"
        f"/ai - phân tích cầu\n"
        f"/next - giống /ai\n"
        f"/reset - xóa dữ liệu chat hiện tại\n"
        f"/factory_reset - xóa sạch toàn bộ bot\n\n"
        f"Quy đổi: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}.\n"
        f"Khi thua {LOSS_STREAK_LIMIT} liên tiếp, bot sẽ tạm dừng, tự reset logic và học lại từ đầu lịch sử.\n"
        f"Luồng hoạt động: cập nhật thống kê → biểu đồ → robot phân tích → chốt cuối."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
    labels = [r[1] for r in rows]
    state["labels"] = _safe_tail(labels, RECENT_CACHE)
    rebuild_counters_from_labels(state, labels)
    report = build_report(labels)
    adv = advanced_metrics(labels)
    adv.update(extract_chart_features(labels))
    await send_bridge_chart(update, labels, report, adv, "📈 BIỂU ĐỒ CẦU - THỐNG KÊ MỚI NHẤT", state)
    await update.message.reply_text(build_stats_message(report, state, adv))


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)

    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id, force_reload=True))
        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        labels = [r[1] for r in rows]
        state["labels"] = _safe_tail(labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, labels)
        clear_loss_cooldown(state)
        result = analyze_state_from_labels(state, labels)
        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()

    report = result["report"]
    adv = result["adv"]

    await send_bridge_chart(update, labels, report, adv, "📈 BIỂU ĐỒ CẦU - DÙNG CHO PHÂN TÍCH", state)

    if not result.get("allowed", False):
        await send_robot_status(
            update,
            f"⏸ BOT TẠM DỪNG\n"
            f"Lý do: {result.get('reason', 'Cầu chưa rõ')}"
        )
        return

    meta = result["meta"]
    await send_robot_analysis_sequence(update, meta, state)


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    await ai_cmd(update, context)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return
    chat_id = get_key(update)

    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)

    users.pop(chat_id, None)
    await update.message.reply_text("🔄 Đã reset chat hiện tại.")


async def factory_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update):
        return
    if not update.message:
        return

    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history")
            conn.execute("DELETE FROM chat_state")
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)

    users.clear()
    await update.message.reply_text("🧼 Đã xóa sạch toàn bộ dữ liệu.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not await admin_only(update):
            return
        if not update.message or not update.message.text:
            return
        nums = parse_input(update.message.text)
        if not nums:
            return
        await process_chat(update, context, nums)
    except Exception as e:
        logger.exception("handle_text failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xử lý dữ liệu")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.exception("Global error: %s", err)

    if isinstance(err, Conflict):
        logger.error("Bot đang bị chạy trùng instance. Hãy tắt instance khác đang dùng cùng token.")
        return

    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after + 1)
    elif isinstance(err, (TimedOut, NetworkError, TelegramError)):
        await asyncio.sleep(1.0)

    try:
        if getattr(update, "message", None):
            await update.message.reply_text("⚠️ Có lỗi tạm thời, bot đã tự giữ an toàn.")
    except Exception:
        pass


def main():
    init_db()
    ensure_robot_asset()
    ensure_robot_animation()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("factory_reset", factory_reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🔥 BOT THỐNG KÊ - PHÂN TÍCH CẦU ĐANG CHẠY...")
    app.run_polling(drop_pending_updates=True)


def run_bot_forever():
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.exception("Bot crashed, restarting in 5s: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot_forever()
