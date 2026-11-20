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
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
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

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")

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
loops = {}      # fingerprint -> {"alert", "round", "history": [...], "ts", "state": active|done|stopped}


# ---------------------------------------------------------------- policy
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


def loop_of(fp, alert="?"):
    """Trạng thái vòng lặp của 1 sự cố (tạo nếu chưa có). Gọi khi đang giữ `lock`."""
    return loops.setdefault(fp, {"alert": alert, "round": 0, "history": [], "ts": time.time(), "state": "active"})


# ---------------------------------------------------------------- ký nút bấm
def sign(pid):
    return hmac.new(SECRET, pid.encode(), hashlib.sha256).hexdigest()[:16]


def callback_data(kind, pid):
    return f"{kind}:{pid}:{sign(pid)}"  # <= 64 byte theo giới hạn Telegram


def parse_callback(data):
    """Trả (kind, pid) nếu chữ ký đúng, ngược lại None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] not in ("a", "r", "n", "s"):
        return None
    if not hmac.compare_digest(parts[2], sign(parts[1])):
        return None
    return parts[0], parts[1]


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
    try:
        rnd = max(1, min(int(payload.get("round", 1)), 99))
    except (TypeError, ValueError):
        rnd = 1
    base = dict(alert=alert, fingerprint=fp, action_id=action_id if isinstance(action_id, str) else None,
                container=container if isinstance(container, str) else None, ai_reason=reason,
                manual=True if manual else None)

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

    loop_note = f" · vòng {rnd}/{MAX_ROUNDS}" if BRIDGE_URL and not manual else ""
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
    if not BRIDGE_URL or p.get("manual"):
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
    req = urllib.request.Request(f"{BRIDGE_URL}/continue", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
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
    audit("executed", command=command, executed=True, http_status=code, success=success,
          duration_s=secs, output_tail=out[-500:], **base)
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
        if BRIDGE_URL and not p.get("manual"):
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

    if cid != TG_CHAT or (APPROVERS and uid not in APPROVERS):
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
        if BRIDGE_URL and p["round"] < MAX_ROUNDS and not p.get("manual"):
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


MANUAL_CMDS = {"restart": "restart_container", "start": "start_container", "logs": "logs_tail"}
HELP_TEXT = ("⌨️ <b>Lệnh bạn có thể gõ</b> (mỗi lệnh vẫn qua policy và cần bấm Duyệt):\n"
             "<code>/restart &lt;container&gt;</code> — khởi động lại\n"
             "<code>/start &lt;container&gt;</code> — bật lại container đã dừng\n"
             "<code>/logs &lt;container&gt;</code> — xem 100 dòng log cuối\n"
             "<code>/stop [tên_cảnh_báo]</code> — dừng vòng xử lý tự động của AI\n"
             "Gõ kiểu terminal cũng được: <code>docker restart backend</code>. Tên container viết tắt được: <code>backend</code> = <code>ecommerce-backend</code>.\n"
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
    if name and not name.startswith("ecommerce-") and NAME_RE.fullmatch("ecommerce-" + name):
        name = "ecommerce-" + name
    if not name:
        tg_send(f"Thiếu tên container. Ví dụ: <code>/{cmd} ecommerce-backend</code>\n\n{HELP_TEXT}")
        return
    audit("manual_command", approver=uid, action_id=MANUAL_CMDS[cmd], container=name[:80])
    handle_propose(policy, {"alert": "Lệnh gõ tay", "fingerprint": "manual-" + secrets.token_hex(4),
                            "action_id": MANUAL_CMDS[cmd], "container": name, "round": 1, "manual": True,
                            "reason": f"người dùng {uid} gõ /{cmd} {name[:60]}"})


def handle_message(policy, msg):
    """Lệnh gõ trong chat: /restart|/start|/logs <container>, /help, /stop [tên_cảnh_báo].
    Chỉ nhận từ đúng chat/người duyệt; người lạ gõ lệnh bị ghi audit và bỏ qua."""
    text = (msg.get("text") or "").strip()
    m = re.fullmatch(r"/(stop|restart|start|logs|help)(?:@\w+)?(?:\s+(\S{1,80}))?", text)
    d = re.fullmatch(r"docker\s+([A-Za-z][\w-]{0,20})(?:\s+(.{0,150}))?", text, re.IGNORECASE)
    if not m and not d:
        return
    cid = str((msg.get("chat") or {}).get("id", ""))
    uid = str((msg.get("from") or {}).get("id", ""))
    if d and not m:   # "docker restart backend" gõ như ngoài terminal
        if cid != TG_CHAT or (APPROVERS and uid not in APPROVERS):
            audit("unauthorized_command", approver=uid, why="sai chat hoặc không nằm trong REMEDIATOR_APPROVER_IDS")
            return
        verb = d.group(1).lower()
        if verb in MANUAL_CMDS:
            handle_manual(policy, verb, first_arg(d.group(2)), uid)
        else:
            refuse_manual(verb, (d.group(2) or "")[:80], uid)
        return
    if cid != TG_CHAT or (APPROVERS and uid not in APPROVERS):
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


def poll_loop(policy):
    offset = 0
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
        def _reply(self, code, text):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        def do_GET(self):
            self._reply(200, "ok") if self.path == "/health" else self._reply(404, "not found")

        def do_POST(self):
            if self.path not in ("/propose", "/close"):
                return self._reply(404, "not found")
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                return self._reply(413, "too large")
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                return self._reply(400, "bad json")
            if self.path == "/close":
                close_loop(str(payload.get("fingerprint", ""))[:80], str(payload.get("reason", "cảnh báo đã hết"))[:100])
                return self._reply(200, "ok")
            code, msg = handle_propose(policy, payload)
            self._reply(code, msg)

        def log_message(self, fmt, *args):
            pass
    return Handler


def main():
    policy = load_policy()
    log.info("remediator :%d · chế độ %s · %s · Telegram %s · %d action, %d container cho phép",
             PORT, MODE.upper(), "BẬT" if ENABLED else "TẮT (kill switch)",
             "có" if TG_TOKEN and TG_CHAT else "THIẾU", len(policy["actions"]), len(policy["containers_allowed"]))
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=poll_loop, args=(policy,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(policy)).serve_forever()


if __name__ == "__main__":
    main()
