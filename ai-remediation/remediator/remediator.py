#!/usr/bin/env python3
"""
Remediator: thực thi lệnh sửa lỗi do AI ĐỀ XUẤT nhưng chỉ sau khi NGƯỜI duyệt trên Telegram.

Luồng:
  1. holmes-bridge gọi POST /propose {alert, fingerprint, action_id, container, reason}
  2. Kiểm tra theo policy.json (action + container). Sai -> từ chối, ghi audit.
  3. Hợp lệ -> gửi Telegram kèm 2 nút Duyệt / Từ chối (callback ký HMAC, hết hạn, dùng 1 lần).
  4. Người bấm Duyệt (đúng chat/người được phép) -> chạy qua docker-socket-proxy
     (hoặc chỉ ghi "sẽ chạy gì" khi REMEDIATION_MODE=dry-run, là mặc định).
  5. Mọi bước ghi vào audit (JSONL + stdout).
  6. VÒNG LẶP: lệnh chạy xong mà hệ thống chưa ổn (hoặc vừa xem log) -> gửi kết quả cho
     holmes-bridge (POST /continue) để AI đề xuất bước kế tiếp, tối đa AI_MAX_ROUNDS vòng.
     Thoát vòng: nút ⏹ Dừng, lệnh /stop, cảnh báo RESOLVED (bridge gọi POST /close), hết vòng.
  7. Policy chặn -> báo kèm "lệnh thay thế" lấy từ mục `alternatives` của policy.json
     (mỗi gợi ý vẫn phải qua kiểm tra policy và vẫn cần người bấm Duyệt).
  8. TOOL API cho AI (cách 2): AI gọi công cụ thay vì viết chữ. GET /tool/catalog, POST /tool/read
     (action chỉ đọc, chạy ngay, kết quả được LỌC trường), POST /tool/propose (action thay đổi:
     tạo nút Duyệt, trả JSON để AI biết ngay bị chặn hay không), GET /tool/session. Xác thực
     Bearer REMEDIATOR_TOOL_TOKEN; trống = tắt. AI vẫn không chạy được lệnh: luật + người duyệt giữ nguyên.

AI không bao giờ chạy lệnh: nó chỉ chọn action_id trong policy. Lệnh là mẫu cố định
trong policy.json, container phải nằm trong danh sách cho phép.
Chỉ dùng thư viện chuẩn Python. Không in token ra log.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VN = timezone(timedelta(hours=7))


def _secret(name):
    return re.sub(r"[\s\"']", "", os.environ.get(name, ""))


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PORT = _int("REMEDIATOR_PORT", 8090)
POLICY_FILE = os.environ.get("POLICY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.json"))
AUDIT_FILE = os.environ.get("AUDIT_FILE", "/audit/audit.jsonl")
DOCKER_URL = (os.environ.get("DOCKER_URL") or "http://docker-proxy:2375").rstrip("/")
PROM_URL = (os.environ.get("PROMETHEUS_URL") or "http://prometheus:9090").rstrip("/")
TG_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = _secret("TELEGRAM_CHAT_ID")
TG_API = (os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
# Chỉ những Telegram user id này được bấm Duyệt (để trống = ai trong chat cũng được)
APPROVERS = {s.strip() for s in os.environ.get("REMEDIATOR_APPROVER_IDS", "").split(",") if s.strip()}
# Chat nhóm có id âm. Nhóm mà KHÔNG khai báo REMEDIATOR_APPROVER_IDS thì mọi thành viên đều duyệt/chạy được lệnh -> mặc định
# CHẶN hết (đóng chặt). Chỉ mở khi cố ý đặt REMEDIATOR_ALLOW_ANY_GROUP_MEMBER=true.
ALLOW_ANY_GROUP_MEMBER = os.environ.get("REMEDIATOR_ALLOW_ANY_GROUP_MEMBER", "false").strip().lower() in ("true", "1", "yes", "on")
# dry-run (mặc định): chỉ ghi "sẽ chạy gì". execute: chạy thật qua docker-socket-proxy.
MODE = "execute" if os.environ.get("REMEDIATION_MODE", "dry-run").strip().lower() == "execute" else "dry-run"
# Công tắc tắt khẩn cấp: false = từ chối mọi đề xuất mới
ENABLED = os.environ.get("REMEDIATION_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")
# Địa chỉ holmes-bridge để gửi kết quả lệnh cho AI (vòng lặp). Trống = tắt vòng lặp.
BRIDGE_URL = (os.environ.get("BRIDGE_URL") or "").rstrip("/")
MAX_ROUNDS = max(1, _int("AI_MAX_ROUNDS", 3))
POST_CHECK_DELAY = _int("REMEDIATOR_POST_CHECK_DELAY_SECONDS", 15)
# Khoá ký nút bấm. Không đặt thì sinh ngẫu nhiên mỗi lần khởi động (nút cũ mất hiệu lực khi restart).
SECRET = (_secret("REMEDIATOR_SECRET") or secrets.token_hex(32)).encode()

# Token cho TOOL API của AI (holmes-bridge). Trống = tắt hẳn /tool/*.
TOOL_TOKEN = _secret("REMEDIATOR_TOOL_TOKEN")
# Phiên làm việc với AI (/ai): trong phiên mọi tin nhắn thường đều đi tới AI (có trí nhớ ở bridge); im lặng quá thời gian này thì tự đóng
AI_SESSION_IDLE = _int("AI_SESSION_IDLE_SECONDS", 900)
TOOL_MAX_READS = _int("TOOL_MAX_READS_PER_SESSION", 12)
TOOL_MAX_DENIED = _int("TOOL_MAX_DENIED_PER_SESSION", 4)

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")


def is_authorized(cid, uid):
    """Người này (uid) trong chat này (cid) có được ra lệnh / bấm Duyệt không.
    - Chỉ đúng TELEGRAM_CHAT_ID.
    - Có REMEDIATOR_APPROVER_IDS: chỉ những người đó.
    - Không có: chat riêng (id dương = chính người dùng) thì được; chat NHÓM (id âm) thì chặn, trừ khi cố ý mở."""
    if not TG_CHAT or str(cid) != TG_CHAT:
        return False
    if APPROVERS:
        return str(uid) in APPROVERS
    return not TG_CHAT.startswith("-") or ALLOW_ANY_GROUP_MEMBER


def group_without_approvers():
    return TG_CHAT.startswith("-") and not APPROVERS and not ALLOW_ANY_GROUP_MEMBER

try:  # Console Windows mặc định không phải UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remediator")

lock = threading.Lock()
pending = {}    # id -> proposal
seen = {}       # (fingerprint, action, container) -> thời điểm, chống đề xuất lặp
history = {}    # (action, container) -> deque thời điểm đã chạy, dùng cho cooldown/giới hạn giờ
ai_session = {"sid": None, "last": 0.0}      # phiên /ai đang mở (1 phiên cho cả chat)
recent_actions = deque(maxlen=10)           # vài lệnh thay đổi gần nhất (AI chat đọc qua /tool/state để hiểu "cái đó", "làm lại")
tool_sessions = {}  # (fingerprint, round) -> {"queued": n, "denied": n, "reads": n, "ts": t}  (giới hạn mỗi phiên AI)
loops = {}      # fingerprint -> {"alert", "round", "history": [...], "ts", "state": active|done|stopped}


# ---------------------------------------------------------------- policy
POLICY_FOR_STATE = {}   # gán trong main(): tool_state cần approval_ttl_seconds


def load_policy(path=POLICY_FILE):
    with open(path, encoding="utf-8") as f:
        p = json.load(f)
    for key in ("containers_allowed", "containers_forbidden", "actions"):
        if key not in p:
            raise ValueError(f"policy thiếu mục {key}")
    return p


def check_proposal(policy, action_id, container):
    """(ok, lý_do). Từ chối theo nguyên tắc: không khai báo thì không được."""
    if not isinstance(action_id, str) or not isinstance(container, str):
        return False, "action_id/container sai kiểu dữ liệu"
    if action_id not in policy["actions"]:
        return False, f"action '{action_id[:40]}' không có trong policy"
    if not NAME_RE.fullmatch(container):
        return False, "tên container không hợp lệ"
    # Ngoại lệ HẸP: action có khai báo `allow_on_forbidden` thì được chạy trên đúng các container đó
    # dù chúng nằm trong containers_forbidden (vd chỉ cho BẬT LẠI ecommerce-db đã dừng, vẫn cấm restart/logs).
    exception = container in policy["actions"][action_id].get("allow_on_forbidden", [])
    if container in policy["containers_forbidden"] and not exception:
        return False, f"container '{container}' bị cấm đụng tới"
    if container not in policy["containers_allowed"] and not exception:
        return False, f"container '{container}' không nằm trong danh sách cho phép"
    return True, ""


def check_rate(policy, action_id, container, now=None):
    """(ok, lý_do) theo cooldown_seconds và max_per_hour của action. Không ghi nhận lần chạy."""
    now = time.time() if now is None else now
    spec = policy["actions"][action_id]
    h = history.setdefault((action_id, container), deque())
    while h and now - h[0] > 3600:
        h.popleft()
    if len(h) >= spec.get("max_per_hour", 1):
        return False, f"đã chạy {len(h)} lần trong 1 giờ (giới hạn {spec.get('max_per_hour', 1)})"
    cd = spec.get("cooldown_seconds", 0)
    if h and now - h[-1] < cd:
        return False, f"đang trong thời gian chờ {cd}s giữa 2 lần chạy"
    return True, ""


def record_run(action_id, container, now=None):
    history.setdefault((action_id, container), deque()).append(time.time() if now is None else now)


def alternatives_for(policy, container, action_id=None):
    """Gợi ý lệnh thay thế khi đề xuất bị chặn. Lấy từ mục `alternatives` của policy.json
    (khoá = container bị chặn, hoặc "_default"; "{container}" = container AI vừa đề xuất).
    Mỗi gợi ý vẫn phải qua check_proposal + check_rate: không bao giờ gợi ý thứ policy sẽ chặn."""
    table = policy.get("alternatives") or {}
    items = table.get(container) if isinstance(container, str) and container in table else table.get("_default", [])
    out, seen_alt = [], set()
    for it in items:
        c = it.get("container", "").replace("{container}", container if isinstance(container, str) else "")
        a = it.get("action_id")
        if (a, c) in seen_alt or (a, c) == (action_id, container):
            continue
        if check_proposal(policy, a, c)[0] and check_rate(policy, a, c)[0]:
            seen_alt.add((a, c))
            out.append({"action_id": a, "container": c, "why": str(it.get("why", ""))[:120]})
    return out[:3]


def no_ai_loop(p):
    """Lệnh gõ tay hoặc do AI CHAT đề xuất (fingerprint chat-...) không thuộc sự cố nào trong holmes-bridge:
    không gửi /continue, không nút "Hỏi AI cách khác", không báo "đợi cảnh báo tự hết"."""
    return bool(p.get("manual")) or str(p.get("fingerprint", "")).startswith("chat-")


def loop_of(fp, alert="?"):
    """Trạng thái vòng lặp của 1 sự cố (tạo nếu chưa có). Gọi khi đang giữ `lock`."""
    return loops.setdefault(fp, {"alert": alert, "round": 0, "history": [], "ts": time.time(), "state": "active"})


# ---------------------------------------------------------------- ký nút bấm
def sign(pid, kind=""):
    """Ký cả loại nút + mã đề xuất: không đổi được nút "Từ chối" thành "Duyệt" bằng cách sửa dữ liệu nút."""
    return hmac.new(SECRET, f"{kind}:{pid}".encode(), hashlib.sha256).hexdigest()[:16]


def callback_data(kind, pid):
    return f"{kind}:{pid}:{sign(pid, kind)}"  # <= 64 byte theo giới hạn Telegram


def parse_callback(data):
    """Trả (kind, pid) nếu chữ ký đúng, ngược lại None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] not in ("a", "r", "n", "s"):
        return None
    if not hmac.compare_digest(parts[2], sign(parts[1], parts[0])):
        return None
    return parts[0], parts[1]


