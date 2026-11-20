#!/usr/bin/env python3
"""
Cầu nối AI: Alertmanager (webhook) -> HolmesGPT (Bedrock) -> Telegram.

Luồng:
  1. Cảnh báo FIRING   -> Alertmanager gọi POST /alert (song song với tin Telegram gốc)
  2. Cầu nối chạy `holmes ask` (đọc Prometheus + Loki, nhờ Bedrock suy luận)
  3. PHÂN TÍCH XONG mới gửi 1 tin Telegram: Vấn đề · Nguyên nhân · Bằng chứng · Hướng xử lý
  4. Cảnh báo RESOLVED -> hỏi lại Prometheus (không tốn tiền Bedrock), gửi tin
     "đã hoạt động bình thường", trả lời (reply) ngay dưới tin phân tích ở bước 3.
  5. VÒNG LẶP: remediator chạy lệnh đã duyệt xong mà chưa ổn (hoặc người duyệt muốn cách khác)
     -> POST /continue kèm kết quả -> chạy lại AI (vòng sau, có lịch sử lệnh) -> đề xuất mới.
     Tối đa AI_MAX_ROUNDS vòng; mỗi vòng = 1 lượt Bedrock và tính vào AI_MAX_PER_HOUR.
     Cảnh báo RESOLVED -> báo remediator /close để huỷ đề xuất đang chờ.

Quy tắc:
  - Mỗi sự cố (fingerprint + startsAt) chỉ phân tích 1 lần: tin nhắc lại mỗi giờ
    của Alertmanager không làm tốn thêm tiền Bedrock.
  - Giới hạn AI_MAX_PER_HOUR lần phân tích mỗi giờ; chỉ các mức trong AI_SEVERITIES.
  - Phân tích lần lượt từng sự cố (1 luồng), không chạy song song.
  - AI lỗi thì gửi 1 dòng báo lỗi ngắn; tin cảnh báo gốc của Alertmanager không bị ảnh hưởng.

Chỉ dùng thư viện chuẩn của Python. Không in token/key ra log.
"""
import html
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VN = timezone(timedelta(hours=7))
LINE = "━━━━━━━━━━━━━━"


