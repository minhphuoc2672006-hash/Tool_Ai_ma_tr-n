#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import pickle
from typing import Dict, List, Tuple, Optional

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ================= ENV =================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

ADMIN_ID = env_int("ADMIN_ID", 0)

if not TOKEN:
    raise Exception("Thiếu BOT_TOKEN")

bot = telebot.TeleBot(TOKEN)

# ================= FILES / LIMITS =================
DATA_FILE = "data.txt"
STATE_FILE = "state.pkl"

MAX_HISTORY = 800
WINDOW_SIZES = [4, 5, 6, 7, 8, 9, 10, 11, 12]

MIN_CLUSTER_SIM = 0.72
MAX_CLUSTER_SAMPLES = 120
MIN_CLUSTER_COUNT_TO_SHOW = 2

MIN_NEXT_TX_TO_TRUST = 4
MIN_CLUSTER_EDGE = 0.15
MAX_SAME_SIDE_PRED = 2

# ================= MEMORY =================
raw_numbers: List[int] = []
tx_stream: List[Optional[int]] = []
valid_tx: List[int] = []

streaks: List[int] = []
streak_blocks: List[Tuple[int, int]] = []

state = {
    "markov": {},
    "clusters": {},
}

prediction_memory = {
    "label": None,
    "pct": 50.0,
    "cluster_sig": None,
    "hold_count": 0,
    "same_side_count": 0,
}

def reset_prediction_memory():
    prediction_memory["label"] = None
    prediction_memory["pct"] = 50.0
    prediction_memory["cluster_sig"] = None
    prediction_memory["hold_count"] = 0
    prediction_memory["same_side_count"] = 0