# ---------------------------------------------------------------- che dữ liệu nhạy cảm (log -> Telegram, AI/Bedrock, audit)
REDACT_RULES = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),                                       # khoá AWS
    (re.compile(r"\bABSK[A-Za-z0-9+/=_-]{16,}"), "ABSK***"),                                 # khoá Bedrock API
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),                  # token bot Telegram
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization|cookie|sid)\b(\"?\s*[:=]\s*\"?)[^\s\"',;&]{3,}"),
     r"\1\2***"),
    (re.compile(r"\b([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9-]+\.[A-Za-z0-9.-]+)\b"), r"***@\2"),  # email khách hàng
]


def redact(text):
    """Che mật khẩu, token, khoá, cookie, email trong log trước khi đưa ra ngoài (Telegram, Bedrock) hoặc vào audit."""
    out = str(text or "")
    for rx, rep_ in REDACT_RULES:
        out = rx.sub(rep_, out)
    return out


# ---------------------------------------------------------------- audit
def audit(event, **fields):
    rec = {"ts": datetime.now(VN).isoformat(timespec="seconds"), "type": "remediation_audit",
           "event": event, "mode": MODE}
    rec.update({k: v for k, v in fields.items() if v is not None})
    line = json.dumps(rec, ensure_ascii=False)
    try:
        print(line, flush=True)  # stdout -> Alloy -> Loki
    except (OSError, UnicodeError):
        pass  # lỗi in ra console không được làm mất bản ghi audit trong file
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.error("Không ghi được file audit: %s", type(e).__name__)


# ---------------------------------------------------------------- Telegram
def esc(s):
    return html.escape(str(s or ""), quote=False)