def _secret(name):
    """Đọc token/ID từ biến môi trường, bỏ dấu cách, ngoặc kép, \\r lỡ dán thừa."""
    return re.sub(r"[\s\"']", "", os.environ.get(name, ""))


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PORT = _int("BRIDGE_PORT", 8080)
TG_TOKEN = _secret("TELEGRAM_BOT_TOKEN")
TG_CHAT = _secret("TELEGRAM_CHAT_ID")
TG_API = (os.environ.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/")
PROM_URL = (os.environ.get("PROMETHEUS_URL") or "http://prometheus:9090").rstrip("/")
SEVERITIES = {s.strip() for s in os.environ.get("AI_SEVERITIES", "critical,warning").split(",") if s.strip()}
MAX_PER_HOUR = _int("AI_MAX_PER_HOUR", 6)
MAX_STEPS = _int("AI_MAX_STEPS", 15)
TIMEOUT = _int("AI_TIMEOUT_SECONDS", 300)
CONFIRM_DELAY = _int("AI_CONFIRM_DELAY_SECONDS", 10)
# Script của image ecommerce-holmes: làm sạch biến AWS, kiểm tra khoá rồi chạy `holmes`
HOLMES_CMD = os.environ.get("HOLMES_CMD", "/usr/local/bin/holmes-entrypoint.sh")
# Runbook markdown (mỗi alertname 1 file) được chèn vào prompt để AI điều tra đúng hướng, nhanh hơn
RUNBOOK_DIR = os.environ.get("RUNBOOK_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "runbooks"))
RUNBOOK_ALIASES = {"HostDiskFull": "HostDiskAlmostFull"}  # 2 alert dùng chung 1 runbook
RUNBOOK_MAX_CHARS = 4000
# Để trống = tắt đề xuất lệnh sửa lỗi. Bật: http://remediator:8090 (AI chỉ ĐỀ XUẤT, người duyệt trên Telegram)
REMEDIATOR_URL = (os.environ.get("REMEDIATOR_URL") or "").rstrip("/")
# Số vòng AI tối đa cho 1 sự cố (vòng 1 = phân tích ban đầu). 1 = tắt vòng lặp.
MAX_ROUNDS = max(1, _int("AI_MAX_ROUNDS", 3))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("holmes-bridge")

state = {}            # fingerprint -> {"starts", "msg_id", "resolved", "ts"}
state_lock = threading.Lock()
jobs = queue.Queue()
recent_runs = deque()  # thời điểm các lần phân tích gần đây (giới hạn mỗi giờ)


# ---------------------------------------------------------------- tiện ích
def parse_time(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def vn_time(s, fmt="%H:%M:%S %d/%m/%Y"):
    t = parse_time(s)
    return t.astimezone(VN).strftime(fmt) if t else "?"


def human_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} giờ {m} phút"
    if m:
        return f"{m} phút {s} giây"
    return f"{s} giây"


def esc(s):
    return html.escape(s or "", quote=False)


def md_to_html(s):
    """Escape HTML rồi đổi **đậm** và `mã` của model sang thẻ Telegram hiểu được."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return s


def has_bedrock_creds():
    region = os.environ.get("AWS_REGION_NAME", "").strip()
    bearer = _secret("AWS_BEARER_TOKEN_BEDROCK")
    keys = _secret("AWS_ACCESS_KEY_ID") and _secret("AWS_SECRET_ACCESS_KEY")
    return bool(region and (bearer or keys))


# ---------------------------------------------------------------- Telegram
def tg_send(text, reply_to=None):
    if not (TG_TOKEN and TG_CHAT):
        log.warning("Chưa có TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, không gửi được tin")
        return None
    body = {"chat_id": TG_CHAT, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if reply_to:
        body["reply_to_message_id"] = reply_to
        body["allow_sending_without_reply"] = True
    url = f"{TG_API}/bot{TG_TOKEN}/sendMessage"
    for attempt in range(2):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("result", {}).get("message_id")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # Model trả về ký tự làm hỏng HTML -> gửi lại dạng chữ thường
            if attempt == 0 and "parse entities" in detail:
                body.pop("parse_mode")
                body["text"] = re.sub(r"</?(b|i|code)>", "", body["text"])
                body["text"] = html.unescape(body["text"])
                continue
            log.error("Telegram từ chối tin (HTTP %s): %s", e.code, detail)
            return None
        except Exception as e:  # mạng lỗi
            log.error("Không gửi được Telegram: %s", type(e).__name__)
            return None
    return None


# ---------------------------------------------------------------- Prometheus
def prom_query(q):
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": q})
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]["result"]


# ---------------------------------------------------------------- HolmesGPT
def load_runbook(alertname):
    """Đọc runbook của alert. Tên alert chỉ được dùng nếu khớp [A-Za-z0-9_] để không thoát khỏi thư mục."""
    name = RUNBOOK_ALIASES.get(alertname, alertname)
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        return ""
    try:
        with open(os.path.join(RUNBOOK_DIR, name + ".md"), encoding="utf-8") as f:
            return f.read(RUNBOOK_MAX_CHARS).strip()
    except OSError:
        return ""


ACTION_HINT = ("\nHÀNH ĐỘNG ĐỀ XUẤT: <đúng 1 dòng dạng `action_id container`, ví dụ `restart_container ecommerce-backend`; "
               "action_id chỉ được là restart_container, start_container hoặc logs_tail; "
               "chỉ đề xuất khi runbook cho phép và bằng chứng rõ ràng, nếu không thì ghi `không có`. "
               "Đây chỉ là đề xuất, con người sẽ duyệt>")


def build_followup_prompt(a, payload, rnd):
    """Prompt cho vòng sau: cùng cảnh báo + runbook, kèm lịch sử lệnh đã chạy và kết quả của chúng."""
    labels = a.get("labels", {})
    runbook = load_runbook(labels.get("alertname", ""))
    label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    lines = []
    for h in (payload.get("history") or [])[-5:]:
        out = re.sub(r"\s+", " ", str(h.get("output", "")))[-300:]
        lines.append(f"- vòng {h.get('round', '?')}: `{h.get('action_id')} {h.get('container')}` -> "
                     f"{'thành công' if h.get('ok') else 'LỖI'} (HTTP {h.get('http')}); kết quả: {out or '(trống)'}")
    down = ", ".join(str(x) for x in (payload.get("down_jobs") or [])[:10]) or "không có"
    return f"""Đây là VÒNG {rnd}/{MAX_ROUNDS} xử lý cảnh báo "{labels.get('alertname', '?')}" (mức {labels.get('severity', '?')}) \
của hệ thống web "Nova Store" (Docker Compose; nginx -> backend Node.js -> MySQL). Cảnh báo VẪN chưa kết thúc.

Nhãn cảnh báo: {label_text}

Các lệnh đã được người vận hành duyệt và chạy ở các vòng trước:
{chr(10).join(lines) or '- (chưa có lệnh nào được chạy)'}
Tình trạng sau lệnh gần nhất: {esc(str(payload.get('status', '')))[:200]}. Thành phần Prometheus đang chưa trả lời (up==0): {down}.

Runbook (đừng lặp lại cách đã thử mà không hiệu quả):
{runbook or '(không có runbook)'}

Hãy điều tra lại bằng Prometheus và Loki (chỉ đọc) rồi cho biết bước tiếp theo. Chỉ đề xuất lệnh KHÁC với các lệnh đã chạy; nếu không còn gì an toàn để thử thì ghi `không có` và nói rõ cần người xử lý tay.

Trả lời bằng tiếng Việt, ĐÚNG khuôn, không thêm tiêu đề markdown, tổng không quá 1000 ký tự:
VẤN ĐỀ: <tình trạng hiện tại sau các lệnh trước>
NGUYÊN NHÂN: <nguyên nhân gốc theo bằng chứng mới nhất; ghi rõ nếu là phỏng đoán>
BẰNG CHỨNG:
- <số liệu hoặc dòng log mới, tối đa 3 ý>
HƯỚNG XỬ LÝ:
1. <bước cụ thể, tối đa 3 bước>{ACTION_HINT if REMEDIATOR_URL else ''}"""


def build_prompt(a):
    labels = a.get("labels", {})
    ann = a.get("annotations", {})
    label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    runbook = load_runbook(labels.get("alertname", ""))
    runbook_text = (f"\nRunbook của cảnh báo này (kinh nghiệm của đội vận hành; làm theo thứ tự điều tra, "
                    f"vẫn phải dựa vào dữ liệu thật để kết luận, không chép nguyên văn):\n{runbook}\n") if runbook else ""
    action_text = ACTION_HINT if REMEDIATOR_URL else ""
    return f"""Alertmanager vừa báo một cảnh báo chuyển sang FIRING trong hệ thống web bán hàng "Nova Store" chạy bằng Docker Compose.

Cảnh báo:
- Tên: {labels.get('alertname', '?')}
- Mức độ: {labels.get('severity', '?')}
- Bắt đầu: {vn_time(a.get('startsAt'))} (giờ Việt Nam)
- Tóm tắt: {ann.get('summary', '')}
- Mô tả: {ann.get('description', '')}
- Nhãn: {label_text}

Bối cảnh: người dùng -> nginx -> backend Node.js -> MySQL (service "db"). Số liệu nằm ở Prometheus (job: backend, nginx, mysql, node, prometheus, alertmanager). Log nằm ở Loki với nhãn service (backend, nginx, db, ...), level (error/warn/info) và container.

{runbook_text}
Hãy điều tra bằng Prometheus và Loki để tìm nguyên nhân gốc. Chỉ đọc dữ liệu, không thay đổi hệ thống.

Trả lời bằng tiếng Việt, ĐÚNG khuôn dưới đây, không thêm tiêu đề markdown, tổng không quá 1200 ký tự:
VẤN ĐỀ: <1-2 câu: chuyện gì đang xảy ra, ảnh hưởng tới người dùng ra sao>
NGUYÊN NHÂN: <1-3 câu: nguyên nhân gốc; nếu chưa chắc chắn thì ghi rõ là phỏng đoán>
BẰNG CHỨNG:
- <số liệu hoặc dòng log cụ thể đã tìm được, tối đa 3 ý>
HƯỚNG XỬ LÝ:
1. <bước cụ thể, ưu tiên lệnh docker compose, tối đa 4 bước>{action_text}"""


def run_holmes(a, prompt=None):
    """Trả về (kết_quả, lỗi). Chạy holmes ask, lấy câu trả lời từ file JSON."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "result.json")
        cmd = shlex.split(HOLMES_CMD) + ["ask", prompt or build_prompt(a), "--no-interactive", "--no-echo",
               "--max-steps", str(MAX_STEPS), "--bash-always-deny", "--json-output-file", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, f"quá {TIMEOUT} giây chưa phân tích xong"
        if os.path.exists(out):
            try:
                with open(out, encoding="utf-8") as f:
                    result = (json.load(f).get("result") or "").strip()
                if result:
                    return result, None
            except (OSError, ValueError):
                pass
        tail = re.sub(r"\x1b\[[0-9;]*m", "", (p.stderr or "") + (p.stdout or "")).strip()
        lines = [l.strip() for l in tail.splitlines() if l.strip()]
        reason = " / ".join(lines[-2:])[-300:] if lines else f"holmes thoát với mã {p.returncode}"
        return None, reason


SECTION_RE = re.compile(r"^[\s#*>\-]*(VẤN ĐỀ|NGUYÊN NHÂN|BẰNG CHỨNG|HƯỚNG XỬ LÝ)[\s*:]*(.*)$", re.IGNORECASE)
SECTIONS = [("VẤN ĐỀ", "Vấn đề"), ("NGUYÊN NHÂN", "Nguyên nhân"),
            ("BẰNG CHỨNG", "Bằng chứng"), ("HƯỚNG XỬ LÝ", "Hướng xử lý")]


def parse_sections(text):
    found, current = {}, None
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1).upper()
            found[current] = [m.group(2).strip()] if m.group(2).strip() else []
        elif current is not None and line.strip():
            found[current].append(line.rstrip())
    return {k: "\n".join(v).strip() for k, v in found.items()}


ACTION_RE = re.compile(r"^[\s#*>\-]*HÀNH ĐỘNG ĐỀ XUẤT[\s*:]*(.*)$", re.IGNORECASE)
ACTION_VALUE_RE = re.compile(r"`?\s*([a-z_]{3,40})\s+(ecommerce-[A-Za-z0-9_.-]{1,60})\s*`?")
NO_ACTION_RE = re.compile(r"^[\s`*_.\-]*(không( có)?|khong( co)?|none|n/?a|không đề xuất)?[\s`*_.\-]*$", re.IGNORECASE)


def extract_action(text):
    """Bóc dòng 'HÀNH ĐỘNG ĐỀ XUẤT' khỏi kết quả AI. Trả (văn_bản_đã_bóc, đề_xuất|None).
    Đúng dạng 'action_id ecommerce-xxx' -> đề xuất bình thường. Dòng khác (không phải 'không có') ví dụ
    'docker exec ...' -> vẫn chuyển đi kèm `raw` để remediator báo admin "policy chặn" + lệnh thay thế
    thay vì bỏ im lặng. remediator luôn kiểm tra lại theo policy."""
    kept, proposal = [], None
    for line in text.splitlines():
        m = ACTION_RE.match(line)
        if not m:
            kept.append(line)
            continue
        value = m.group(1).strip()
        v = ACTION_VALUE_RE.fullmatch(value)
        if proposal is not None:
            continue
        if v:
            proposal = {"action_id": v.group(1), "container": v.group(2)}
        elif not NO_ACTION_RE.match(value):
            proposal = {"action_id": "khong_hop_le", "container": "", "raw": re.sub(r"[`\s]+", " ", value)[:100]}
    return "\n".join(kept).strip(), proposal


def send_proposal(a, proposal, analysis_text, rnd=1):
    """Chuyển đề xuất sang remediator. Lỗi ở đây không được làm hỏng tin phân tích."""
    if not REMEDIATOR_URL:
        return
    reason = parse_sections(analysis_text[:3000]).get("NGUYÊN NHÂN", "")[:200]
    body = {"alert": a.get("labels", {}).get("alertname", "?"), "fingerprint": a.get("fingerprint", ""),
            "reason": reason, "round": rnd, **proposal}
    req = urllib.request.Request(f"{REMEDIATOR_URL}/propose", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log.info("Đã gửi đề xuất %s %s (vòng %d) sang remediator (HTTP %s)", proposal["action_id"], proposal["container"], rnd, r.status)
    except urllib.error.HTTPError as e:
        log.info("Remediator từ chối đề xuất %s %s (HTTP %s)", proposal["action_id"], proposal["container"], e.code)
    except Exception as e:
        log.error("Không gửi được đề xuất sang remediator: %s", type(e).__name__)


def close_remediator_loop(fp):
    """Cảnh báo đã hết: báo remediator huỷ đề xuất đang chờ và kết thúc vòng. Lỗi ở đây không quan trọng."""
    if not REMEDIATOR_URL:
        return
    req = urllib.request.Request(f"{REMEDIATOR_URL}/close", data=json.dumps({"fingerprint": fp, "reason": "cảnh báo đã hết"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log.warning("Không báo được remediator đóng vòng: %s", type(e).__name__)


def header(a, title):
    labels = a.get("labels", {})
    return (f"{title}: {esc(labels.get('alertname', '?'))}</b>\n"
            f"Mức độ: {esc(labels.get('severity', '?').upper())} · Bắt đầu: {vn_time(a.get('startsAt'))}\n{LINE}")


def format_analysis(a, result, seconds, title="AI PHÂN TÍCH"):
    parts = parse_sections(result[:3000])
    if "VẤN ĐỀ" in parts or "NGUYÊN NHÂN" in parts:
        body = []
        for key, name in SECTIONS:
            val = parts.get(key)
            if not val:
                continue
            sep = "\n" if "\n" in val or key in ("BẰNG CHỨNG", "HƯỚNG XỬ LÝ") else " "
            body.append(f"<b>{name}:</b>{sep}{md_to_html(val)}")
        body = "\n\n".join(body)
    else:  # model không theo khuôn -> gửi nguyên văn
        body = md_to_html(result[:3000])
    return (f"🔎 <b>{header(a, title)}\n{body}\n{LINE}\n"
            f"<i>HolmesGPT + Bedrock · phân tích trong {human_duration(seconds)}. "
            f"Kết quả do AI đưa ra, cần người vận hành kiểm tra lại.</i>")


def format_failure(a, reason):
    return (f"⚠️ <b>{header(a, 'AI CHƯA PHÂN TÍCH ĐƯỢC')}\n"
            f"Lý do: {esc(reason)}\n"
            f"Tin cảnh báo gốc vẫn được gửi bình thường. Xem log: docker logs ecommerce-holmes-bridge")


def format_resolved(a):
    labels = a.get("labels", {})
    name = labels.get("alertname", "?")
    starts, ends = parse_time(a.get("startsAt")), parse_time(a.get("endsAt"))
    dur = human_duration((ends - starts).total_seconds()) if starts and ends else "?"
    checks, healthy = [], True
    try:
        others = []
        for s in prom_query('ALERTS{alertstate="firing"}'):
            m = {k: v for k, v in s["metric"].items() if k not in ("__name__", "alertstate")}
            if m != labels:
                others.append(m.get("alertname", "?"))
        ups = prom_query("up")
        down = sorted({s["metric"].get("job", "?") for s in ups if s["value"][1] == "0"})
        if down:
            healthy = False
            checks.append(f"• {len(ups) - len(down)}/{len(ups)} thành phần đang trả lời. Chưa trả lời: {esc(', '.join(down))}")
        else:
            checks.append(f"• {len(ups)}/{len(ups)} thành phần đang trả lời Prometheus")
        if others:
            healthy = False
            checks.append(f"• Còn {len(others)} cảnh báo khác đang bật: {esc(', '.join(sorted(set(others))))}")
        else:
            checks.append("• Không còn cảnh báo nào đang bật")
    except Exception as e:
        healthy = False
        checks.append(f"• Không kiểm tra lại được Prometheus ({type(e).__name__})")

    title = (f"🟢 <b>ĐÃ HOẠT ĐỘNG BÌNH THƯỜNG: {esc(name)}</b>" if healthy
             else f"🟡 <b>{esc(name)} ĐÃ HẾT, NHƯNG HỆ THỐNG CHƯA ỔN HẲN</b>")
    return (f"{title}\n"
            f"Sự cố: {esc(a.get('annotations', {}).get('summary', ''))}\n"
            f"Thời gian: {vn_time(a.get('startsAt'), '%H:%M:%S')} → {vn_time(a.get('endsAt'), '%H:%M:%S %d/%m/%Y')} "
            f"(kéo dài {dur})\n{LINE}\n"
            f"Kiểm tra lại lúc {datetime.now(VN).strftime('%H:%M:%S')}:\n" + "\n".join(checks))


# ---------------------------------------------------------------- xử lý cảnh báo
def rate_ok():
    now = time.time()
    while recent_runs and now - recent_runs[0] > 3600:
        recent_runs.popleft()
    if len(recent_runs) >= MAX_PER_HOUR:
        return False
    recent_runs.append(now)
    return True


def handle_alert(a):
    labels = a.get("labels", {})
    fp = a.get("fingerprint") or json.dumps(labels, sort_keys=True)
    starts, status = a.get("startsAt"), a.get("status")
    name = labels.get("alertname", "?")
    with state_lock:
        for k in [k for k, v in state.items() if time.time() - v["ts"] > 48 * 3600]:
            state.pop(k)
        st = state.get(fp)
        if status == "firing":
            if st and st["starts"] == starts:
                return  # đã phân tích / đang chờ: bỏ qua tin nhắc lại của Alertmanager
            if labels.get("severity", "") not in SEVERITIES:
                log.info("Bỏ qua %s: mức %s không nằm trong AI_SEVERITIES", name, labels.get("severity"))
                return
            if not rate_ok():
                log.warning("Bỏ qua %s: đã đủ %d lần phân tích trong 1 giờ (AI_MAX_PER_HOUR)", name, MAX_PER_HOUR)
                return
            state[fp] = {"starts": starts, "msg_id": None, "resolved": False, "ts": time.time(), "alert": a, "round": 1}
            log.info("Nhận %s FIRING -> xếp hàng phân tích", name)
            jobs.put(("analyze", fp, a))
        elif status == "resolved":
            if not st or st["resolved"] or st["starts"] != starts:
                return  # sự cố này chưa từng được AI phân tích -> không gửi xác nhận
            st["resolved"] = True
            log.info("Nhận %s RESOLVED -> xếp hàng xác nhận", name)
            jobs.put(("resolved", fp, a))


def handle_continue(payload):
    """remediator gửi kết quả lệnh đã chạy -> xếp 1 lượt phân tích vòng sau. Trả (mã_http, thông_báo)."""
    fp = str(payload.get("fingerprint", ""))
    try:
        rnd = int(payload.get("round", 1))
    except (TypeError, ValueError):
        return 400, "bad round"
    with state_lock:
        st = state.get(fp)
        if not st or not REMEDIATOR_URL:
            return 404, "unknown incident"
        if st["resolved"]:
            return 409, "already resolved"
        if rnd + 1 > MAX_ROUNDS:
            return 429, "max rounds"
        if st.get("pending_followup"):
            return 200, "already queued"
        if not rate_ok():
            log.warning("Bỏ qua vòng %d của %s: đã đủ %d lần phân tích trong 1 giờ", rnd + 1, fp, MAX_PER_HOUR)
            tg_send(f"⏸ Chưa thể hỏi AI tiếp vì đã đủ {MAX_PER_HOUR} lượt phân tích trong 1 giờ (AI_MAX_PER_HOUR). "
                    f"Vòng xử lý tự động dừng; xử lý tay hoặc chờ sang giờ sau.", reply_to=st.get("msg_id"))
            return 429, "rate limited"
        st["pending_followup"] = True
    jobs.put(("followup", fp, payload))
    log.info("Nhận kết quả lệnh của %s -> xếp hàng phân tích vòng %d", fp, rnd + 1)
    return 200, "queued"


def worker():
    while True:
        kind, fp, a = jobs.get()
        name = a.get("labels", {}).get("alertname", "?")
        try:
            st = state.get(fp, {})
            if kind == "analyze":
                if st.get("resolved"):
                    log.info("%s đã hết trước khi kịp phân tích, bỏ qua bước AI", name)
                    continue
                if not has_bedrock_creds():
                    st["msg_id"] = tg_send(format_failure(a, "chưa cấu hình Bedrock (BEDROCK_REGION + AWS_BEARER_TOKEN_BEDROCK)"))
                    continue
                t0 = time.time()
                result, err = run_holmes(a)
                secs = time.time() - t0
                if result:
                    log.info("Phân tích xong %s trong %.0fs", name, secs)
                    result, proposal = extract_action(result)
                    st["msg_id"] = tg_send(format_analysis(a, result, secs))
                    if proposal:
                        send_proposal(a, proposal, result)
                else:
                    log.error("Phân tích %s thất bại: %s", name, err)
                    st["msg_id"] = tg_send(format_failure(a, err))
            elif kind == "followup":
                st["pending_followup"] = False
                rnd = int(a.get("round", 1)) + 1
                alert = st.get("alert") or {}
                name = alert.get("labels", {}).get("alertname", "?")
                if st.get("resolved"):
                    log.info("%s đã hết trước khi kịp phân tích vòng %d", name, rnd)
                    continue
                if not has_bedrock_creds():
                    tg_send(format_failure(alert, "chưa cấu hình Bedrock"), reply_to=st.get("msg_id"))
                    continue
                t0 = time.time()
                result, err = run_holmes(alert, build_followup_prompt(alert, a, rnd))
                secs = time.time() - t0
                if result:
                    log.info("Phân tích vòng %d xong %s trong %.0fs", rnd, name, secs)
                    result, proposal = extract_action(result)
                    st["round"] = rnd
                    tg_send(format_analysis(alert, result, secs, title=f"AI TIẾP TỤC (vòng {rnd}/{MAX_ROUNDS})"), reply_to=st.get("msg_id"))
                    if proposal:
                        send_proposal(alert, proposal, result, rnd)
                    else:
                        tg_send("🔚 AI không còn lệnh an toàn nào để đề xuất — cần người vận hành xử lý tay. Vòng xử lý tự động kết thúc.",
                                reply_to=st.get("msg_id"))
                else:
                    log.error("Phân tích vòng %d của %s thất bại: %s", rnd, name, err)
                    tg_send(format_failure(alert, err), reply_to=st.get("msg_id"))
            elif kind == "resolved":
                close_remediator_loop(fp)
                time.sleep(CONFIRM_DELAY)  # chờ Prometheus cập nhật lượt lấy số mới
                tg_send(format_resolved(a), reply_to=st.get("msg_id"))
                log.info("Đã gửi xác nhận %s hoạt động lại", name)
        except Exception:
            log.exception("Lỗi khi xử lý %s (%s)", name, kind)
        finally:
            jobs.task_done()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        self._reply(200, "ok") if self.path == "/health" else self._reply(404, "not found")

    def do_POST(self):
        if self.path not in ("/alert", "/continue"):
            return self._reply(404, "not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._reply(413, "too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/continue":
                if not isinstance(payload, dict):
                    return self._reply(400, "bad json")
                code, msg = handle_continue(payload)
                return self._reply(code, msg)
            for a in payload.get("alerts", []):
                handle_alert(a)
        except ValueError:
            return self._reply(400, "bad json")
        # Trả lời ngay để Alertmanager không chờ; phân tích chạy ở luồng riêng
        self._reply(200, "ok")

    def log_message(self, fmt, *args):
        pass  # không ghi log mỗi request (healthcheck)


def main():
    log.info("holmes-bridge chạy ở :%d · Telegram: %s · Bedrock: %s · mức: %s · tối đa %d lần/giờ · tối đa %d vòng/sự cố",
             PORT, "BẬT" if TG_TOKEN and TG_CHAT else "TẮT (thiếu token/chat id)",
             "có khoá" if has_bedrock_creds() else "CHƯA CÓ KHOÁ",
             ",".join(sorted(SEVERITIES)), MAX_PER_HOUR, MAX_ROUNDS)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