# ================= PERMISSION =================
def is_admin_user_id(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID

def is_admin_message(msg) -> bool:
    if not getattr(msg, "from_user", None):
        return False
    return is_admin_user_id(msg.from_user.id)

def is_admin_callback(call) -> bool:
    if not getattr(call, "from_user", None):
        return False
    return is_admin_user_id(call.from_user.id)

# ================= INPUT / CONVERT =================
def parse_input(text: str) -> List[int]:
    if not text:
        return []
    nums = re.findall(r"-?\d+", text)
    out = []
    for n in nums:
        try:
            out.append(int(n))
        except Exception:
            continue
    return out

def to_tx(num: int) -> Optional[int]:
    if num in (3, 18):
        return None
    if 11 <= num <= 17:
        return 1
    if 4 <= num <= 10:
        return 0
    return None

def tx_name(v: Optional[int]) -> str:
    if v == 1:
        return "TÀI"
    if v == 0:
        return "XỈU"
    return "BÃO"

def safe_percent(x: float) -> int:
    return max(0, min(100, int(round(x * 100))))

def pct_float(x: float, digits: int = 2) -> str:
    return f"{x * 100:.{digits}f}"

def cluster_display_state(score: float) -> str:
    """
    score là giá trị 0.0 -> 1.0
    0.5000 trở xuống: GIỮ
    0.5001 trở lên: vẫn dự đoán bình thường
    """
    if score <= 0.50:
        return "GIỮ"
    if score < 0.60:
        return "YẾU"
    if score < 0.75:
        return "KHÁ RÕ"
    return "RÕ"

# ================= STREAKS =================
def build_streak_blocks(seq: List[Optional[int]]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    current = None
    count = 0

    for v in seq:
        if v is None:
            if current is not None and count > 0:
                out.append((current, count))
            current = None
            count = 0
            continue

        if current is None:
            current = v
            count = 1
        elif v == current:
            count += 1
        else:
            out.append((current, count))
            current = v
            count = 1

    if current is not None and count > 0:
        out.append((current, count))

    return out

def build_streak_lengths(blocks: List[Tuple[int, int]]) -> List[int]:
    return [length for _, length in blocks]

# ================= CLUSTER ENGINE =================
def cluster_similarity(a: Tuple[int, ...], b: Tuple[int, ...]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0

    score = 0.0
    for x, y in zip(a, b):
        if x == y:
            score += 1.0
        elif abs(x - y) <= 1:
            score += 0.80
        elif abs(x - y) == 2:
            score += 0.55
    return score / len(a)

def compute_prototype(samples: List[Tuple[int, ...]]) -> Tuple[int, ...]:
    if not samples:
        return ()
    if len(samples) == 1:
        return tuple(samples[0])

    cols = list(zip(*samples))
    proto = []
    for col in cols:
        proto.append(int(round(sum(col) / len(col))))
    return tuple(proto)

def cluster_kind(proto: Tuple[int, ...]) -> str:
    if not proto:
        return "CHƯA_RÕ"

    if len(proto) == 1:
        return "NGẮN"

    diffs = [proto[i + 1] - proto[i] for i in range(len(proto) - 1)]
    signs = [0 if d == 0 else (1 if d > 0 else -1) for d in diffs]

    if all(x == proto[0] for x in proto):
        return "BỆT_ĐỀU"

    if all(d > 0 for d in diffs):
        return "TĂNG_DẦN"

    if all(d < 0 for d in diffs):
        return "GIẢM_DẦN"

    if all(s != 0 for s in signs) and all(signs[i] != signs[i + 1] for i in range(len(signs) - 1)):
        return "ĐẢO/SHORT-LONG"

    if signs.count(0) >= 1:
        return "LỆCH_NHẸ"

    return "HỖN_HỢP"

def merge_sample_into_clusters(clusters: Dict[str, dict], sample: Tuple[int, ...], seen_at: int, next_tx: Optional[int]):
    if not sample:
        return

    best_key = None
    best_score = 0.0

    for key, info in clusters.items():
        score = cluster_similarity(sample, info["prototype"])
        if score > best_score:
            best_score = score
            best_key = key

    if best_key is not None and best_score >= MIN_CLUSTER_SIM:
        info = clusters[best_key]
        info["count"] += 1
        info["last_seen"] = seen_at
        info["samples"].append(tuple(sample))
        if len(info["samples"]) > MAX_CLUSTER_SAMPLES:
            info["samples"] = info["samples"][-MAX_CLUSTER_SAMPLES:]
        info["prototype"] = compute_prototype(info["samples"])
        info["kind"] = cluster_kind(info["prototype"])

        if next_tx is not None:
            info["next_tx"][next_tx] += 1
        return

    new_id = f"C{len(clusters) + 1:03d}"
    clusters[new_id] = {
        "prototype": tuple(sample),
        "samples": [tuple(sample)],
        "count": 1,
        "kind": cluster_kind(sample),
        "first_seen": seen_at,
        "last_seen": seen_at,
        "next_tx": [0, 0],  # [X, T]
    }

    if next_tx is not None:
        clusters[new_id]["next_tx"][next_tx] += 1

def build_clusters_from_blocks(blocks: List[Tuple[int, int]]) -> Dict[int, Dict[str, dict]]:
    lengths = [length for _, length in blocks]
    next_vals = [tx for tx, _ in blocks]

    clusters_by_window = {}

    for win in WINDOW_SIZES:
        clusters = {}
        if len(lengths) >= win:
            for i in range(len(lengths) - win + 1):
                sample = tuple(lengths[i:i + win])
                next_tx = next_vals[i + win] if (i + win) < len(next_vals) else None
                merge_sample_into_clusters(clusters, sample, i, next_tx)
        clusters_by_window[win] = clusters

    return clusters_by_window

# ================= PERSISTENCE =================
def save_data_append(nums: List[int]):
    with open(DATA_FILE, "a", encoding="utf-8") as f:
        for n in nums:
            f.write(f"{n}\n")

def save_state():
    with open(STATE_FILE, "wb") as f:
        pickle.dump({"raw_numbers": raw_numbers}, f)

def load_state_file():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None

def load_history_from_file() -> List[int]:
    nums = []
    if not os.path.exists(DATA_FILE):
        return nums
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            for line in f:
                for n in parse_input(line):
                    if 3 <= n <= 18:
                        nums.append(n)
    except Exception:
        pass
    return nums

# ================= REBUILD =================
def rebuild_all():
    global tx_stream, valid_tx, streaks, streak_blocks, state

    tx_stream = [to_tx(n) for n in raw_numbers]
    valid_tx = [v for v in tx_stream if v is not None]

    streak_blocks = build_streak_blocks(tx_stream)
    streaks = build_streak_lengths(streak_blocks)

    markov = {}
    if len(valid_tx) >= 4:
        for i in range(len(valid_tx) - 3):
            key = tuple(valid_tx[i:i + 3])
            nxt = valid_tx[i + 3]
            if key not in markov:
                markov[key] = [0, 0]
            markov[key][nxt] += 1

    clusters = build_clusters_from_blocks(streak_blocks)

    state = {
        "markov": markov,
        "clusters": clusters,
    }

# ================= ANALYSIS =================
def get_current_cluster():
    if len(streaks) < min(WINDOW_SIZES):
        return None

    candidates = []

    for win in WINDOW_SIZES:
        if len(streaks) < win:
            continue

        sample = tuple(streaks[-win:])
        clusters = state.get("clusters", {}).get(win, {})
        best_key = None
        best_score = 0.0

        for key, info in clusters.items():
            score = cluster_similarity(sample, info["prototype"])
            if score > best_score:
                best_score = score
                best_key = key

        if best_key is not None:
            info = clusters[best_key]
            candidates.append({
                "window": win,
                "id": best_key,
                "sample": sample,
                "prototype": info["prototype"],
                "kind": info["kind"],
                "score": best_score,
                "count": info["count"],
                "next_tx": info.get("next_tx", [0, 0]),
            })
        else:
            candidates.append({
                "window": win,
                "id": "NEW",
                "sample": sample,
                "prototype": sample,
                "kind": cluster_kind(sample),
                "score": 0.0,
                "count": 0,
                "next_tx": [0, 0],
            })

    if not candidates:
        return None

    return max(candidates, key=lambda x: (x["score"], x["window"]))

def predict_markov():
    if len(valid_tx) < 3:
        return None, 0.0

    key = tuple(valid_tx[-3:])
    counts = state.get("markov", {}).get(key)
    if not counts:
        return None, 0.0

    x_count, t_count = counts[0], counts[1]
    total = x_count + t_count
    if total < 3:
        return None, 0.0

    p_t = t_count / total
    p_x = x_count / total

    if abs(p_t - p_x) < 0.08:
        return None, 0.5

    return (1, p_t) if p_t > p_x else (0, p_x)

def analyze_status():
    if not valid_tx:
        return "Chưa đủ dữ liệu", 0.0

    cluster = get_current_cluster()
    _, ai_conf = predict_markov()

    score = 0.0
    if cluster:
        score += cluster["score"] * 0.75

        next_x, next_t = cluster.get("next_tx", [0, 0])
        total_next = next_x + next_t
        if total_next >= MIN_NEXT_TX_TO_TRUST:
            p_t = next_t / total_next
            p_x = next_x / total_next
            if abs(p_t - p_x) >= MIN_CLUSTER_EDGE:
                score += 0.20
            else:
                score += 0.05
        elif cluster["kind"] in ("BỆT_ĐỀU", "ĐẢO/SHORT-LONG"):
            score += 0.10

    score += ai_conf * 0.10

    if len(valid_tx) >= 10:
        bias = sum(valid_tx) / len(valid_tx)
        if bias > 0.68 or bias < 0.32:
            score += 0.03

    score = max(0.0, min(1.0, score))

    if score < 0.35:
        return "Nhiễu mạnh", score
    if score < 0.60:
        return "Trung bình", score
    return "Cụm rõ", score

def cluster_primary_direction(cluster) -> Optional[int]:
    if not cluster:
        return None

    next_x, next_t = cluster.get("next_tx", [0, 0])
    total = next_x + next_t

    if total >= MIN_NEXT_TX_TO_TRUST:
        p_t = next_t / total
        p_x = next_x / total
        if abs(p_t - p_x) >= MIN_CLUSTER_EDGE:
            return 1 if p_t > p_x else 0

    kind = cluster.get("kind")
    if kind not in ("BỆT_ĐỀU", "ĐẢO/SHORT-LONG"):
        return None

    if not valid_tx:
        return None

    last = valid_tx[-1]

    if kind == "BỆT_ĐỀU":
        return 0 if last == 1 else 1

    if kind == "ĐẢO/SHORT-LONG":
        return 1 if last == 1 else 0

    return None

def make_prediction(stateful: bool = False):
    """
    Cụm là chính, IT/Markov chỉ phụ.
    Yếu vẫn dự đoán bình thường.
    Chỉ thêm cân bằng nhẹ để tránh nghiêng một phía quá nhiều.
    """
    cluster = get_current_cluster()
    ai_pred, ai_conf = predict_markov()

    score_t = 0.0
    score_x = 0.0

    # ===== CỤM LÀ CHÍNH =====
    if cluster:
        kind = cluster["kind"]
        sim = cluster["score"]
        next_x, next_t = cluster.get("next_tx", [0, 0])
        total_next = next_x + next_t

        if sim >= 0.90:
            weight = 2.0
        elif sim >= 0.85:
            weight = 1.5
        elif sim >= 0.80:
            weight = 1.0
        else:
            weight = 0.55

        if total_next >= MIN_NEXT_TX_TO_TRUST:
            p_t = next_t / total_next
            p_x = next_x / total_next

            # 50.07 vẫn là trên 50, không bị giữ.
            if abs(p_t - p_x) >= MIN_CLUSTER_EDGE:
                score_t += 3.0 * weight * p_t
                score_x += 3.0 * weight * p_x
            else:
                score_t += 0.35 * weight * p_t
                score_x += 0.35 * weight * p_x
        else:
            primary = cluster_primary_direction(cluster)
            if kind in ("BỆT_ĐỀU", "ĐẢO/SHORT-LONG") and primary is not None:
                if primary == 1:
                    score_t += 2.2 * weight
                    score_x += 0.25 * weight
                else:
                    score_x += 2.2 * weight
                    score_t += 0.25 * weight
            else:
                score_t += 0.45 * weight
                score_x += 0.45 * weight

    # ===== IT / MARKOV CHỈ PHỤ =====
    if ai_pred is not None:
        markov_weight = 0.12
        if cluster and cluster["score"] >= 0.88 and cluster["kind"] in ("BỆT_ĐỀU", "ĐẢO/SHORT-LONG"):
            markov_weight = 0.05

        if ai_pred == 1:
            score_t += markov_weight * ai_conf
        else:
            score_x += markov_weight * ai_conf

    # ===== BIAS CHUNG RẤT NHẸ =====
    if len(valid_tx) >= 10:
        bias = sum(valid_tx) / len(valid_tx)
        if bias > 0.68:
            score_x += 0.05
        elif bias < 0.32:
            score_t += 0.05

    total = score_t + score_x
    if total <= 0:
        return "KHÔNG RÕ", 50.0

    p_t = score_t / total
    p_x = score_x / total

    # ===== CÂN BẰNG ĐỂ KHÔNG NGHIÊNG MỘT PHÍA =====
    if stateful:
        last = prediction_memory.get("label")
        same_side = prediction_memory.get("same_side_count", 0)

        current_label = "TÀI" if p_t >= p_x else "XỈU"

        if last == current_label:
            same_side += 1
        else:
            same_side = 1

        prediction_memory["same_side_count"] = same_side

        # chỉ giảm nhẹ, không ép về KHÔNG RÕ
        if same_side >= 3 and abs(p_t - p_x) < 0.20:
            if current_label == "TÀI":
                p_t *= 0.92
                p_x *= 1.05
            else:
                p_x *= 0.92
                p_t *= 1.05

            total = p_t + p_x
            if total > 0:
                p_t /= total
                p_x /= total

    best = max(p_t, p_x)

    if p_t > p_x:
        label = "TÀI"
        pct = round(p_t * 100, 1)
    else:
        label = "XỈU"
        pct = round(p_x * 100, 1)

    # Nếu rất yếu thì vẫn ra kèo bình thường, chỉ hiển thị tỉ lệ thấp hơn
    if best < 0.52:
        pct = round(best * 100, 1)

    if stateful and label != "KHÔNG RÕ":
        prediction_memory["label"] = label
        prediction_memory["pct"] = pct

    return label, pct

def clusters_summary(max_items=6):
    lines = []
    clusters_by_window = state.get("clusters", {})

    for win in WINDOW_SIZES:
        clusters = clusters_by_window.get(win, {})
        if not clusters:
            lines.append(f"• Window {win}: chưa có cụm")
            continue

        top = sorted(clusters.items(), key=lambda kv: kv[1]["count"], reverse=True)
        top = [item for item in top if item[1]["count"] >= MIN_CLUSTER_COUNT_TO_SHOW][:max_items]

        lines.append(f"• Window {win}: {len(clusters)} cụm")
        if not top:
            lines.append("  - Chưa có cụm đủ mạnh để hiển thị")
            continue

        for cid, info in top:
            proto = "-".join(map(str, info["prototype"]))
            nx, nt = info.get("next_tx", [0, 0])
            lines.append(f"  - {cid} | {info['kind']} | n={info['count']} | {proto} | next(X/T)={nx}/{nt}")

    return "\n".join(lines)

def dashboard_text():
    cluster = get_current_cluster()
    status, score = analyze_status()
    ai_pred, ai_conf = predict_markov()
    prediction_label, prediction_pct = make_prediction(stateful=False)

    ai_text = "??"
    if ai_pred == 1:
        ai_text = "TÀI"
    elif ai_pred == 0:
        ai_text = "XỈU"

    cluster_text = "Chưa đủ dữ liệu để nhận cụm."
    if cluster:
        nx, nt = cluster.get("next_tx", [0, 0])
        cluster_score = cluster["score"]
        cluster_state = cluster_display_state(cluster_score)

        cluster_text = (
            f"┏━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    🧩 CỤM HIỆN TẠI   ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━┛\n"
            f"• Window: <b>{cluster['window']}</b>\n"
            f"• Mã cụm: <b>{cluster['id']}</b>\n"
            f"• Loại: <b>{cluster['kind']}</b>\n"
            f"• Trạng thái: <b>{cluster_state}</b>\n"
            f"• Mẫu: <b>{cluster['sample']}</b>\n"
            f"• Đại diện: <b>{cluster['prototype']}</b>\n"
            f"• Điểm cụm: <b>{pct_float(cluster_score)}%</b>\n"
            f"• Số lần gặp: <b>{cluster['count']}</b>\n"
            f"• Next(X/T): <b>{nx}/{nt}</b>"
        )

    last_num = raw_numbers[-1] if raw_numbers else None
    last_tx = tx_name(tx_stream[-1]) if tx_stream else "N/A"

    return (
        f"🤖 <b>AI Pattern Tool</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 Số cuối: <b>{last_num if last_num is not None else 'N/A'}</b> ({last_tx})\n\n"
        f"{cluster_text}\n\n"
        f"┏━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃     📊 KẾT QUẢ AI    ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━┛\n"
        f"• Trạng thái: <b>{status}</b>\n"
        f"• Điểm tổng: <b>{safe_percent(score)}%</b>\n"
        f"• IT/Markov: <b>{ai_text}</b> ({safe_percent(ai_conf)}%)\n"
        f"• Dự đoán: <b>{prediction_label}</b>\n"
        f"• Tỷ lệ: <b>{prediction_pct}%</b>\n\n"
        f"📚 Tổng phiên: <b>{len(raw_numbers)}</b>\n"
        f"🎯 T/X hợp lệ: <b>{len(valid_tx)}</b>\n"
        f"🧱 Streak gần nhất: <b>{streaks[-8:] if streaks else []}</b>"
    )

# ================= UI =================
def main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📌 Dashboard", callback_data="menu_panel"),
        InlineKeyboardButton("➕ Nhập dữ liệu", callback_data="menu_input"),
        InlineKeyboardButton("📊 Thống kê", callback_data="menu_stats"),
        InlineKeyboardButton("🧩 Cụm", callback_data="menu_clusters"),
        InlineKeyboardButton("🔄 Train", callback_data="menu_train"),
        InlineKeyboardButton("🧹 Reset", callback_data="menu_reset"),
        InlineKeyboardButton("ℹ️ Hướng dẫn", callback_data="menu_help"),
    )
    return kb

def confirm_reset_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Xác nhận reset", callback_data="reset_yes"),
        InlineKeyboardButton("❌ Hủy", callback_data="reset_no"),
    )
    return kb

# ================= COMMANDS =================
@bot.message_handler(commands=["start"])
def start(msg):
    if not is_admin_message(msg):
        return
    bot.send_message(
        msg.chat.id,
        "BOT AI PRO đã sẵn sàng.\n"
        "Nhập dữ liệu thật theo dạng: 11-10-13-8-12-6\n"
        "Bot sẽ tự lưu, tự chia cụm ngắn/dài, và hiển thị dashboard.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["menu"])
def menu(msg):
    if not is_admin_message(msg):
        return
    bot.send_message(msg.chat.id, "Chọn chức năng bên dưới:", reply_markup=main_menu())

@bot.message_handler(commands=["panel"])
def panel(msg):
    if not is_admin_message(msg):
        return
    bot.send_message(
        msg.chat.id,
        dashboard_text(),
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

@bot.message_handler(commands=["help"])
def help_cmd(msg):
    if not is_admin_message(msg):
        return
    bot.send_message(
        msg.chat.id,
        "Cách dùng:\n"
        "• Nhập dữ liệu thật: 11-10-13-8-12\n"
        "• /panel xem dashboard\n"
        "• /stats xem thống kê\n"
        "• /clusters xem các cụm\n"
        "• /train rebuild model\n"
        "• /reset xóa toàn bộ dữ liệu\n\n"
        "Số 3 và 18 sẽ được tách riêng như bão.",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["stats"])
def stats(msg):
    if not is_admin_message(msg):
        return
    cluster_count = sum(len(v) for v in state.get("clusters", {}).values())
    bot.send_message(
        msg.chat.id,
        f"📊 <b>Thống kê</b>\n\n"
        f"• Tổng số đã nhập: <b>{len(raw_numbers)}</b>\n"
        f"• T/X hợp lệ: <b>{len(valid_tx)}</b>\n"
        f"• Số cụm đang giữ: <b>{cluster_count}</b>\n"
        f"• Window cụm: <b>{WINDOW_SIZES}</b>\n"
        f"• Streak gần nhất: <b>{streaks[-10:] if streaks else []}</b>",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

@bot.message_handler(commands=["clusters"])
def clusters_cmd(msg):
    if not is_admin_message(msg):
        return
    text = clusters_summary(max_items=6)
    bot.send_message(
        msg.chat.id,
        f"🧩 <b>Danh sách cụm</b>\n\n{text}",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

@bot.message_handler(commands=["train"])
def train_cmd(msg):
    if not is_admin_message(msg):
        return
    rebuild_all()
    save_state()
    reset_prediction_memory()
    cluster_count = sum(len(v) for v in state.get("clusters", {}).values())
    bot.send_message(
        msg.chat.id,
        f"✅ Train xong.\n"
        f"• Tổng phiên: {len(raw_numbers)}\n"
        f"• Số cụm: {cluster_count}",
        reply_markup=main_menu()
    )

@bot.message_handler(commands=["reset"])
def reset_cmd(msg):
    if not is_admin_message(msg):
        return
    bot.send_message(msg.chat.id, "Bạn có chắc muốn xóa toàn bộ dữ liệu?", reply_markup=confirm_reset_menu())

# ================= CALLBACKS =================
@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    if not is_admin_callback(call):
        bot.answer_callback_query(call.id, "Không có quyền", show_alert=True)
        return

    data = call.data

    try:
        if data == "menu_panel":
            bot.edit_message_text(
                dashboard_text(),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu(),
                parse_mode="HTML"
            )

        elif data == "menu_input":
            bot.edit_message_text(
                "➕ Nhập dữ liệu thật vào chat theo dạng:\n"
                "11-10-13-8-12-6\n\n"
                "Bot sẽ tự học cụm ngắn và dài.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )

        elif data == "menu_stats":
            cluster_count = sum(len(v) for v in state.get("clusters", {}).values())
            bot.edit_message_text(
                f"📊 <b>Thống kê</b>\n\n"
                f"• Tổng số đã nhập: <b>{len(raw_numbers)}</b>\n"
                f"• T/X hợp lệ: <b>{len(valid_tx)}</b>\n"
                f"• Số cụm đang giữ: <b>{cluster_count}</b>\n"
                f"• Window cụm: <b>{WINDOW_SIZES}</b>\n"
                f"• Streak gần nhất: <b>{streaks[-10:] if streaks else []}</b>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu(),
                parse_mode="HTML"
            )

        elif data == "menu_clusters":
            text = clusters_summary(max_items=6)
            bot.edit_message_text(
                f"🧩 <b>Danh sách cụm</b>\n\n{text}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu(),
                parse_mode="HTML"
            )

        elif data == "menu_train":
            rebuild_all()
            save_state()
            reset_prediction_memory()
            cluster_count = sum(len(v) for v in state.get("clusters", {}).values())
            bot.edit_message_text(
                f"✅ Train xong.\n"
                f"• Tổng phiên: {len(raw_numbers)}\n"
                f"• Số cụm: {cluster_count}",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )

        elif data == "menu_reset":
            bot.edit_message_text(
                "Bạn có chắc muốn xóa toàn bộ dữ liệu?",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=confirm_reset_menu()
            )

        elif data == "reset_yes":
            raw_numbers.clear()
            tx_stream.clear()
            valid_tx.clear()
            streaks.clear()
            streak_blocks.clear()
            state["markov"] = {}
            state["clusters"] = {}
            reset_prediction_memory()

            try:
                if os.path.exists(DATA_FILE):
                    os.remove(DATA_FILE)
            except Exception:
                pass

            try:
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
            except Exception:
                pass

            bot.edit_message_text(
                "🧹 Đã reset toàn bộ dữ liệu.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )

        elif data == "reset_no":
            bot.edit_message_text(
                "Đã hủy reset.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )

        elif data == "menu_help":
            bot.edit_message_text(
                "Cách dùng:\n"
                "• Nhập dữ liệu thật: 11-10-13-8-12\n"
                "• /panel xem dashboard\n"
                "• /stats xem thống kê\n"
                "• /clusters xem các cụm\n"
                "• /train rebuild model\n"
                "• /reset xóa toàn bộ dữ liệu\n\n"
                "Số 3 và 18 sẽ được tách riêng như bão.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_menu()
            )

        bot.answer_callback_query(call.id)

    except Exception:
        bot.answer_callback_query(call.id, "Đã có lỗi nhỏ, thử lại.")

# ================= INPUT HANDLER =================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(msg):
    if not is_admin_message(msg):
        return

    text = msg.text or ""
    if text.startswith("/"):
        return

    nums = parse_input(text)
    valid_nums = [n for n in nums if 3 <= n <= 18]

    if not valid_nums:
        bot.reply_to(msg, "Chỉ nhận số từ 3 đến 18. Ví dụ: 11-10-13-8")
        return

    raw_numbers.extend(valid_nums)
    if len(raw_numbers) > MAX_HISTORY:
        raw_numbers[:] = raw_numbers[-MAX_HISTORY:]

    save_data_append(valid_nums)
    rebuild_all()
    save_state()

    cluster = get_current_cluster()
    status, score = analyze_status()
    ai_pred, ai_conf = predict_markov()
    prediction_label, prediction_pct = make_prediction(stateful=True)

    ai_text = "??"
    if ai_pred == 1:
        ai_text = "TÀI"
    elif ai_pred == 0:
        ai_text = "XỈU"

    if cluster:
        nx, nt = cluster.get("next_tx", [0, 0])
        cluster_score = cluster["score"]
        cluster_state = cluster_display_state(cluster_score)
        cluster_text = (
            f"┏━━━━━━━━━━━━━━━━━━━━━┓\n"
            f"┃    🧩 CỤM HIỆN TẠI   ┃\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━┛\n"
            f"• Window: <b>{cluster['window']}</b>\n"
            f"• Mã cụm: <b>{cluster['id']}</b>\n"
            f"• Loại: <b>{cluster['kind']}</b>\n"
            f"• Trạng thái: <b>{cluster_state}</b>\n"
            f"• Mẫu: <b>{cluster['sample']}</b>\n"
            f"• Đại diện: <b>{cluster['prototype']}</b>\n"
            f"• Điểm cụm: <b>{pct_float(cluster_score)}%</b>\n"
            f"• Số lần gặp: <b>{cluster['count']}</b>\n"
            f"• Next(X/T): <b>{nx}/{nt}</b>"
        )
    else:
        cluster_text = "Chưa đủ dữ liệu để nhận cụm."

    last_num = valid_nums[-1]
    last_tx = tx_name(to_tx(last_num))

    bot.send_message(
        msg.chat.id,
        f"✅ <b>Đã nhận:</b> <b>{last_num}</b> ({last_tx})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{cluster_text}\n\n"
        f"┏━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃     📊 KẾT QUẢ AI    ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━┛\n"
        f"• Trạng thái: <b>{status}</b>\n"
        f"• Điểm tổng: <b>{safe_percent(score)}%</b>\n"
        f"• IT/Markov: <b>{ai_text}</b> ({safe_percent(ai_conf)}%)\n"
        f"• Dự đoán: <b>{prediction_label}</b>\n"
        f"• Tỷ lệ: <b>{prediction_pct}%</b>\n\n"
        f"📚 Tổng phiên: <b>{len(raw_numbers)}</b>\n"
        f"🎯 T/X hợp lệ: <b>{len(valid_tx)}</b>",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

# ================= BOOTSTRAP =================
def bootstrap():
    global raw_numbers

    saved = load_state_file()
    if saved and isinstance(saved.get("raw_numbers"), list):
        raw_numbers = [n for n in saved["raw_numbers"] if isinstance(n, int) and 3 <= n <= 18]
    else:
        raw_numbers = load_history_from_file()

    rebuild_all()
    reset_prediction_memory()

bootstrap()

# ================= RUN =================
def run_bot():
    try:
        bot.delete_webhook()
    except Exception:
        try:
            bot.remove_webhook()
        except Exception:
            pass

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
        except Exception as e:
            print("BOT ERROR:", e)
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