def tg_call(method, body, timeout=40):
    if not TG_TOKEN:
        return None
    req = urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/{method}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        log.error("Telegram %s từ chối (HTTP %s): %s", method, e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:
        log.error("Telegram %s lỗi mạng: %s", method, type(e).__name__)
    return None


def tg_send(text, reply_to=None, buttons=None):
    body = {"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        body["reply_to_message_id"] = reply_to
        body["allow_sending_without_reply"] = True
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons if isinstance(buttons[0], list) else [buttons]}
    res = tg_call("sendMessage", body, timeout=20)
    return (res or {}).get("message_id")


# ---------------------------------------------------------------- Docker (qua socket proxy)
def demux_logs(raw):
    """Docker trả log dạng nhiều khung: 8 byte đầu (kiểu, 0,0,0, độ dài) rồi dữ liệu."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode("utf-8", errors="replace") if out else raw.decode("utf-8", errors="replace")


def build_docker_request(policy, action_id, container):
    d = policy["actions"][action_id]["docker"]
    path = d["path"].replace("{container}", urllib.parse.quote(container, safe=""))
    q = urllib.parse.urlencode(d.get("query", {}))
    return d["method"], f"{DOCKER_URL}{path}" + (f"?{q}" if q else "")


def docker_call(policy, action_id, container):
    """(mã_http, nội_dung). Chỉ gọi đúng đường dẫn mẫu trong policy, không có shell."""
    method, url = build_docker_request(policy, action_id, container)
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(200_000)
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(2000), e.code
    except Exception as e:
        return 0, f"lỗi kết nối Docker proxy: {type(e).__name__}"
    return code, (demux_logs(raw) if action_id == "logs_tail" else raw.decode("utf-8", errors="replace"))


def prom_down_jobs():
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": "up == 0"})
    with urllib.request.urlopen(url, timeout=10) as r:
        return sorted({s["metric"].get("job", "?") for s in json.load(r)["data"]["result"]})


# ---------------------------------------------------------------- xử lý đề xuất
def describe(policy, p):
    return policy["actions"][p["action_id"]]["description"]


def ttl(policy):
    return policy.get("approval_ttl_seconds", 600)


def new_pending(policy, base, rnd, group, state="proposed"):
    """Tạo 1 mục chờ (đề xuất / gợi ý thay thế / menu sau khi từ chối). Gọi khi đang giữ `lock`."""
    pid = secrets.token_hex(6)
    p = pending[pid] = dict(base, id=pid, round=rnd, group=group, state=state,
                            expires=time.time() + ttl(policy), msg_id=None)
    return p


def drop_markup(msg_ids):
    for mid in {m for m in msg_ids if m}:
        tg_call("editMessageReplyMarkup", {"chat_id": TG_CHAT, "message_id": mid,
                                           "reply_markup": {"inline_keyboard": []}}, timeout=10)


def drop_pending(pred):
    """Xoá các mục chờ thoả pred và gỡ nút khỏi tin của chúng. Trả số mục đã xoá."""
    with lock:
        gone = [pending.pop(pid) for pid in [k for k, x in pending.items() if pred(x)]]
    drop_markup(x["msg_id"] for x in gone)
    return len(gone)


def deny_with_alternatives(policy, base, rnd, why, raw):
    """Policy chặn: báo admin kèm các lệnh thay thế (mỗi cái 1 nút Duyệt riêng, vẫn qua policy)."""
    audit("denied", why=why, round=rnd, raw=raw or None, **base)
    if base.get("tool"):   # AI gọi công cụ: nhận lý do + gợi ý ngay trong phiên, admin chỉ được báo ngắn
        tg_send(f"🚫 <b>Policy chặn đề xuất của AI</b> (cảnh báo {esc(base['alert'])}): "
                f"<code>{esc(str(base.get('action_id')))}</code> → <code>{esc(str(base.get('container'))[:60])}</code>\n"
                f"Lý do: {esc(why)}\n<i>AI đã được báo lý do và có thể chọn lệnh khác.</i>")
        return 403, "denied by policy"
    if base.get("manual"):   # người tự gõ: nói thẳng là không được phép, KHÔNG chìa nút chạy lệnh khác
        tg_send(f"⛔ <b>KHÔNG ĐƯỢC PHÉP</b>: <code>{esc(raw or str(base.get('action_id')))}</code>"
                + (f" → <code>{esc(str(base.get('container'))[:60])}</code>" if base.get("container") else "") +
                f"\nLý do: {esc(why)}\nLệnh này nằm ngoài kho lệnh cho phép nên đã bị chặn, không chạy gì cả. Gõ /help để xem lệnh được phép.")
        with lock:
            loop_of(base["fingerprint"], base["alert"])["state"] = "done"
        return 403, "denied by policy"
    shown = raw or str(base.get("action_id"))
    head = (f"🚫 <b>Policy chặn đề xuất của AI</b> (cảnh báo {esc(base['alert'])})\n"
            f"Đề xuất: <code>{esc(shown[:100])}</code>"
            + (f" → <code>{esc(str(base.get('container'))[:60])}</code>" if base.get("container") else "") +
            f"\nLý do: {esc(why)}")
    alts = alternatives_for(policy, base.get("container"), base.get("action_id"))
    fp = base["fingerprint"]
    if not alts:
        tg_send(head + "\nKhông có lệnh thay thế an toàn nào trong policy → cần người vận hành xử lý tay. Vòng xử lý tự động dừng.")
        with lock:
            loop_of(fp, base["alert"])["state"] = "done"
        return 403, "denied by policy"
    group = secrets.token_hex(4)
    with lock:
        lp = loop_of(fp, base["alert"])
        lp["state"], lp["round"] = "active", rnd
        ps = [new_pending(policy, dict(base, action_id=a["action_id"], container=a["container"],
                                       ai_reason="(gợi ý thay thế) " + a["why"]), rnd, group) for a in alts]
    lines = [f"{i}. <code>{esc(a['action_id'])} {esc(a['container'])}</code> — {esc(a['why'])}" for i, a in enumerate(alts, 1)]
    rows = [[{"text": f"✅ Chạy {i}: {a['action_id']} {a['container']}"[:60], "callback_data": callback_data("a", p["id"])}]
            for i, (a, p) in enumerate(zip(alts, ps), 1)]
    rows.append([{"text": "⏹ Dừng", "callback_data": callback_data("s", ps[0]["id"])}])
    mid = tg_send(f"{head}\n━━━━━━━━━━━━━━\n💡 <b>Lệnh thay thế (cần bạn bấm Duyệt đúng lệnh đó):</b>\n" + "\n".join(lines) +
                  f"\n<i>Chế độ: {'CHẠY THẬT' if MODE == 'execute' else 'DRY-RUN'} · hết hạn sau {ttl(policy) // 60} phút.</i>",
                  buttons=rows)
    with lock:
        for p in ps:
            if mid is None:
                pending.pop(p["id"], None)
            else:
                p["msg_id"] = mid
    audit("alternatives_offered", round=rnd, alternatives=[f"{a['action_id']} {a['container']}" for a in alts], **base)
    return 403, "denied by policy"


def handle_propose(policy, payload):
    """Trả (mã_http, thông_báo)."""
    alert = str(payload.get("alert", "?"))[:80]
    fp = str(payload.get("fingerprint", ""))[:80]
    action_id, container = payload.get("action_id"), payload.get("container")
    reason = str(payload.get("reason", ""))[:300]
    raw = str(payload.get("raw", ""))[:100]   # dòng AI viết nhưng bridge không hiểu (vd "docker exec ...")
    manual = payload.get("manual") is True    # lệnh do người gõ trong chat, không phải AI đề xuất: không có vòng lặp AI
    tool = payload.get("tool") is True        # AI gọi qua /tool/propose: bị chặn thì báo ngắn, AI tự chọn lại (không chìa nút thay thế)
    try:
        rnd = max(1, min(int(payload.get("round", 1)), 99))
    except (TypeError, ValueError):
        rnd = 1
    base = dict(alert=alert, fingerprint=fp, action_id=action_id if isinstance(action_id, str) else None,
                container=container if isinstance(container, str) else None, ai_reason=reason,
                manual=True if manual else None, tool=True if tool else None)

    if not ENABLED:
        audit("denied", why="REMEDIATION_ENABLED=false (công tắc tắt khẩn cấp)", round=rnd, **base)
        return 403, "disabled"
    if not (TG_TOKEN and TG_CHAT):
        audit("denied", why="chưa cấu hình Telegram nên không có ai để duyệt", round=rnd, **base)
        return 503, "no telegram"
    with lock:
        if rnd == 1 and fp in loops:        # đề xuất đầu tiên = sự cố mới: bỏ trạng thái dừng/xong của sự cố cũ cùng fingerprint
            loops.pop(fp)
        lp = loop_of(fp, alert)
        if lp["state"] == "stopped":
            audit("denied", why="vòng xử lý đã bị người dùng dừng", round=rnd, **base)
            return 409, "stopped"
    if rnd > MAX_ROUNDS:
        audit("denied", why=f"vượt số vòng tối đa ({MAX_ROUNDS})", round=rnd, **base)
        return 429, "too many rounds"
    ok, why = check_proposal(policy, action_id, container)
    if not ok:
        return deny_with_alternatives(policy, base, rnd, why, raw)

    key = (fp, action_id, container)
    now = time.time()
    with lock:
        for k in [k for k, t in seen.items() if now - t > ttl(policy)]:
            seen.pop(k)
        if key in seen:
            return 200, "duplicate"
        if sum(1 for x in pending.values() if x["state"] == "proposed") >= policy.get("max_pending", 10):
            audit("denied", why="quá nhiều đề xuất đang chờ duyệt", round=rnd, **base)
            return 429, "too many pending"
        ok, why = check_rate(policy, action_id, container, now)
        if not ok:
            audit("denied", why=why, round=rnd, **base)
            rate_msg = f"⏳ AI đề xuất <code>{esc(action_id)} {esc(container)}</code> nhưng chưa chạy được: {esc(why)}."
        else:
            seen[key] = now
            lp["state"], lp["round"] = "active", rnd
            p = new_pending(policy, base, rnd, secrets.token_hex(4))
    if not ok:
        tg_send(rate_msg)
        return 429, "rate limited"
    pid = p["id"]

    loop_note = f" · vòng {rnd}/{MAX_ROUNDS}" if BRIDGE_URL and not no_ai_loop(base) else ""
    title = ("🛠 <b>LỆNH BẠN VỪA GÕ</b> — xác nhận để chạy" if manual
             else f"🛠 <b>AI ĐỀ XUẤT LỆNH SỬA LỖI</b> — chờ bạn duyệt{loop_note}")
    text = (f"{title}\n━━━━━━━━━━━━━━\n"
            + ("" if manual else f"Cảnh báo: <b>{esc(alert)}</b>\n") +
            f"Hành động: <code>{esc(action_id)}</code> — {esc(describe(policy, p))}\n"
            f"Container: <code>{esc(container)}</code>\n"
            f"{'Người gõ' if manual else 'Lý do của AI'}: {esc(reason) or '(không nêu)'}\n━━━━━━━━━━━━━━\n"
            f"Chế độ: <b>{'CHẠY THẬT' if MODE == 'execute' else 'DRY-RUN (chỉ ghi nhận, không chạy)'}</b> · "
            f"hết hạn sau {ttl(policy) // 60} phút.\n"
            + ("<i>Kiểm tra đúng container rồi bấm Duyệt.</i>" if manual else
               "<i>Kết quả do AI đề xuất, hãy kiểm tra trước khi duyệt. Gõ /stop hoặc bấm ⏹ để dừng xử lý.</i>"))
    p["msg_id"] = tg_send(text, buttons=[
        {"text": "✅ Duyệt", "callback_data": callback_data("a", pid)},
        {"text": "❌ Từ chối", "callback_data": callback_data("r", pid)},
        {"text": "⏹ Dừng", "callback_data": callback_data("s", pid)}])
    if p["msg_id"] is None:
        with lock:
            pending.pop(pid, None)
        audit("denied", why="không gửi được tin Telegram để xin duyệt", round=rnd, **base)
        return 502, "telegram failed"
    audit("proposed", id=pid, round=rnd, **base)
    return 200, "queued"


# ---------------------------------------------------------------- TOOL API cho AI (cách 2)
# Chỉ mở cho holmes-bridge (Bearer token). AI chọn action_id + container từ danh sách; lệnh Docker luôn là
# mẫu cố định trong policy.json. Danh tính sự cố (fingerprint/round/alert) do bridge nhét vào môi trường
# của tiến trình holmes, công cụ đọc từ đó, AI không tự đặt được.
READ_OUTPUT_MAX = 4000


def internal_headers():
    """Header cho mọi lần gọi nội bộ remediator <-> holmes-bridge (cùng token REMEDIATOR_TOOL_TOKEN)."""
    h = {"Content-Type": "application/json"}
    if TOOL_TOKEN:
        h["Authorization"] = f"Bearer {TOOL_TOKEN}"
    return h


def tool_auth_ok(header):
    """True nếu header Authorization khớp REMEDIATOR_TOOL_TOKEN. Token trống = tắt hẳn tool API."""
    if not TOOL_TOKEN:
        return False
    given = (header or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(given.encode(), TOOL_TOKEN.encode())


def tool_session_of(fp, rnd):
    """Bộ đếm giới hạn cho 1 phiên AI. Gọi khi đang giữ `lock`."""
    now = time.time()
    for k in [k for k, v in tool_sessions.items() if now - v["ts"] > 6 * 3600]:
        tool_sessions.pop(k)
    return tool_sessions.setdefault((fp, rnd), {"queued": 0, "denied": 0, "reads": 0, "ts": now})


def _mb(n):
    return round(n / 1048576, 1) if isinstance(n, (int, float)) else None


def shape_read_output(action_id, code, text):
    """Trả cho AI CHỈ các trường an toàn. inspect của Docker chứa Config.Env (mật khẩu, khoá) nên TUYỆT ĐỐI không
    trả nguyên văn: lọc theo allowlist. Log cắt ngắn."""
    if not (200 <= code < 300):
        return {"ok": False, "http": code, "error": text[:300]}
    if action_id == "inspect_container":
        try:
            d = json.loads(text)
        except ValueError:
            return {"ok": False, "http": code, "error": "không đọc được phản hồi Docker"}
        st, cfg = d.get("State") or {}, d.get("Config") or {}
        return {"ok": True, "http": code, "data": {
            "status": st.get("Status"), "running": st.get("Running"), "exit_code": st.get("ExitCode"),
            "oom_killed": st.get("OOMKilled"), "started_at": st.get("StartedAt"), "finished_at": st.get("FinishedAt"),
            "health": (st.get("Health") or {}).get("Status"), "restart_count": d.get("RestartCount"),
            "image": cfg.get("Image")}}
    if action_id == "container_stats":
        try:
            d = json.loads(text)
        except ValueError:
            return {"ok": False, "http": code, "error": "không đọc được phản hồi Docker"}
        mem = d.get("memory_stats") or {}
        cpu, pre = d.get("cpu_stats") or {}, d.get("precpu_stats") or {}
        cpu_delta = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cpu_pct = round(100.0 * cpu_delta / sys_delta * (cpu.get("online_cpus") or 1), 1) if sys_delta > 0 else None
        usage, limit = mem.get("usage"), mem.get("limit")
        return {"ok": True, "http": code, "data": {
            "cpu_percent": cpu_pct, "mem_usage_mb": _mb(usage), "mem_limit_mb": _mb(limit),
            "mem_percent": round(100.0 * usage / limit, 1) if usage and limit else None,
            "pids": (d.get("pids_stats") or {}).get("current")}}
    return {"ok": True, "http": code, "data": redact(text[-READ_OUTPUT_MAX:])}   # logs_tail và các action đọc khác (đã che dữ liệu nhạy cảm)


def tool_catalog(policy):
    acts = {}
    for aid, spec in policy["actions"].items():
        extra = spec.get("allow_on_forbidden", [])
        acts[aid] = {"description": spec.get("description", ""),
                     "approval": spec.get("approval", "required"), "mutating": bool(spec.get("mutating")),
                     "containers": sorted(set(policy["containers_allowed"]) | set(extra))}
    return {"actions": acts, "containers_forbidden": policy["containers_forbidden"],
            "note": "approval=none: gọi read_status (chạy ngay). approval=required: gọi propose_fix (cần người duyệt trên Telegram)."}


def tool_read(policy, payload):
    """AI đọc trạng thái. Chỉ nhận action có approval=none. Trả (mã_http, dict)."""
    fp = str(payload.get("fingerprint", ""))[:80]
    try:
        rnd = max(1, min(int(payload.get("round", 1)), 99))
    except (TypeError, ValueError):
        rnd = 1
    action_id, container = payload.get("action_id"), payload.get("container")
    base = dict(alert=str(payload.get("alert", "?"))[:80], fingerprint=fp,
                action_id=action_id if isinstance(action_id, str) else None,
                container=container if isinstance(container, str) else None, tool=True)
    if not ENABLED:
        return 403, {"status": "denied", "message": "hệ thống đang tắt khẩn cấp (REMEDIATION_ENABLED=false)"}
    ok, why = check_proposal(policy, action_id, container)
    if not ok:
        audit("denied", why=why, round=rnd, **base)
        return 403, {"status": "denied", "message": why,
                     "hint": "gọi list_allowed_actions để xem action và container được phép"}
    spec = policy["actions"][action_id]
    if spec.get("approval", "required") != "none":
        return 403, {"status": "denied", "message": f"'{action_id}' làm thay đổi hệ thống, cần người duyệt: hãy dùng propose_fix"}
    with lock:
        sess = tool_session_of(fp, rnd)
        if sess["reads"] >= TOOL_MAX_READS:
            return 429, {"status": "rate_limited", "message": f"đã đọc {sess['reads']} lần trong phiên này, hãy kết luận"}
        ok, why = check_rate(policy, action_id, container)
        if not ok:
            return 429, {"status": "rate_limited", "message": why}
        record_run(action_id, container)
        sess["reads"] += 1
    code, out = docker_call(policy, action_id, container)
    audit("tool_read", round=rnd, http_status=code, **base)
    return 200, dict(shape_read_output(action_id, code, out), status="ok" if 200 <= code < 300 else "error")


def tool_propose(policy, payload):
    """AI đề xuất lệnh thay đổi. Luôn cần người duyệt. Trả (mã_http, dict) để AI biết ngay kết quả."""
    fp = str(payload.get("fingerprint", ""))[:80]
    try:
        rnd = max(1, min(int(payload.get("round", 1)), 99))
    except (TypeError, ValueError):
        rnd = 1
    action_id, container = payload.get("action_id"), payload.get("container")
    spec = policy["actions"].get(action_id) if isinstance(action_id, str) else None
    if spec and spec.get("approval", "required") == "none":
        return 403, {"status": "denied", "message": f"'{action_id}' chỉ đọc, chạy ngay: hãy dùng read_status thay vì propose_fix"}
    with lock:
        sess = tool_session_of(fp, rnd)
        if sess["queued"] >= 1:
            return 200, {"status": "already_proposed",
                         "message": "đã có 1 đề xuất đang chờ người duyệt trong phiên này, hãy kết thúc phiên"}
        if sess["denied"] >= TOOL_MAX_DENIED:
            return 429, {"status": "rate_limited", "message": "bị chặn nhiều lần, hãy kết luận và nhờ người vận hành xử lý tay"}
    body = {"alert": payload.get("alert", "?"), "fingerprint": fp, "action_id": action_id, "container": container,
            "reason": payload.get("reason", ""), "round": rnd, "tool": True}
    code, msg = handle_propose(policy, body)
    alts = []
    if code == 200 and msg == "queued":
        with lock:
            tool_session_of(fp, rnd)["queued"] += 1
        audit("tool_propose", fingerprint=fp, round=rnd, action_id=action_id if isinstance(action_id, str) else None,
              container=container if isinstance(container, str) else None, result="queued")
        return 200, {"status": "queued", "message": "đã gửi xin duyệt trên Telegram. Hãy kết thúc phiên; kết quả sau khi duyệt sẽ được gửi lại cho bạn ở vòng sau."}
    with lock:
        tool_session_of(fp, rnd)["denied"] += 1
    if code == 403 and msg == "denied by policy":
        ok, why = check_proposal(policy, action_id, container)
        alts = alternatives_for(policy, container if isinstance(container, str) else None,
                                action_id if isinstance(action_id, str) else None)
        return 403, {"status": "denied", "message": why or "bị policy chặn",
                     "alternatives": [{"action_id": a["action_id"], "container": a["container"], "why": a["why"],
                                       "via": "read_status" if policy["actions"][a["action_id"]].get("approval") == "none" else "propose_fix"}
                                      for a in alts]}
    status = {200: "duplicate", 409: "stopped", 429: "rate_limited"}.get(code, "denied")
    text = {"duplicate": "bạn đã đề xuất đúng lệnh này trước đó (có thể đã chạy ở vòng trước); hãy đề xuất lệnh KHÁC hoặc kết luận cần người xử lý tay", "stopped": "người dùng đã dừng xử lý sự cố này",
            "rate_limited": "đang trong thời gian chờ/giới hạn, hãy chọn lệnh khác hoặc kết luận"}.get(status, msg)
    return code, {"status": status, "message": text}


def tool_state():
    """Ngữ cảnh hiện tại cho AI chat: đề xuất đang chờ duyệt + lệnh vừa chạy (không có bí mật)."""
    now = time.time()
    with lock:
        pend = [{"action_id": x["action_id"], "container": x["container"], "alert": x["alert"],
                 "age_s": int(now - (x["expires"] - ttl(POLICY_FOR_STATE))), "state": x["state"]}
                for x in pending.values() if x["state"] == "proposed" and x.get("action_id")]
    return {"pending": pend[:10], "recent": list(recent_actions)}


def tool_overview(policy):
    text = re.sub(r"<[^>]+>", "", overview_text(policy))
    return {"status": "ok", "text": html.unescape(text)[:3500]}


def tool_session_info(fp, rnd):
    with lock:
        sess = tool_sessions.get((fp, rnd)) or {}
        return {"queued": sess.get("queued", 0), "denied": sess.get("denied", 0), "reads": sess.get("reads", 0)}


# ---------------------------------------------------------------- vòng lặp AI <-> người duyệt
def stop_loop(fp, who, why="người dùng dừng"):
    with lock:
        lp = loop_of(fp)
        lp["state"] = "stopped"
        alert = lp["alert"]
    drop_pending(lambda x: x["fingerprint"] == fp)
    audit("loop_stopped", fingerprint=fp, alert=alert, approver=who, why=why)
    tg_send(f"⏹ <b>Đã dừng xử lý tự động</b> cho cảnh báo <b>{esc(alert)}</b> ({esc(why)}).\n"
            f"Các đề xuất đang chờ đã bị huỷ; AI sẽ không đề xuất thêm cho sự cố này. Cảnh báo gốc vẫn do người vận hành xử lý.")


def close_loop(fp, why="cảnh báo đã hết"):
    """holmes-bridge gọi khi cảnh báo RESOLVED: huỷ đề xuất đang chờ, kết thúc vòng (không nhắn Telegram, bridge tự nhắn)."""
    with lock:
        lp = loops.get(fp)
        if lp and lp["state"] != "stopped":
            lp["state"] = "done"
    n = drop_pending(lambda x: x["fingerprint"] == fp)
    audit("loop_closed", fingerprint=fp, why=why, cancelled_pending=n)


def continue_loop(policy, p, note, down=None):
    """Gửi kết quả cho AI (holmes-bridge) để đề xuất bước kế tiếp. Không làm gì nếu vòng lặp bị tắt/dừng/hết vòng."""
    fp, rnd = p["fingerprint"], p.get("round", 1)
    if not BRIDGE_URL or no_ai_loop(p):
        with lock:
            if fp in loops:
                loops[fp]["state"] = "done"
        return
    with lock:
        lp = loops.get(fp) or loop_of(fp, p["alert"])
        if lp["state"] == "stopped":
            return
        hist = list(lp["history"])[-5:]
    if rnd >= MAX_ROUNDS:
        with lock:
            lp["state"] = "done"
        audit("loop_ended", fingerprint=fp, alert=p["alert"], round=rnd, why="hết số vòng")
        tg_send(f"🔚 Đã dùng hết {MAX_ROUNDS} vòng AI cho cảnh báo <b>{esc(p['alert'])}</b> mà chưa ổn hẳn. "
                f"Cần người vận hành xử lý tiếp.", reply_to=p.get("msg_id"))
        return
    with lock:  # mục chỉ để có nút ⏹ Dừng trong lúc AI đang suy nghĩ
        w = new_pending(policy, dict(alert=p["alert"], fingerprint=fp, action_id=None, container=None, ai_reason=""),
                        rnd, secrets.token_hex(4), state="waiting_ai")
    w["msg_id"] = tg_send(f"🔁 Đang gửi kết quả cho AI để đề xuất bước tiếp (vòng {rnd + 1}/{MAX_ROUNDS})… "
                          f"Gõ /stop hoặc bấm ⏹ để dừng.", reply_to=p.get("msg_id"),
                          buttons=[{"text": "⏹ Dừng", "callback_data": callback_data("s", w["id"])}])
    body = {"fingerprint": fp, "alert": p["alert"], "round": rnd, "history": hist,
            "status": note, "down_jobs": down or []}
    req = urllib.request.Request(f"{BRIDGE_URL}/continue", data=json.dumps(body).encode(), headers=internal_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            audit("loop_continue", fingerprint=fp, alert=p["alert"], round=rnd, http_status=r.status)
    except urllib.error.HTTPError as e:
        audit("loop_ended", fingerprint=fp, alert=p["alert"], round=rnd, why=f"AI không nhận yêu cầu (HTTP {e.code})")
        tg_send(f"⚠️ AI không nhận được kết quả (HTTP {e.code}) nên vòng xử lý dừng ở đây.", reply_to=p.get("msg_id"))
    except Exception as e:
        audit("loop_ended", fingerprint=fp, alert=p["alert"], round=rnd, why=f"không gọi được holmes-bridge: {type(e).__name__}")
        tg_send("⚠️ Không gọi được holmes-bridge nên vòng xử lý dừng ở đây.", reply_to=p.get("msg_id"))


def execute(policy, p, approver):
    """Chạy trong luồng riêng: thực thi (hoặc dry-run) rồi báo kết quả, rồi (nếu cần) đẩy kết quả lại cho AI."""
    action_id, container = p["action_id"], p["container"]
    base = dict(id=p["id"], alert=p["alert"], fingerprint=p["fingerprint"], action_id=action_id,
                container=container, approver=approver, round=p.get("round", 1))
    method, url = build_docker_request(policy, action_id, container)
    command = f"{method} {urllib.parse.urlsplit(url).path}"

    if MODE != "execute":
        audit("approved_dry_run", command=command, executed=False, **base)
        tg_send(f"🧪 <b>DRY-RUN</b>: đã duyệt nhưng KHÔNG chạy.\nSẽ chạy: <code>{esc(command)}</code>",
                reply_to=p.get("msg_id"))
        with lock:
            loop_of(p["fingerprint"], p["alert"])["state"] = "done"   # không chạy thật thì không có kết quả để AI xem
        return

    # Kiểm tra lại giới hạn ngay lúc chạy thật (giữa lúc đề xuất và lúc duyệt có thể đã chạy lần khác)
    with lock:
        ok, why = check_rate(policy, action_id, container)
        if ok:
            record_run(action_id, container)
    if not ok:
        audit("denied", why=why, **base)
        tg_send(f"🚫 Không chạy: {esc(why)}", reply_to=p.get("msg_id"))
        return

    t0 = time.time()
    code, out = docker_call(policy, action_id, container)
    secs = round(time.time() - t0, 1)
    success = 200 <= code < 300
    if policy["actions"][action_id].get("approval") == "none" and action_id != "logs_tail":
        # inspect/stats của Docker chứa biến môi trường (mật khẩu, khoá): chỉ dùng bản đã LỌC cho audit/Telegram
        shaped = shape_read_output(action_id, code, out)
        out = json.dumps(shaped.get("data", shaped.get("error", "")), ensure_ascii=False)
        audit("executed", command=command, executed=True, http_status=code, success=success,
              duration_s=secs, output_tail=out[-500:], **base)
        tg_send(f"📊 <code>{esc(container)}</code> ({esc(action_id)}):\n<pre>{esc(out[:1500])}</pre>", reply_to=p.get("msg_id"))
        with lock:
            loop_of(p["fingerprint"], p["alert"])["state"] = "done"
        return
    if action_id == "logs_tail":
        out = redact(out)                     # log có thể chứa mật khẩu/token/email: che trước khi gửi Telegram, AI, audit
    audit("executed", command=command, executed=True, http_status=code, success=success,
          duration_s=secs, output_tail=out[-500:], **base)
    recent_actions.append({"ts": datetime.now(VN).strftime("%H:%M:%S"), "action_id": action_id, "container": container,
                           "ok": success, "http": code})
    with lock:
        loop_of(p["fingerprint"], p["alert"])["history"].append(
            {"round": p.get("round", 1), "action_id": action_id, "container": container, "ok": success,
             "http": code, "output": out[-400:]})

    if action_id == "logs_tail":
        body = f"<pre>{esc(out[-1500:]) or '(trống)'}</pre>" if success else f"Lỗi HTTP {code}: {esc(out[:300])}"
        tg_send(f"📄 Log cuối của <code>{esc(container)}</code>:\n{body}", reply_to=p.get("msg_id"))
        continue_loop(policy, p, f"đã xem log của {container}" if success else f"xem log lỗi HTTP {code}")
        return

    check, down = "", None
    if success:
        time.sleep(POST_CHECK_DELAY)
        try:
            down = prom_down_jobs()
            check = ("\nKiểm tra lại: mọi thành phần đang trả lời Prometheus ✅" if not down
                     else f"\nKiểm tra lại: còn chưa trả lời: {esc(', '.join(down))} ⚠️")
        except Exception as e:
            check = f"\nKhông kiểm tra lại được Prometheus ({type(e).__name__})"
    tg_send(f"{'✅' if success else '❌'} <code>{esc(command)}</code> → HTTP {code} trong {secs}s"
            f"{'' if success else chr(10) + esc(out[:300])}{check}", reply_to=p.get("msg_id"))

    if success and down == []:
        with lock:
            loop_of(p["fingerprint"], p["alert"])["state"] = "done"
        audit("loop_ended", fingerprint=p["fingerprint"], alert=p["alert"], round=p.get("round", 1), why="hệ thống đã ổn sau lệnh")
        if BRIDGE_URL and not no_ai_loop(p):
            tg_send("🏁 Hệ thống đã ổn sau lệnh này nên vòng AI kết thúc. Đợi tin \"đã hoạt động bình thường\" khi cảnh báo tự hết.",
                    reply_to=p.get("msg_id"))
        return
    continue_loop(policy, p, "lệnh chạy lỗi" if not success else "lệnh chạy xong nhưng hệ thống chưa ổn", down)


def handle_callback(policy, cb):
    cid = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
    uid = str((cb.get("from") or {}).get("id", ""))
    cb_id = cb.get("id")

    def answer(text):
        tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:180]}, timeout=10)

    if not is_authorized(cid, uid):
        audit("unauthorized_click", approver=uid, why="sai chat hoặc không nằm trong REMEDIATOR_APPROVER_IDS")
        return answer("Bạn không có quyền duyệt.")
    parsed = parse_callback(cb.get("data"))
    if not parsed:
        audit("invalid_click", approver=uid, why="chữ ký nút bấm không hợp lệ")
        return answer("Nút không hợp lệ.")
    kind, pid = parsed
    with lock:
        p = pending.pop(pid, None)  # pop = dùng 1 lần
    if not p:
        return answer("Đề xuất này đã được xử lý hoặc đã hết hạn.")
    if time.time() > p["expires"]:
        if p["state"] == "proposed":
            audit("expired", id=pid, alert=p["alert"], action_id=p["action_id"], container=p["container"])
        return answer("Đề xuất đã hết hạn.")

    # Bấm 1 nút = xong việc của cả nhóm nút trên tin đó (các gợi ý còn lại bị huỷ)
    drop_pending(lambda x: x.get("group") == p.get("group") and x["id"] != pid)
    drop_markup([p["msg_id"]])
    if kind == "s":
        stop_loop(p["fingerprint"], uid)
        return answer("Đã dừng xử lý.")
    with lock:
        stopped = (loops.get(p["fingerprint"]) or {}).get("state") == "stopped"
    if stopped:
        return answer("Vòng xử lý này đã bị dừng.")
    if kind == "n":
        if p["state"] != "rejected":
            return answer("Nút không hợp lệ.")
        audit("followup_requested", id=pid, alert=p["alert"], fingerprint=p["fingerprint"], approver=uid, round=p["round"])
        answer("Đang hỏi AI cách khác.")
        threading.Thread(target=continue_loop, args=(policy, p, "người duyệt TỪ CHỐI lệnh vừa đề xuất, cần cách khác"), daemon=True).start()
        return
    if p["state"] != "proposed":
        return answer("Nút không hợp lệ.")

    if kind == "r":
        audit("rejected", id=pid, alert=p["alert"], fingerprint=p["fingerprint"],
              action_id=p["action_id"], container=p["container"], approver=uid, round=p["round"])
        text = f"❌ Đã từ chối lệnh <code>{esc(p['action_id'])}</code> → <code>{esc(p['container'])}</code>."
        if BRIDGE_URL and p["round"] < MAX_ROUNDS and not no_ai_loop(p):
            with lock:
                m = new_pending(policy, {k: p[k] for k in ("alert", "fingerprint", "action_id", "container", "ai_reason")},
                                p["round"], secrets.token_hex(4), state="rejected")
            m["msg_id"] = tg_send(text + "\nMuốn AI nghĩ cách khác hay dừng?", reply_to=p["msg_id"], buttons=[
                {"text": "🔁 Hỏi AI cách khác", "callback_data": callback_data("n", m["id"])},
                {"text": "⏹ Dừng", "callback_data": callback_data("s", m["id"])}])
        else:
            tg_send(text, reply_to=p["msg_id"])
            with lock:
                loop_of(p["fingerprint"], p["alert"])["state"] = "done"
        return answer("Đã từ chối.")

    ok, why = check_proposal(policy, p["action_id"], p["container"])  # kiểm tra lại, phòng policy đổi
    if not ok:
        audit("denied", id=pid, why=why, approver=uid, action_id=p["action_id"], container=p["container"])
        return answer("Policy chặn: " + why)
    answer("Đã duyệt.")
    threading.Thread(target=execute, args=(policy, p, uid), daemon=True).start()


MANUAL_CMDS = {"restart": "restart_container", "start": "start_container", "logs": "logs_tail", "status": "inspect_container"}
HELP_TEXT = ("⌨️ <b>Lệnh bạn có thể gõ</b> (<code>/ai</code> mở phiên trò chuyện tự nhiên với AI; lệnh <b>đọc</b> như xem tình trạng/log chạy ngay; lệnh làm thay đổi hệ thống vẫn qua policy và cần bấm Duyệt):\n"
             "<code>/restart &lt;container&gt;</code> — khởi động lại\n"
             "<code>/start &lt;container&gt;</code> — bật lại container đã dừng\n"
             "<code>/logs &lt;container&gt;</code> — xem 100 dòng log cuối\n"
             "<code>/status</code> — <b>tổng quan cả hệ thống</b> (container, CPU/RAM máy, web, cảnh báo); cũng hỏi được: <code>các container thế nào?</code>\n"
             "<code>/status &lt;container&gt;</code> — trạng thái 1 container (chạy ngay, chỉ đọc)\n"
             "<code>/stop [tên_cảnh_báo]</code> — dừng vòng xử lý tự động của AI\n"
             "Gõ kiểu terminal cũng được: <code>docker restart backend</code>. Tên container viết tắt được: <code>backend</code> = <code>ecommerce-backend</code>.\n"
             "Nói tự nhiên cũng được: <code>chạy cho tôi container db</code>, <code>khởi động lại backend</code>, "
             "<code>xem log nginx</code>. Tôi hiện lại việc đã hiểu kèm nút Duyệt; câu lạ sẽ nhờ AI hiểu.\n"
             "Lệnh khác (stop, rm, exec, restart DB...) <b>không được phép</b>, bot sẽ báo và không chạy gì.")


def refuse_manual(verb, arg, uid):
    """Người gõ lệnh docker không có trong kho lệnh (stop, rm, kill, exec, ...): báo không được phép, không chạy gì."""
    shown = ("docker " + verb + (" " + arg if arg else ""))[:100]
    audit("manual_command", approver=uid, action_id="khong_hop_le", container=None, raw=shown)
    audit("denied", why=f"lệnh 'docker {verb[:20]}' không có trong kho lệnh (policy.json)", approver=uid,
          alert="Lệnh gõ tay", manual=True, raw=shown)
    tg_send(f"⛔ <b>KHÔNG ĐƯỢC PHÉP</b>: <code>{esc(shown)}</code>\n"
            f"Lý do: lệnh <code>docker {esc(verb[:20])}</code> không có trong kho lệnh cho phép "
            f"(chỉ có restart, start, logs). Không chạy gì cả. Gõ /help để xem lệnh được phép.")


def first_arg(rest):
    """Tên container = từ cuối cùng không phải cờ (docker logs --tail 50 nginx -> nginx). Cờ bị bỏ qua: lệnh thật luôn lấy từ mẫu cố định."""
    for tok in reversed((rest or "").split()):
        if not tok.startswith("-"):
            return tok[:80]
    return ""


def handle_manual(policy, cmd, arg, uid):
    """Người gõ /restart|/start|/logs <container>: đi qua đúng đường của đề xuất AI (policy, cooldown, nút Duyệt, audit)."""
    name = arg.strip()
    if not name and cmd == "status":
        return send_overview(policy, uid, f"/{cmd}")
    if name and not name.startswith("ecommerce-") and NAME_RE.fullmatch("ecommerce-" + name):
        name = "ecommerce-" + name
    if not name:
        tg_send(f"Thiếu tên container. Ví dụ: <code>/{cmd} ecommerce-backend</code>\n\n{HELP_TEXT}")
        return
    if policy["actions"].get(MANUAL_CMDS[cmd], {}).get("approval") == "none":
        return run_read_now(policy, MANUAL_CMDS[cmd], name, uid, f"/{cmd} {name}")      # chỉ đọc: chạy ngay
    audit("manual_command", approver=uid, action_id=MANUAL_CMDS[cmd], container=name[:80])
    handle_propose(policy, {"alert": "Lệnh gõ tay", "fingerprint": "manual-" + secrets.token_hex(4),
                            "action_id": MANUAL_CMDS[cmd], "container": name, "round": 1, "manual": True,
                            "reason": f"người dùng {uid} gõ /{cmd} {name[:60]}"})


# ---------------------------------------------------------------- monitor qua chat (hỏi tình trạng, bot lấy số liệu báo lại)
# Chỉ ĐỌC (không đổi gì), nên chạy ngay không cần nút Duyệt, không gọi AI, không tốn token. Dữ liệu lấy từ Docker (inspect đã lọc
# trường) và Prometheus. Người hỏi vẫn phải đúng chat + REMEDIATOR_APPROVER_IDS.
def prom_scalar(query):
    try:
        url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=6) as r:
            res = json.load(r)["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def prom_names(query, label):
    try:
        url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=6) as r:
            return sorted({x["metric"].get(label, "?") for x in json.load(r)["data"]["result"]})
    except Exception:
        return None


def human_uptime(started_at):
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", started_at or "")
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    sec = max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
    if sec >= 86400:
        return f"{sec // 86400} ngày {sec % 86400 // 3600} giờ"
    if sec >= 3600:
        return f"{sec // 3600} giờ {sec % 3600 // 60} phút"
    return f"{sec // 60} phút" if sec >= 60 else f"{sec} giây"


def container_line(name, res):
    """(emoji, dòng mô tả) cho 1 container từ kết quả inspect đã lọc."""
    short = name.replace("ecommerce-", "", 1)
    if res is None:
        return "⚪", f"{short} · không được phép đọc"
    if not res.get("ok"):
        return "⚪", f"{short} · không đọc được (HTTP {res.get('http')}; có thể chưa được tạo)"
    d = res["data"]
    health = d.get("health")
    if d.get("running"):
        emoji = "🟢" if health in (None, "", "healthy", "none") else "🟡"
    else:
        emoji = "🔴"
    bits = [str(d.get("status") or "?")]
    up = human_uptime(d.get("started_at")) if d.get("running") else None
    if up:
        bits.append(f"chạy {up}")
    if health and health != "none":
        bits.append(f"health {health}")
    if d.get("restart_count"):
        bits.append(f"đã restart {d['restart_count']} lần")
    if d.get("oom_killed"):
        bits.append("BỊ OOM")
    if not d.get("running") and d.get("exit_code") is not None:
        bits.append(f"mã thoát {d['exit_code']}")
    return emoji, f"{short} · " + " · ".join(bits)


def overview_text(policy):
    """Báo cáo tình trạng: container (Docker), máy chủ + web (Prometheus), cảnh báo đang bật."""
    names = list(policy["containers_allowed"])
    for c in policy["actions"].get("inspect_container", {}).get("allow_on_forbidden", []):
        if c not in names:
            names.insert(0, c)

    def one(c):
        if not check_proposal(policy, "inspect_container", c)[0]:
            return c, None
        code, out = docker_call(policy, "inspect_container", c)
        return c, shape_read_output("inspect_container", code, out)
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, names))
    lines, running = [], 0
    for c, res in results:
        emoji, line = container_line(c, res)
        running += emoji in ("🟢", "🟡")
        lines.append(f"{emoji} {esc(line)}")
    cpu = prom_scalar('100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))')
    mem = prom_scalar("100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)")
    disk = prom_scalar('max(100 - (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"} * 100))')
    rps = prom_scalar('sum(rate(http_requests_total{job="backend",route!~"/health|/metrics"}[1m]))')
    err = prom_scalar('sum(rate(http_requests_total{job="backend",route!~"/health|/metrics",status_code=~"5.."}[1m]))')

    def pct(v):
        return f"{v:.0f}%" if v is not None else "n/a"
    err_pct = f"{100 * err / rps:.1f}%" if rps and err is not None else ("0%" if rps else "n/a")
    firing = prom_names('ALERTS{alertstate="firing"}', "alertname")
    down = prom_names("up == 0", "job")
    alert_line = "không rõ (không hỏi được Prometheus)" if firing is None else (", ".join(firing) if firing else "không có ✅")
    down_line = "" if not down else f"\n⚠️ Chưa trả lời Prometheus: {esc(', '.join(down))}"
    now = datetime.now(VN).strftime("%H:%M:%S %d/%m")
    return (f"📊 <b>Tình trạng hệ thống</b> · {now}\n━━━━━━━━━━━━━━\n" + "\n".join(lines) +
            f"\n━━━━━━━━━━━━━━\nChạy: <b>{running}/{len(results)}</b> container\n"
            f"🖥 Máy: CPU {pct(cpu)} · RAM {pct(mem)} · ổ đĩa {pct(disk)}\n"
            f"🌐 Web: {f'{rps:.1f}' if rps is not None else 'n/a'} req/s · lỗi 5xx {err_pct}\n"
            f"🚨 Cảnh báo đang bật: {esc(alert_line)}{down_line}")


def send_overview(policy, uid, shown):
    audit("monitor_query", approver=uid, raw=shown[:100], kind="overview")
    tg_send(overview_text(policy))


def run_read_now(policy, action_id, container, uid, shown):
    """Người hỏi 1 container (log / trạng thái / CPU-RAM): action chỉ đọc nên chạy NGAY, không cần nút Duyệt, không gọi AI."""
    ok, why = check_proposal(policy, action_id, container)
    if not ok:    # bị chặn: hiện đúng tin "KHÔNG ĐƯỢC PHÉP" như mọi lệnh gõ tay
        propose_manual(policy, action_id, container, uid, shown, force_confirm=True)
        return
    audit("manual_command", approver=uid, action_id=action_id, container=container[:80], raw=shown[:100])
    code, out = docker_call(policy, action_id, container)
    if action_id == "logs_tail":
        out = redact(out)
        body = f"<pre>{esc(out[-1500:]) or '(trống)'}</pre>" if 200 <= code < 300 else f"Lỗi HTTP {code}: {esc(out[:300])}"
        tg_send(f"📄 Log cuối của <code>{esc(container)}</code>:\n{body}")
        audit("executed", approver=uid, action_id=action_id, container=container, executed=True, read_only=True,
              http_status=code, success=200 <= code < 300, output_tail=out[-300:], alert="Lệnh gõ tay", fingerprint="manual-read")
        return
    shaped = shape_read_output(action_id, code, out)
    if action_id == "inspect_container":
        emoji, line = container_line(container, shaped)
        extra = f"\nimage: {esc((shaped.get('data') or {}).get('image'))}" if shaped.get("ok") else ""
        text = f"{emoji} {esc(line)}{extra}"
    else:
        d = shaped.get("data") or {}
        text = (f"📈 <code>{esc(container)}</code>: CPU {d.get('cpu_percent', 'n/a')}% · RAM {d.get('mem_usage_mb', 'n/a')}/"
                f"{d.get('mem_limit_mb', 'n/a')} MB ({d.get('mem_percent', 'n/a')}%) · {d.get('pids', 'n/a')} tiến trình"
                if shaped.get("ok") else f"Lỗi HTTP {code}: {esc(str(shaped.get('error', ''))[:200])}")
    tg_send(text)
    audit("executed", approver=uid, action_id=action_id, container=container, executed=True, read_only=True,
          http_status=code, success=shaped.get("ok", False), output_tail=json.dumps(shaped.get("data", shaped.get("error", "")),
                                                                                     ensure_ascii=False)[-300:],
          alert="Lệnh gõ tay", fingerprint="manual-read")


# ---------------------------------------------------------------- phiên làm việc với AI (/ai)
AI_HELLO = ("🤖 <b>Đã mở phiên làm việc với AI.</b> Bạn cứ nói tự nhiên, ví dụ:\n"
            "• <code>tôi muốn chạy lại container của backend</code>\n"
            "• <code>db sao rồi?</code> · <code>vì sao web chậm?</code>\n"
            "AI nhớ các câu trước trong phiên, tự đọc trạng thái hệ thống và <b>đề xuất lệnh kèm nút Duyệt</b> "
            "(AI không tự chạy; duyệt chỉ bằng nút ✅). Lệnh <code>/restart</code>, <code>/status</code>... vẫn dùng được.\n"
            f"Kết thúc: <code>/ai off</code> (hoặc <code>/exit</code>) · quên hội thoại: <code>/ai reset</code> · tự đóng sau "
            f"{AI_SESSION_IDLE // 60} phút im lặng. Mỗi tin nhắn tốn một lượt AI nhỏ.")


def session_sid():
    """sid của phiên đang mở và còn hạn, ngược lại None (phiên hết hạn thì đóng luôn)."""
    with lock:
        sid, last = ai_session["sid"], ai_session["last"]
    if sid and time.time() - last > AI_SESSION_IDLE:
        end_session("hệ thống", f"{AI_SESSION_IDLE // 60} phút không hoạt động", quiet_if_none=True)
        return None
    return sid


def notify_bridge_end(sid):
    if not (BRIDGE_URL and TOOL_TOKEN and sid):
        return
    req = urllib.request.Request(f"{BRIDGE_URL}/chat/end", data=json.dumps({"sid": sid}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOOL_TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass   # bridge tự dọn trí nhớ theo thời gian; lỗi ở đây không quan trọng


def start_session(policy, uid, first_text=""):
    if not (BRIDGE_URL and TOOL_TOKEN):
        tg_send("⚠️ Phiên AI chưa bật: cần đặt <code>REMEDIATOR_TOOL_TOKEN</code> (xem README2 mục 1c). "
                "Các lệnh gõ trực tiếp (<code>/help</code>) vẫn dùng được.")
        return
    old = session_sid()
    sid = secrets.token_hex(3)
    with lock:
        ai_session.update(sid=sid, last=time.time())
    notify_bridge_end(old)
    audit("ai_session_start", approver=uid, session=sid)
    tg_send(AI_HELLO)
    if first_text:
        handle_session_message(policy, first_text, uid)


def end_session(uid, why="người dùng kết thúc", quiet_if_none=False):
    with lock:
        sid = ai_session["sid"]
        ai_session.update(sid=None, last=0.0)
    if not sid:
        if not quiet_if_none:
            tg_send("ℹ️ Không có phiên AI nào đang mở. Gõ <code>/ai</code> để bắt đầu.")
        return
    notify_bridge_end(sid)
    audit("ai_session_end", approver=uid, session=sid, why=why)
    tg_send(f"👋 Đã kết thúc phiên AI ({esc(why)}). Hội thoại đã được xoá khỏi bộ nhớ. Gõ <code>/ai</code> để mở phiên mới.")


def handle_ai_command(policy, arg, uid):
    a = (arg or "").strip()
    low = a.lower()
    if low in ("off", "stop", "exit", "end", "thoat", "thoát", "dong", "đóng"):
        return end_session(uid)
    if low == "reset":
        if not session_sid():
            return tg_send("ℹ️ Không có phiên AI nào đang mở. Gõ <code>/ai</code> để bắt đầu.")
        with lock:
            old = ai_session["sid"]
            ai_session.update(sid=secrets.token_hex(3), last=time.time())
        notify_bridge_end(old)
        audit("ai_session_reset", approver=uid)
        return tg_send("🧹 Đã xoá trí nhớ hội thoại, phiên AI vẫn mở.")
    if low == "status":
        sid = session_sid()
        return tg_send("🤖 Phiên AI: <b>đang mở</b>" if sid else "🤖 Phiên AI: <b>đang đóng</b>")
    start_session(policy, uid, a)


def handle_session_message(policy, text, uid):
    """Đang trong phiên /ai: tin nhắn thường đi thẳng tới AI (có trí nhớ). Hỏi tổng quan chung thì vẫn trả ngay, miễn phí."""
    it = parse_intent(policy, text)
    acts, conts = it["actions"], it["containers"]
    clear = bool(
        (it["dangerous"] and conts) or                                         # "xoá db": từ chối ngay, không tốn lượt AI
        (it["overview"] and not conts and not acts) or                         # hỏi tổng quan chung ("các container thế nào?")
        (not it["question"] and acts and conts and len(acts) * len(conts) <= 3))   # "khởi động lại backend" (hoặc vài cách hiểu -> nút chọn), không phải câu hỏi
    if clear:
        with lock:
            ai_session["last"] = time.time()
        return handle_natural(policy, text, uid)             # trả ngay (~0 giây) thay vì chờ AI 15-20 giây
    sid = session_sid()
    if not sid:                            # vừa hết hạn đúng lúc này
        return handle_natural(policy, text, uid)
    with lock:
        ai_session["last"] = time.time()
    if not ask_ai_chat(text, uid, sid):
        tg_send("⚠️ Không gọi được AI (thiếu cấu hình). Gõ <code>/ai off</code> rồi dùng lệnh trực tiếp (<code>/help</code>).")


# ---------------------------------------------------------------- câu nói tự nhiên ("chạy cho tôi container db")
# Đường A (miễn phí, tất định): bảng từ khoá tiếng Việt/Anh -> action + container. Rõ ràng thì hiện lệnh đã hiểu kèm nút
# Duyệt; mơ hồ thì hỏi lại bằng nút. Đường B: câu lạ nhưng có vẻ là yêu cầu -> nhờ AI (bridge /chat) dùng công cụ.
# Cả hai đường đều kết thúc bằng nút Duyệt hiển thị ĐÚNG lệnh đã hiểu; luật policy.json áp dụng như mọi lệnh khác.
NL_RESTART = re.compile(r"\b(khoi dong lai|restart|reboot|chay lai)\b")
NL_START = re.compile(r"\b(bat lai|bat|mo|start|khoi dong|chay)\b")
NL_LOGS = re.compile(r"\b(xem log|logs?|nhat ky)\b")
NL_STATUS = re.compile(r"\b(trang thai|status|con song|dang chay khong|kiem tra)\b")
NL_DANGEROUS = re.compile(r"\b(xoa|delete|remove|rm|kill|tat|stop|prune|dung lai|ngung)\b")
NL_OVERVIEW = re.compile(r"\b(tong quan|tinh trang|overview|cac container|tat ca container|moi thu|he thong (the nao|ra sao|on|co on)|"
                         r"on khong|co van de (gi )?khong|dang chay (het )?(chua|khong)|dashboard)\b")
NL_QUESTION = re.compile(r"\b(vi sao|tai sao|sao|the nao|co van de|bi gi|loi|cham|nguyen nhan|\?)")
EXTRA_ALIASES = {"database": "ecommerce-db", "csdl": "ecommerce-db", "mysql": "ecommerce-db", "my sql": "ecommerce-db",
                 "web": "ecommerce-nginx", "api": "ecommerce-backend", "back end": "ecommerce-backend",
                 "front end": "ecommerce-frontend", "node exporter": "ecommerce-node-exporter",
                 "nginx exporter": "ecommerce-nginx-exporter", "mysqld exporter": "ecommerce-mysqld-exporter",
                 "mysql exporter": "ecommerce-mysqld-exporter"}


def normalize_text(text):
    t = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9?-]+", " ", t)).strip()


def parse_intent(policy, text):
    """Hiểu câu nói bằng bảng từ khoá. Trả {"actions": [...], "containers": [...], "dangerous": bool, "question": bool}."""
    t = normalize_text(text)
    known = set(policy["containers_allowed"]) | set(policy["containers_forbidden"])
    for spec in policy["actions"].values():
        known |= set(spec.get("allow_on_forbidden", []))
    aliases = {c.replace("ecommerce-", "", 1): c for c in known}
    aliases.update({c: c for c in known})                          # gõ tên đầy đủ ecommerce-xxx cũng khớp
    aliases.update({a: c for a, c in EXTRA_ALIASES.items() if c in known})
    found = []
    for alias in sorted(aliases, key=len, reverse=True):          # alias dài trước: nginx-exporter thắng nginx
        if re.search(rf"(?<![a-z0-9-]){re.escape(alias)}(?![a-z0-9-])", t):
            if aliases[alias] not in found:
                found.append(aliases[alias])
            t = re.sub(rf"(?<![a-z0-9-]){re.escape(alias)}(?![a-z0-9-])", " ", t)   # đã dùng, không khớp lại bởi alias ngắn hơn
    t_act = re.sub(r"\s+", " ", t)
    actions = []
    if NL_RESTART.search(t_act):
        actions.append("restart_container")
        t_act = NL_RESTART.sub(" ", t_act)                         # "khởi động lại" không được tính thêm là "khởi động"
    if NL_START.search(t_act):
        actions.append("start_container")
    if NL_LOGS.search(t_act):
        actions.append("logs_tail")
    if NL_STATUS.search(t_act):
        actions.append("inspect_container")
    return {"actions": actions, "containers": found, "dangerous": bool(NL_DANGEROUS.search(t_act)),
            "question": bool(NL_QUESTION.search(normalize_text(text))) or "?" in text or normalize_text(text).endswith(" khong"),
            "overview": bool(NL_OVERVIEW.search(normalize_text(text)))}


def propose_manual(policy, action_id, container, uid, shown, force_confirm=False):
    if not force_confirm and policy["actions"].get(action_id, {}).get("approval") == "none":
        return run_read_now(policy, action_id, container, uid, shown)     # chỉ đọc: chạy ngay, không cần nút
    audit("manual_command", approver=uid, action_id=action_id, container=container[:80], raw=shown[:100])
    handle_propose(policy, {"alert": "Lệnh gõ tay", "fingerprint": "manual-" + secrets.token_hex(4),
                            "action_id": action_id, "container": container, "round": 1, "manual": True,
                            "reason": f"người dùng {uid} nói: {shown[:80]}"})


def smart_start(policy, container, uid, shown):
    """Người nói "chạy/bật <container>" bằng câu tự nhiên: nếu nó ĐANG chạy thì `start` vô ích (Docker trả 304) nên nói rõ
    và đề xuất khởi động lại (vẫn cần nút Duyệt). Trả True nếu đã xử lý."""
    if not check_proposal(policy, "inspect_container", container)[0]:
        return False
    try:
        code, out = docker_call(policy, "inspect_container", container)
        shaped = shape_read_output("inspect_container", code, out)
    except Exception:
        return False
    d = shaped.get("data") if shaped.get("ok") and isinstance(shaped.get("data"), dict) else None
    if not d or not d.get("running"):
        return False
    up = human_uptime(d.get("started_at")) or "?"
    tg_send(f"ℹ️ <code>{esc(container)}</code> <b>đang chạy rồi</b> (chạy {esc(up)}), nên không cần bật. "
            f"Nếu bạn muốn khởi động lại nó thì bấm Duyệt ở tin dưới.")
    propose_manual(policy, "restart_container", container, uid, shown)
    return True


def offer_choices(policy, pairs, uid, shown):
    """Câu nói mơ hồ: mỗi cách hiểu hợp lệ là 1 nút Duyệt riêng (bấm 1 nút thì các nút còn lại bị huỷ)."""
    valid = [(a, c) for a, c in pairs if check_proposal(policy, a, c)[0]]
    if not valid:                                                   # không cách hiểu nào được phép: hiện lý do của cách đầu tiên
        propose_manual(policy, pairs[0][0], pairs[0][1], uid, shown)
        return
    audit("manual_command", approver=uid, action_id="mơ hồ", container=None, raw=shown[:100])
    fp, group = "manual-" + secrets.token_hex(4), secrets.token_hex(4)
    with lock:
        ps = [new_pending(policy, dict(alert="Lệnh gõ tay", fingerprint=fp, action_id=a, container=c,
                                       ai_reason=f"người dùng {uid} nói: {shown[:80]}", manual=True), 1, group) for a, c in valid]
    rows = [[{"text": f"✅ {describe(policy, p)[:28]} · {p['container'].replace('ecommerce-', '')}"[:60],
              "callback_data": callback_data("a", p["id"])}] for p in ps]
    mid = tg_send("🤔 Tôi hiểu câu của bạn có thể là một trong các việc sau. Chọn đúng việc cần làm "
                  "(không chọn thì không có gì chạy):\n" + "\n".join(f"{i}. <code>{esc(p['action_id'])} {esc(p['container'])}</code>"
                                                                       for i, p in enumerate(ps, 1)), buttons=rows)
    with lock:
        for p in ps:
            if mid is None:
                pending.pop(p["id"], None)
            else:
                p["msg_id"] = mid


def ask_ai_chat(text, uid, sid=None):
    """Đường B / phiên /ai: nhờ AI (holmes-bridge /chat) hiểu và xử lý bằng công cụ. Trả True nếu đã gửi yêu cầu.
    Có `sid` = đang trong phiên /ai: bridge giữ trí nhớ hội thoại theo sid."""
    if not (BRIDGE_URL and TOOL_TOKEN):
        return False
    body = {"text": text[:500], "uid": uid}
    if sid:
        body["sid"] = sid
    req = urllib.request.Request(f"{BRIDGE_URL}/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOOL_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = r.status == 200
    except urllib.error.HTTPError as e:
        audit("chat_request", approver=uid, raw=text[:100], result=f"AI từ chối (HTTP {e.code})")
        tg_send("⏸ AI chưa nhận được yêu cầu (HTTP %d: có thể đã hết lượt phân tích trong giờ). Thử lại sau hoặc dùng /help." % e.code)
        return True
    except Exception as e:
        audit("chat_request", approver=uid, raw=text[:100], result=f"lỗi {type(e).__name__}")
        tg_send("⚠️ Không gọi được AI. Dùng /help để xem lệnh gõ trực tiếp.")
        return True
    audit("chat_request", approver=uid, raw=text[:100], result="queued" if ok else "lỗi", session=sid)
    if sid:
        tg_call("sendChatAction", {"chat_id": TG_CHAT, "action": "typing"}, timeout=10)   # đang gõ... (không spam tin)
    else:
        tg_send("🤖 Đang nhờ AI hiểu và xử lý… (nếu AI đề xuất lệnh, bạn sẽ thấy nút Duyệt)")
    return True


def handle_natural(policy, text, uid):
    it = parse_intent(policy, text)
    acts, conts = it["actions"], it["containers"]
    if it["dangerous"] and conts:
        audit("manual_command", approver=uid, action_id="khong_hop_le", container=None, raw=text[:100])
        audit("denied", why="yêu cầu dừng/xoá/tắt container không có trong kho lệnh (policy.json)", approver=uid,
              alert="Lệnh gõ tay", manual=True, raw=text[:100])
        tg_send(f"⛔ <b>KHÔNG ĐƯỢC PHÉP</b>: <code>{esc(text[:100])}</code>\nLý do: dừng/xoá/tắt container không có trong kho lệnh "
                f"(chỉ có restart, start, xem log, xem trạng thái). Không chạy gì cả. Gõ /help để xem lệnh được phép.")
        return
    if (it["overview"] and not conts) or (acts == ["inspect_container"] and not conts):
        send_overview(policy, uid, text)                            # "các container thế nào?" -> báo cáo tổng quan ngay
        return
    if acts and conts:
        pairs = [(a, c) for a in acts for c in conts]
        read_pairs = [(a, c) for a, c in pairs if policy["actions"].get(a, {}).get("approval") == "none"]
        pairs = [x for x in pairs if x not in read_pairs]
        for a, c in read_pairs[:3]:                                 # đọc thì chạy ngay (tối đa 3 việc)
            run_read_now(policy, a, c, uid, text)
        if not pairs:
            return
        if len(pairs) == 1:
            if pairs[0][0] == "start_container" and smart_start(policy, pairs[0][1], uid, text):
                return
            propose_manual(policy, pairs[0][0], pairs[0][1], uid, text)
        elif len(pairs) <= 3:
            offer_choices(policy, pairs, uid, text)
        else:
            tg_send("🤔 Câu này nhắm tới quá nhiều việc cùng lúc. Hãy nói cụ thể từng việc, ví dụ <code>restart backend</code>.")
        return
    if acts or conts or it["question"]:
        if ask_ai_chat(text, uid):
            return
        tg_send("🤔 Tôi chưa hiểu ý bạn. Hãy nói rõ cả việc cần làm và container, ví dụ <code>bật lại db</code> hoặc "
                "<code>xem log nginx</code>. Gõ /help để xem hướng dẫn.")


def handle_message(policy, msg):
    """Lệnh gõ trong chat: /restart|/start|/logs <container>, /help, /stop [tên_cảnh_báo].
    Chỉ nhận từ đúng chat/người duyệt; người lạ gõ lệnh bị ghi audit và bỏ qua."""
    text = (msg.get("text") or "").strip()
    ai_cmd = re.fullmatch(r"/(?:ai|exit|thoat)(?:@\w+)?(?:\s+(.{1,500}))?", text, re.S)
    m = re.fullmatch(r"/(stop|restart|start|logs|status|help)(?:@\w+)?(?:\s+(\S{1,80}))?", text)
    d = re.fullmatch(r"docker\s+([A-Za-z][\w-]{0,20})(?:\s+(.{0,150}))?", text, re.IGNORECASE)
    cid = str((msg.get("chat") or {}).get("id", ""))
    uid = str((msg.get("from") or {}).get("id", ""))
    authorized = is_authorized(cid, uid)
    if ai_cmd:
        if not authorized:
            audit("unauthorized_command", approver=uid, why="sai chat hoặc không nằm trong REMEDIATOR_APPROVER_IDS")
            return
        cmd_word = text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd_word in ("exit", "thoat"):
            return end_session(uid)
        return handle_ai_command(policy, ai_cmd.group(1), uid)
    if not m and not d:
        # Chữ thường: chỉ xử lý nếu đúng chat + đúng người; người khác nói chuyện trong chat thì im lặng bỏ qua (không ghi audit).
        if text and not text.startswith("/") and len(text) <= 500 and authorized:
            if session_sid():                       # đang trong phiên /ai: mọi tin nhắn đều tới AI (ngắn như "ok" cũng được)
                handle_session_message(policy, text, uid)
            elif len(text) >= 4:
                handle_natural(policy, text, uid)
        return
    if d and not m:   # "docker restart backend" gõ như ngoài terminal
        if not authorized:
            audit("unauthorized_command", approver=uid, why="sai chat hoặc không nằm trong REMEDIATOR_APPROVER_IDS")
            return
        verb = d.group(1).lower()
        if verb == "ps" and not first_arg(d.group(2)):
            return send_overview(policy, uid, text)               # "docker ps" như ngoài terminal -> báo cáo tình trạng
        if verb in MANUAL_CMDS:
            handle_manual(policy, verb, first_arg(d.group(2)), uid)
        else:
            refuse_manual(verb, (d.group(2) or "")[:80], uid)
        return
    if not authorized:
        audit("unauthorized_stop" if m.group(1) == "stop" else "unauthorized_command", approver=uid,
              why="sai chat hoặc không nằm trong REMEDIATOR_APPROVER_IDS")
        return
    if m.group(1) == "help":
        tg_send(HELP_TEXT)
        return
    if m.group(1) in MANUAL_CMDS:
        handle_manual(policy, m.group(1), m.group(2) or "", uid)
        return
    name = (m.group(2) or "").lower()
    with lock:
        cands = sorted((lp["ts"], fp) for fp, lp in loops.items()
                       if lp["state"] == "active" and (not name or lp["alert"].lower() == name))
    if not cands:
        tg_send("ℹ️ Không có vòng xử lý tự động nào đang chạy" + (f" cho cảnh báo {esc(name)}" if name else "") + ".")
        return
    for _, fp in (cands if name else cands[-1:]):   # có tên = dừng hết các vòng của cảnh báo đó; không tên = vòng mới nhất
        stop_loop(fp, uid)


def expire_pending(policy):
    session_sid()                  # phiên /ai quá hạn im lặng thì đóng và báo
    now = time.time()
    with lock:
        gone = [pid for pid, p in pending.items() if now > p["expires"]]
        items = [pending.pop(pid) for pid in gone]
        for k in [k for k, lp in loops.items() if now - lp["ts"] > 48 * 3600]:
            loops.pop(k)
    for p in items:
        if p["state"] == "proposed":
            audit("expired", id=p["id"], alert=p["alert"], action_id=p["action_id"], container=p["container"], round=p.get("round"))
    drop_markup(p["msg_id"] for p in items)
    for p in items:   # không ai duyệt trong hạn -> vòng tự kết thúc
        if p["state"] == "proposed":
            with lock:
                lp = loops.get(p["fingerprint"])
                if lp and lp["state"] == "active":
                    lp["state"] = "done"
            tg_send(f"⌛ Đề xuất <code>{esc(p['action_id'])}</code> → <code>{esc(p['container'])}</code> đã hết hạn duyệt, "
                    f"vòng xử lý tự động kết thúc.", reply_to=p.get("msg_id"))


def skip_backlog():
    """Bỏ qua các tin/nút bấm gửi lúc remediator đang tắt (Telegram giữ tới 24 giờ): lệnh cũ không được chạy bất ngờ."""
    res = tg_call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
    if res:
        last = res[-1]["update_id"]
        log.info("Bỏ qua tin Telegram cũ (tới update_id %d) gửi lúc remediator đang tắt", last)
        return last + 1
    return 0


def poll_loop(policy):
    offset = skip_backlog()
    while True:
        expire_pending(policy)
        res = tg_call("getUpdates", {"offset": offset, "timeout": 25,
                                     "allowed_updates": ["callback_query", "message"]}, timeout=40)
        if res is None:
            time.sleep(5)
            continue
        for u in res:
            offset = max(offset, u["update_id"] + 1)
            try:
                if "callback_query" in u:
                    handle_callback(policy, u["callback_query"])
                elif "message" in u:
                    handle_message(policy, u["message"])
            except Exception:
                log.exception("Lỗi khi xử lý cập nhật Telegram")


# ---------------------------------------------------------------- HTTP (chỉ nội bộ)
def make_handler(policy):
    class Handler(BaseHTTPRequestHandler):
        timeout = 20      # giây: kết nối treo (gửi chậm từng byte) bị cắt, không giữ luồng mãi
        def _reply(self, code, text):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        def _json(self, code, obj):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _tool_auth(self):
            if tool_auth_ok(self.headers.get("Authorization")):
                return True
            if self.command == "POST":
                try:
                    self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 100_000))   # đọc bỏ thân yêu cầu rồi mới trả 401
                except (ValueError, OSError):
                    pass
            audit("tool_denied_auth", why="thiếu/sai token công cụ" if TOOL_TOKEN else "tool API đang tắt (chưa đặt REMEDIATOR_TOOL_TOKEN)")
            self._json(401 if TOOL_TOKEN else 503, {"status": "denied", "message": "unauthorized" if TOOL_TOKEN else "tool api disabled"})
            return False

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path)
            if path.path == "/health":
                return self._reply(200, "ok")
            if path.path == "/tool/catalog":
                return self._json(200, tool_catalog(policy)) if self._tool_auth() else None
            if path.path == "/tool/state":
                return self._json(200, tool_state()) if self._tool_auth() else None
            if path.path == "/tool/overview":
                return self._json(200, tool_overview(policy)) if self._tool_auth() else None
            if path.path == "/tool/session":
                if not self._tool_auth():
                    return
                q = urllib.parse.parse_qs(path.query)
                try:
                    rnd = int((q.get("round") or ["1"])[0])
                except ValueError:
                    rnd = 1
                return self._json(200, tool_session_info((q.get("fingerprint") or [""])[0][:80], rnd))
            self._reply(404, "not found")

        def do_POST(self):
            if self.path not in ("/propose", "/close", "/tool/read", "/tool/propose"):
                return self._reply(404, "not found")
            # /tool/* luôn cần token. /propose, /close cũng cần khi đã đặt token (để không container nào khác trong mạng
            # giả đề xuất, huỷ đề xuất). Chưa đặt token = chế độ cũ (mở), có cảnh báo lúc khởi động.
            if (self.path.startswith("/tool/") or TOOL_TOKEN) and not self._tool_auth():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                return self._reply(413, "too large")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                return self._reply(400, "bad json")
            if self.path == "/tool/read":
                code, res = tool_read(policy, payload)
                return self._json(code, res)
            if self.path == "/tool/propose":
                code, res = tool_propose(policy, payload)
                return self._json(code, res)
            if self.path == "/close":
                close_loop(str(payload.get("fingerprint", ""))[:80], str(payload.get("reason", "cảnh báo đã hết"))[:100])
                return self._reply(200, "ok")
            # Cờ manual/tool chỉ do code bên trong đặt; từ HTTP thì bỏ (không giả được "LỆNH BẠN VỪA GÕ")
            payload.pop("manual", None)
            payload.pop("tool", None)
            code, msg = handle_propose(policy, payload)
            self._reply(code, msg)

        def log_message(self, fmt, *args):
            pass
    return Handler


def main():
    policy = load_policy()
    POLICY_FOR_STATE.update(policy)
    log.info("remediator :%d · chế độ %s · %s · Telegram %s · %d action, %d container cho phép · tool API %s",
             PORT, MODE.upper(), "BẬT" if ENABLED else "TẮT (kill switch)",
             "có" if TG_TOKEN and TG_CHAT else "THIẾU", len(policy["actions"]), len(policy["containers_allowed"]),
             "BẬT" if TOOL_TOKEN else "TẮT (chưa đặt REMEDIATOR_TOOL_TOKEN)")
    if not TOOL_TOKEN:
        log.warning("CHƯA đặt REMEDIATOR_TOOL_TOKEN: /propose, /close KHÔNG xác thực (container khác trong mạng gọi được). Nên đặt token.")
    if group_without_approvers():
        log.error("TELEGRAM_CHAT_ID là NHÓM nhưng chưa có REMEDIATOR_APPROVER_IDS: CHẶN mọi lệnh/nút duyệt cho tới khi khai báo người duyệt")
        if TG_TOKEN:
            tg_send("⚠️ <b>Remediator đang CHẶN mọi lệnh và nút Duyệt</b>: chat này là nhóm nhưng chưa khai báo "
                    "<code>REMEDIATOR_APPROVER_IDS</code> (ai trong nhóm cũng sẽ duyệt được). Điền user id người được duyệt vào "
                    "<code>.env.monitoring</code> rồi khởi động lại remediator.")
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=poll_loop, args=(policy,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(policy)).serve_forever()


if __name__ == "__main__":
    main()
