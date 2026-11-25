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
  7. CHAT 2 LỚP (nhanh): tin nhắn chat được định tuyến. Nói chuyện / hỏi cách bot hoạt động / kiến thức chung -> gọi THẲNG Bedrock
     bằng chính mô hình đã cấu hình (không công cụ, không khởi động Holmes, chữ hiện dần trên Telegram, ~1-3 giây). Cần số liệu thật
     hoặc lệnh -> Holmes + công cụ. AI trả đúng dấu hiệu [[OPS]] khi tự thấy câu cần dữ liệu thật => bridge chuyển sang Holmes.
  6. CÁCH 2 (công cụ): khi có REMEDIATOR_TOOL_TOKEN, AI không ghi dòng chữ nữa mà GỌI CÔNG CỤ
     (tools/remediation_tool.py: list_allowed_actions, read_status, propose_fix) -> remediator /tool/*.
     Danh tính sự cố đặt qua biến môi trường của tiến trình holmes (AI không tự đặt được). Mỗi vòng
     lưu TÓM TẮT phân tích để vòng sau không phải điều tra lại từ đầu (phiên AI không nhớ gì giữa các lần).

Quy tắc:
  - Mỗi sự cố (fingerprint + startsAt) chỉ phân tích 1 lần: tin nhắc lại mỗi giờ
    của Alertmanager không làm tốn thêm tiền Bedrock.
  - Giới hạn AI_MAX_PER_HOUR lần phân tích mỗi giờ; chỉ các mức trong AI_SEVERITIES.
  - Phân tích lần lượt từng sự cố (1 luồng), không chạy song song.
  - AI lỗi thì gửi 1 dòng báo lỗi ngắn; tin cảnh báo gốc của Alertmanager không bị ảnh hưởng.

Chỉ dùng thư viện chuẩn của Python. Không in token/key ra log.
"""
import hmac
import html
import importlib
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
import unicodedata
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
# Cách 2: AI gọi công cụ (cần token). Thiếu token hoặc AI_USE_TOOLS=false -> cách cũ (AI ghi 1 dòng chữ).
TOOL_TOKEN = _secret("REMEDIATOR_TOOL_TOKEN")
USE_TOOLS = bool(REMEDIATOR_URL and TOOL_TOKEN) and os.environ.get("AI_USE_TOOLS", "true").strip().lower() not in ("false", "0", "no", "off")
ANALYSIS_SUMMARY_CHARS = 1500
# Bản đồ hệ thống (knowledge/system.md) chèn vào prompt để AI hiểu quan hệ giữa các thành phần; giới hạn độ dài để kiểm soát token
KNOWLEDGE_FILE = os.environ.get("KNOWLEDGE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge", "system.md"))
KNOWLEDGE_MAX_CHARS = _int("AI_KNOWLEDGE_MAX_CHARS", 4500)
# Đường B của câu nói tự nhiên: người vận hành nhắn câu lạ trên Telegram -> AI hiểu và dùng công cụ (ít bước hơn phân tích sự cố)
CHAT_MAX_STEPS = _int("AI_CHAT_MAX_STEPS", 6)
# Chat có ngân sách RIÊNG (không ăn vào AI_MAX_PER_HOUR của phân tích sự cố) vì trò chuyện cần nhiều lượt hơn
CHAT_MAX_PER_HOUR = _int("AI_CHAT_MAX_PER_HOUR", 30)
CHAT_HISTORY_TURNS = _int("AI_CHAT_HISTORY_TURNS", 8)      # số lượt hỏi-đáp gần nhất AI được nhớ trong 1 phiên /ai
CHAT_SESSION_TTL = _int("AI_CHAT_SESSION_TTL_SECONDS", 3600)
# Chat trực tiếp (lớp 2): gọi thẳng mô hình đã cấu hình (biến MODEL, cùng mô hình Holmes dùng; KHÔNG có mô hình phụ).
CHAT_DIRECT = os.environ.get("AI_CHAT_DIRECT", "true").strip().lower() not in ("false", "0", "no", "off")
CHAT_DIRECT_MAX_TOKENS = _int("AI_CHAT_DIRECT_MAX_TOKENS", 700)
CHAT_STREAM_EDIT_INTERVAL = float(os.environ.get("AI_CHAT_STREAM_EDIT_SECONDS", "1.0") or 1.0)   # Telegram giới hạn ~1 lần sửa tin/giây
CHAT_LLM_MODULE = os.environ.get("AI_CHAT_LLM_MODULE", "")      # chỉ để thử/sandbox: module có hàm complete(messages) -> các đoạn chữ
OPS_MARKER = "[[OPS]]"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("holmes-bridge")

state = {}            # fingerprint -> {"starts", "msg_id", "resolved", "ts"}
state_lock = threading.Lock()
jobs = queue.Queue()
chat_runs = deque()    # thời điểm các lượt chat gần đây (giới hạn mỗi giờ, tách khỏi recent_runs)
chat_jobs = queue.Queue()      # hàng đợi + luồng riêng cho chat: không phải chờ phân tích alert (và ngược lại)
chat_sessions = {}     # sid -> {"hist": deque([{"u","a"}]), "n": số tin, "ts": lần cuối}
chat_lock = threading.Lock()
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


def tg_edit(message_id, text):
    """Sửa nội dung 1 tin đã gửi (để hiện chữ dần). Lỗi/không đổi thì bỏ qua."""
    if not (TG_TOKEN and TG_CHAT and message_id):
        return False
    body = {"chat_id": TG_CHAT, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    req = urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/editMessageText", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if "not modified" not in detail:
            log.debug("Telegram không sửa được tin: %s", detail)
        return False
    except Exception:
        return False


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


def load_knowledge():
    """Đọc bản đồ hệ thống. Thiếu file thì thôi (AI vẫn chạy, chỉ kém hiểu hệ thống)."""
    try:
        with open(KNOWLEDGE_FILE, encoding="utf-8") as f:
            return f.read(KNOWLEDGE_MAX_CHARS).strip()
    except OSError:
        return ""


def knowledge_text():
    k = load_knowledge()
    return (f"\nBản đồ hệ thống (đáng tin; dùng để biết nhìn đâu, vẫn phải dựa vào dữ liệu thật để kết luận):\n{k}\n") if k else ""


ACTION_HINT = ("\nHÀNH ĐỘNG ĐỀ XUẤT: <đúng 1 dòng dạng `action_id container`, ví dụ `restart_container ecommerce-backend`; "
               "action_id chỉ được là restart_container, start_container hoặc logs_tail; "
               "chỉ đề xuất khi runbook cho phép và bằng chứng rõ ràng, nếu không thì ghi `không có`. "
               "Đây chỉ là đề xuất, con người sẽ duyệt>")


TOOLS_HINT = ("\nCÔNG CỤ: bạn có 4 công cụ làm việc với hệ thống: `list_allowed_actions` (xem kho lệnh + container được phép), "
              "`system_overview` (báo cáo tổng quan: mọi container, CPU/RAM máy, web, cảnh báo đang bật), "
              "`read_status` (đọc trạng thái container / log / CPU-RAM; chạy NGAY, chỉ đọc), "
              "`propose_fix` (ĐỀ XUẤT lệnh sửa lỗi; KHÔNG chạy ngay: con người duyệt trên Telegram). "
              "Quy tắc: điều tra bằng Prometheus, Loki và `read_status` trước; chỉ gọi `propose_fix` tối đa 1 lần, khi runbook cho phép "
              "và bằng chứng rõ ràng; nếu `propose_fix` trả `denied` thì đọc `alternatives` rồi chọn lại, hoặc kết luận cần người xử lý tay. "
              "KHÔNG tự viết lệnh docker, KHÔNG ghi dòng `HÀNH ĐỘNG ĐỀ XUẤT`. "
              "Sau khi `propose_fix` trả `queued` hãy kết thúc phiên ngay bằng phần trả lời đúng khuôn.")


def action_hint():
    """Phần cuối prompt nói cho AI cách đề xuất lệnh: công cụ (cách 2), dòng chữ (cách cũ) hoặc không (tắt remediator)."""
    if USE_TOOLS:
        return TOOLS_HINT
    return ACTION_HINT if REMEDIATOR_URL else ""


def summarize_analysis(text):
    """Tóm tắt phân tích để đưa vào prompt vòng sau (phiên AI không nhớ gì giữa các lần chạy)."""
    parts = parse_sections((text or "")[:3000])
    keep = [f"{k}: {parts[k]}" for k in ("VẤN ĐỀ", "NGUYÊN NHÂN", "BẰNG CHỨNG") if parts.get(k)]
    return re.sub(r"[ \t]+", " ", "\n".join(keep))[:ANALYSIS_SUMMARY_CHARS]


def build_followup_prompt(a, payload, rnd, prev_analysis=""):
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
    prev_text = (f"\nPhân tích của chính bạn ở vòng trước (bạn không nhớ phiên cũ; đây là bản tóm tắt đã lưu, "
                 f"chỉ kiểm tra lại phần cần thiết thay vì điều tra lại từ đầu):\n{prev_analysis}\n") if prev_analysis else ""
    return f"""Đây là VÒNG {rnd}/{MAX_ROUNDS} xử lý cảnh báo "{labels.get('alertname', '?')}" (mức {labels.get('severity', '?')}) \
của hệ thống web "Nova Store" (Docker Compose; nginx -> backend Node.js -> MySQL). Cảnh báo VẪN chưa kết thúc.

Nhãn cảnh báo: {label_text}

Các lệnh đã được người vận hành duyệt và chạy ở các vòng trước:
{chr(10).join(lines) or '- (chưa có lệnh nào được chạy)'}
Tình trạng sau lệnh gần nhất: {esc(str(payload.get('status', '')))[:200]}. Thành phần Prometheus đang chưa trả lời (up==0): {down}.
{prev_text}
{knowledge_text()}
Runbook (đừng lặp lại cách đã thử mà không hiệu quả):
{runbook or '(không có runbook)'}

Hãy điều tra lại bằng Prometheus và Loki (chỉ đọc) rồi cho biết bước tiếp theo. Chỉ đề xuất lệnh KHÁC với các lệnh đã chạy; nếu không còn gì an toàn để thử thì ghi `không có` và nói rõ cần người xử lý tay.

Trả lời bằng tiếng Việt, ĐÚNG khuôn, không thêm tiêu đề markdown, tổng không quá 1000 ký tự:
VẤN ĐỀ: <tình trạng hiện tại sau các lệnh trước>
NGUYÊN NHÂN: <nguyên nhân gốc theo bằng chứng mới nhất; ghi rõ nếu là phỏng đoán>
BẰNG CHỨNG:
- <số liệu hoặc dòng log mới, tối đa 3 ý>
HƯỚNG XỬ LÝ:
1. <bước cụ thể, tối đa 3 bước>{action_hint()}"""


def build_prompt(a):
    labels = a.get("labels", {})
    ann = a.get("annotations", {})
    label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    runbook = load_runbook(labels.get("alertname", ""))
    runbook_text = (f"\nRunbook của cảnh báo này (kinh nghiệm của đội vận hành; làm theo thứ tự điều tra, "
                    f"vẫn phải dựa vào dữ liệu thật để kết luận, không chép nguyên văn):\n{runbook}\n") if runbook else ""
    action_text = action_hint()
    return f"""Alertmanager vừa báo một cảnh báo chuyển sang FIRING trong hệ thống web bán hàng "Nova Store" chạy bằng Docker Compose.

Cảnh báo:
- Tên: {labels.get('alertname', '?')}
- Mức độ: {labels.get('severity', '?')}
- Bắt đầu: {vn_time(a.get('startsAt'))} (giờ Việt Nam)
- Tóm tắt: {ann.get('summary', '')}
- Mô tả: {ann.get('description', '')}
- Nhãn: {label_text}

Bối cảnh: người dùng -> nginx -> backend Node.js -> MySQL (service "db"). Số liệu nằm ở Prometheus (job: backend, nginx, mysql, node, prometheus, alertmanager). Log nằm ở Loki với nhãn service (backend, nginx, db, ...), level (error/warn/info) và container.
{knowledge_text()}
{runbook_text}
Hãy điều tra bằng Prometheus và Loki để tìm nguyên nhân gốc. Chỉ đọc dữ liệu, không thay đổi hệ thống.

Trả lời bằng tiếng Việt, ĐÚNG khuôn dưới đây, không thêm tiêu đề markdown, tổng không quá 1200 ký tự:
VẤN ĐỀ: <1-2 câu: chuyện gì đang xảy ra, ảnh hưởng tới người dùng ra sao>
NGUYÊN NHÂN: <1-3 câu: nguyên nhân gốc; nếu chưa chắc chắn thì ghi rõ là phỏng đoán>
BẰNG CHỨNG:
- <số liệu hoặc dòng log cụ thể đã tìm được, tối đa 3 ý>
HƯỚNG XỬ LÝ:
1. <bước cụ thể, ưu tiên lệnh docker compose, tối đa 4 bước>{action_text}"""


def build_chat_prompt(text, history=None, context=""):
    """Prompt cho yêu cầu do người vận hành gõ trên Telegram (chỉ nhận từ đúng chat/người duyệt).
    Trong phiên /ai: kèm `history` (các lượt gần đây, vì mỗi lần chạy AI không nhớ gì) và `context` (tình trạng hiện tại)."""
    hist_lines = []
    for h in (history or [])[-CHAT_HISTORY_TURNS:]:
        hist_lines.append(f"- Người vận hành: {re.sub(chr(10), ' ', str(h.get('u', '')))[:300]}\n  Bạn đã trả lời: {re.sub(chr(10), ' ', str(h.get('a', '')))[:500]}")
    hist_text = ("\nHội thoại gần đây trong phiên này (bạn không nhớ gì ngoài những dòng này; dùng để hiểu \"cái đó\", \"làm lại\", \"ok\"):\n"
                 + "\n".join(hist_lines) + "\n") if hist_lines else ""
    ctx_text = f"\nTình trạng hiện tại:\n{context}\n" if context else ""
    return f"""Người vận hành hệ thống web "Nova Store" (Docker Compose; nginx -> backend Node.js -> MySQL "db") vừa nhắn trên Telegram:
«{text[:500]}»
{hist_text}{ctx_text}{knowledge_text()}
Hãy hiểu ý và làm bằng công cụ:
- Muốn chạy/bật/khởi động lại một container -> gọi `propose_fix` (KHÔNG chạy ngay: người vận hành sẽ bấm Duyệt trên Telegram). Chọn action_id và tên container đầy đủ đúng ý họ ("container của backend" = ecommerce-backend). Muốn "chạy" mà chưa rõ container đang dừng hay đang chạy: đọc `read_status` (inspect_container) trước: đang dừng -> `start_container`, đang chạy -> `restart_container`. Nếu thật sự mơ hồ thì hỏi lại ngắn gọn, đừng đoán.
- Muốn xem trạng thái/log/CPU-RAM -> dùng `read_status` hoặc `system_overview`, rồi tóm tắt kết quả.
- Là câu hỏi (vì sao chậm, có lỗi không...) -> điều tra bằng Prometheus, Loki và công cụ chỉ đọc rồi trả lời.
- Người vận hành nói "ok", "làm đi", "duyệt đi" về một đề xuất đang chờ -> bạn KHÔNG duyệt thay được: nhắc họ bấm nút ✅ trên tin đề xuất.
- Nếu yêu cầu nằm ngoài kho lệnh (dừng, xoá, sửa dữ liệu...) -> nói rõ không làm được, đừng thử lách.
Trả lời bằng tiếng Việt, tối đa 800 ký tự, không tiêu đề markdown.{TOOLS_HINT}"""


def chat_rate_ok():
    now = time.time()
    while chat_runs and now - chat_runs[0] > 3600:
        chat_runs.popleft()
    if len(chat_runs) >= CHAT_MAX_PER_HOUR:
        return False
    chat_runs.append(now)
    return True


def chat_context():
    """Tình trạng hiện tại cho AI chat: cảnh báo đang bật, đề xuất đang chờ duyệt, lệnh vừa chạy. Lỗi thì bỏ qua phần đó."""
    parts = []
    try:
        firing = sorted({s_["metric"].get("alertname", "?") for s_ in prom_query('ALERTS{alertstate="firing"}')})
        parts.append("- Cảnh báo đang bật: " + (", ".join(firing) if firing else "không có"))
    except Exception:
        pass
    if REMEDIATOR_URL and TOOL_TOKEN:
        try:
            req = urllib.request.Request(f"{REMEDIATOR_URL}/tool/state", headers={"Authorization": f"Bearer {TOOL_TOKEN}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                st = json.load(r)
            pend = [f"{x['action_id']} {x['container']} (chờ {x.get('age_s', 0)}s, cảnh báo {x.get('alert')})" for x in st.get("pending", [])]
            rec = [f"{x['ts']} {x['action_id']} {x['container']} -> {'thành công' if x.get('ok') else 'LỖI'}" for x in st.get("recent", [])[-5:]]
            parts.append("- Đề xuất đang chờ người duyệt: " + ("; ".join(pend) if pend else "không có"))
            parts.append("- Lệnh thay đổi vừa chạy: " + ("; ".join(rec) if rec else "chưa có"))
        except Exception:
            pass
    return "\n".join(parts)


def run_holmes(a, prompt=None, fp="", rnd=1, max_steps=None):
    """Trả về (kết_quả, lỗi). Chạy holmes ask, lấy câu trả lời từ file JSON.
    Danh tính sự cố đặt vào MÔI TRƯỜNG của tiến trình holmes để công cụ đọc (AI không tự đặt được)."""
    env = dict(os.environ, AI_INCIDENT_FP=str(fp)[:80], AI_INCIDENT_ROUND=str(rnd),
               AI_INCIDENT_ALERT=str(a.get("labels", {}).get("alertname", "?"))[:80])
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "result.json")
        cmd = shlex.split(HOLMES_CMD) + ["ask", prompt or build_prompt(a), "--no-interactive", "--no-echo",
               "--max-steps", str(max_steps or MAX_STEPS), "--bash-always-deny", "--json-output-file", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
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


def internal_headers():
    """Header cho lần gọi nội bộ bridge -> remediator (cùng token REMEDIATOR_TOOL_TOKEN)."""
    h = {"Content-Type": "application/json"}
    if TOOL_TOKEN:
        h["Authorization"] = f"Bearer {TOOL_TOKEN}"
    return h


def send_proposal(a, proposal, analysis_text, rnd=1):
    """Chuyển đề xuất sang remediator. Lỗi ở đây không được làm hỏng tin phân tích."""
    if not REMEDIATOR_URL:
        return
    reason = parse_sections(analysis_text[:3000]).get("NGUYÊN NHÂN", "")[:200]
    body = {"alert": a.get("labels", {}).get("alertname", "?"), "fingerprint": a.get("fingerprint", ""),
            "reason": reason, "round": rnd, **proposal}
    req = urllib.request.Request(f"{REMEDIATOR_URL}/propose", data=json.dumps(body).encode(), headers=internal_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log.info("Đã gửi đề xuất %s %s (vòng %d) sang remediator (HTTP %s)", proposal["action_id"], proposal["container"], rnd, r.status)
    except urllib.error.HTTPError as e:
        log.info("Remediator từ chối đề xuất %s %s (HTTP %s)", proposal["action_id"], proposal["container"], e.code)
    except Exception as e:
        log.error("Không gửi được đề xuất sang remediator: %s", type(e).__name__)


def tool_session_queued(fp, rnd):
    """Số đề xuất AI đã gửi trong phiên này qua công cụ (hỏi remediator). None nếu không hỏi được."""
    if not (REMEDIATOR_URL and TOOL_TOKEN):
        return None
    url = f"{REMEDIATOR_URL}/tool/session?" + urllib.parse.urlencode({"fingerprint": str(fp)[:80], "round": rnd})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOOL_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return int(json.load(r).get("queued", 0))
    except Exception as e:
        log.warning("Không hỏi được remediator về phiên công cụ: %s", type(e).__name__)
        return None


def close_remediator_loop(fp):
    """Cảnh báo đã hết: báo remediator huỷ đề xuất đang chờ và kết thúc vòng. Lỗi ở đây không quan trọng."""
    if not REMEDIATOR_URL:
        return
    req = urllib.request.Request(f"{REMEDIATOR_URL}/close", data=json.dumps({"fingerprint": fp, "reason": "cảnh báo đã hết"}).encode(),
                                 headers=internal_headers())
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


def handle_chat(payload):
    """remediator chuyển câu nói của người vận hành (đã qua kiểm tra chat/người). Có `sid` = đang trong phiên /ai (có trí nhớ).
    Trả (mã_http, thông_báo)."""
    if not USE_TOOLS:
        return 503, "tool mode off"
    text = re.sub(r"\s+", " ", str(payload.get("text", ""))).strip()[:500]
    if len(text) < 1:
        return 400, "empty"
    sid = re.sub(r"[^a-f0-9]", "", str(payload.get("sid") or ""))[:12]
    with chat_lock:
        if not chat_rate_ok():
            log.warning("Bỏ qua tin chat: đã đủ %d lượt chat trong 1 giờ (AI_CHAT_MAX_PER_HOUR)", CHAT_MAX_PER_HOUR)
            return 429, "rate limited"
        now = time.time()
        for k in [k for k, v in chat_sessions.items() if now - v["ts"] > CHAT_SESSION_TTL]:
            chat_sessions.pop(k)
        if sid:
            sess = chat_sessions.setdefault(sid, {"hist": deque(maxlen=CHAT_HISTORY_TURNS), "n": 0, "ts": now})
            sess["n"] += 1
            sess["ts"] = now
            fp = f"chat-{sid}-{sess['n']}"        # mỗi tin 1 danh tính riêng: giới hạn "1 đề xuất mỗi phiên AI" tính theo từng tin
        else:
            fp = "chat-" + os.urandom(4).hex()
    chat_jobs.put((fp, {"text": text, "uid": str(payload.get("uid", ""))[:20], "sid": sid}))
    log.info("Nhận tin chat (%d ký tự%s) -> xếp hàng", len(text), ", trong phiên /ai" if sid else "")
    return 200, "queued"


def end_chat_session(payload):
    sid = re.sub(r"[^a-f0-9]", "", str(payload.get("sid") or ""))[:12]
    with chat_lock:
        chat_sessions.pop(sid, None)
    return 200, "ok"


# ---------------------------------------------------------------- chat trực tiếp (lớp 2) + bộ định tuyến
CHAT_CAPABILITIES = (
    "Bot này làm được:\n"
    "1) Lệnh gõ: /status (tổng quan hệ thống), /status <container>, /logs <container>, /restart <container>, /start <container>, "
    "/stop (dừng vòng xử lý tự động), /help. Nói tự nhiên như \"khởi động lại backend\", \"các container thế nào?\" cũng được.\n"
    "2) Khi bạn hỏi về trạng thái thật, log, sự cố hoặc muốn chạy/bật/khởi động lại một container, bot nhờ AI (Holmes) điều tra bằng công cụ "
    "CHỈ ĐỌC (trạng thái, log, CPU/RAM, tổng quan, Prometheus, Loki) rồi ĐỀ XUẤT lệnh kèm nút Duyệt. Lệnh chỉ chạy sau khi bạn bấm ✅. "
    "Kho lệnh chỉ có restart_container và start_container (ecommerce-db chỉ được bật lại, không được restart). "
    "Không có lệnh dừng, xoá, sửa dữ liệu.\n"
    "3) /ai mở phiên trò chuyện (AI nhớ ngữ cảnh), /ai off hoặc /exit đóng, /ai reset quên hội thoại; tự đóng sau 15 phút im lặng.\n"
    "4) Khi Prometheus báo cảnh báo mức critical/warning, AI tự phân tích và có thể đề xuất lệnh sửa lỗi.")

CHAT_SYSTEM = (
    'Bạn là trợ lý vận hành của hệ thống web "Nova Store" (Docker Compose; nginx -> backend Node.js -> MySQL), đang trò chuyện với người vận hành '
    "trên Telegram. Trả lời bằng tiếng Việt, ngắn gọn (tối đa 600 ký tự), thân thiện, không tiêu đề markdown.\n"
    "QUY TẮC BẮT BUỘC:\n"
    "- Bạn KHÔNG có số liệu trực tiếp của hệ thống lúc này và KHÔNG chạy được lệnh nào. Nếu câu hỏi cần trạng thái/log/số liệu THẬT, cần điều tra "
    "một sự cố, hoặc người dùng muốn bật/khởi động lại/dừng một container: trả lời ĐÚNG MỘT dòng `" + OPS_MARKER + "` (không thêm chữ nào) "
    "để hệ thống chuyển cho AI có công cụ.\n"
    "- Không bao giờ bịa số liệu, trạng thái hay kết quả lệnh. Không nói bạn đã chạy hay đã duyệt lệnh: lệnh thay đổi hệ thống luôn cần người bấm nút Duyệt.\n"
    "- Câu hỏi về cách bot hoạt động: trả lời theo mô tả bên dưới. Kiến thức chung (Docker, Linux, MySQL, Prometheus, Telegram...): trả lời bình thường, "
    "nếu không chắc thì nói không chắc.\n\n")

# Tên CHẮC CHẮN là thành phần của hệ thống này: nhắc tới là cần Holmes. Tên "chung" (nginx, prometheus, mysql...) cũng là công cụ nổi tiếng nên
# "giải thích prometheus" là kiến thức chung (chat nhanh); chỉ coi là hỏi về hệ thống khi đi kèm từ chỉ trạng thái/hành động.
OPS_STRONG_RE = re.compile(
    r"(ecommerce-[a-z0-9-]+|\b(back ?end|front ?end|db|alertmanager|node ?exporter|nginx ?exporter|mysqld? ?exporter)\b|"
    r"mysqldown|targetdown|nginxdown|high5xxrate|highlatencyp95|hosthigh|hostdisk|mysqltoomany)")
OPS_WEAK_RE = re.compile(r"\b(nginx|prometheus|loki|grafana|alloy|mysql|database)\b")
OPS_CUE_RE = re.compile(r"(\bsao\b|the nao|\broi\b|\bloi\b|\bcham\b|\btreo\b|\bchet\b|\bdown\b|\bdang\b|khoi dong|restart|\bbat\b|chay lai|\blog\b|trang thai|co on|on khong|van de)")
FOLLOWUP_RE = re.compile(r"^(ok|oke|okay|duoc|lam di|lam luon|duyet|dong y|dung roi|vay|the con|con .*(sao|the nao)|cai do|xong chua|roi sao|lam lai|thu lai|tiep)\b")
OPS_STATE_RE = re.compile(
    r"(trang thai|tinh trang|dang chay|con song|canh bao (dang|nao)|log (gan|moi|cuoi)|xem log|cpu|\bram\b|o dia|loi 5xx|"
    r"bi (treo|loi|down|chet)|tong quan)")
CHITCHAT_RE = re.compile(r"^(cam on|thanks|thank you|tuyet|hay qua|ok(e|ay)? cam on|tot qua)\b")


def plain(text):
    t = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9?\- ]+", " ", "".join(c for c in t if unicodedata.category(c) != "Mn"))).strip()


def classify_route(text, last_route=""):
    """"ops" (cần Holmes + công cụ: trạng thái thật, sự cố, lệnh) hay "chat" (trả lời trực tiếp, nhanh).
    Dấu hiệu rõ ràng quyết định ngay (miễn phí); phần còn lại để mô hình tự báo [[OPS]] nếu thấy cần dữ liệu thật."""
    if not CHAT_DIRECT or not USE_TOOLS:
        return "ops"
    t = plain(text)
    if CHITCHAT_RE.search(t):
        return "chat"
    if OPS_STRONG_RE.search(t) or OPS_STATE_RE.search(t) or (OPS_WEAK_RE.search(t) and OPS_CUE_RE.search(t)):
        return "ops"
    if last_route == "ops" and len(t) <= 40 and FOLLOWUP_RE.search(t):   # câu nối tiếp ngắn của việc đang làm với hệ thống ("ok làm đi", "cái đó thì sao")
        return "ops"
    return "chat"


def _clean_aws_env():
    """Giống holmes-entrypoint.sh: bỏ biến AWS trống và ký tự thừa (dấu cách, ngoặc kép, \\r) để litellm/boto3 không chọn nhầm cách đăng nhập."""
    for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE"):
        val = re.sub(r"[\s\"']", "", os.environ.get(v, ""))
        if val:
            os.environ[v] = val
        else:
            os.environ.pop(v, None)


def warm_llm():
    """Nạp trước thư viện gọi mô hình (import litellm mất vài giây) để tin chat đầu tiên không bị chậm."""
    if CHAT_LLM_MODULE or not has_bedrock_creds():
        return
    try:
        _clean_aws_env()
        import litellm  # noqa: F401
        log.info("Đã nạp sẵn thư viện gọi mô hình cho chat trực tiếp")
    except Exception as e:
        log.warning("Không nạp được litellm cho chat trực tiếp (%s): chat sẽ dùng Holmes", type(e).__name__)


def llm_stream(messages):
    """Sinh từng đoạn chữ từ mô hình đã cấu hình (biến MODEL = cùng mô hình Holmes dùng). KHÔNG truyền công cụ nào."""
    if CHAT_LLM_MODULE:
        yield from importlib.import_module(CHAT_LLM_MODULE).complete(messages)
        return
    _clean_aws_env()
    import litellm
    resp = litellm.completion(model=os.environ.get("MODEL", ""), messages=messages, stream=True, max_tokens=CHAT_DIRECT_MAX_TOKENS,
                              temperature=0.3, timeout=60, drop_params=True)
    for chunk in resp:
        try:
            delta = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            delta = None
        if delta:
            yield delta


def build_direct_messages(text, history):
    msgs = [{"role": "system", "content": CHAT_SYSTEM + CHAT_CAPABILITIES + (("\n\nBản đồ hệ thống (chỉ để giải thích cấu trúc, KHÔNG phải số liệu hiện tại):\n" + load_knowledge()) if load_knowledge() else "")}]
    for h in (history or [])[-CHAT_HISTORY_TURNS:]:
        msgs.append({"role": "user", "content": str(h.get("u", ""))[:400]})
        msgs.append({"role": "assistant", "content": str(h.get("a", ""))[:600]})
    msgs.append({"role": "user", "content": text[:500]})
    return msgs


CHAT_HEADER = "🤖 <b>AI trả lời</b>\n"


def stream_direct_reply(messages):
    """Gọi mô hình trực tiếp và hiện chữ dần trên Telegram (gửi 1 tin rồi sửa tối đa ~1 lần/giây).
    Trả ("chat", văn_bản) hoặc ("ops", "") khi mô hình báo cần dữ liệu thật/lệnh (dấu hiệu [[OPS]] ở đầu câu trả lời)."""
    buf, mid, last_edit, sent_len = "", None, 0.0, 0
    gen = llm_stream(messages)
    try:
        for delta in gen:
            buf += delta
            head = buf.lstrip()
            if head.startswith(OPS_MARKER):
                return "ops", ""
            if len(head) < len(OPS_MARKER) and OPS_MARKER.startswith(head):
                continue                                   # có thể là phần đầu của dấu hiệu: chờ thêm, chưa gửi gì
            now = time.time()
            if mid is None:
                mid = tg_send(CHAT_HEADER + md_to_html(buf[:3500]))
                last_edit, sent_len = now, len(buf)
            elif now - last_edit >= CHAT_STREAM_EDIT_INTERVAL and len(buf) > sent_len:
                tg_edit(mid, CHAT_HEADER + md_to_html(buf[:3500]))
                last_edit, sent_len = now, len(buf)
    finally:
        close = getattr(gen, "close", None)
        if close:
            close()
    text = buf.strip()
    if not text:
        return "ops", ""                                   # mô hình không trả gì: để Holmes xử lý thay vì im lặng
    if text.startswith(OPS_MARKER):
        return "ops", ""
    final = CHAT_HEADER + md_to_html(text[:3500])
    if mid is None:
        tg_send(final)
    elif len(buf) > sent_len or buf.strip() != buf:
        tg_edit(mid, final)
    return "chat", text


def remember_chat(sid, text, answer, route):
    if not sid:
        return
    with chat_lock:
        sess = chat_sessions.get(sid)
        if sess is not None:
            sess["hist"].append({"u": text, "a": answer[:600]})
            sess["route"] = route


def process_chat(fp, payload):
    """Chạy 1 lượt chat (luồng riêng). Định tuyến: nói chuyện/kiến thức -> gọi thẳng mô hình (nhanh, chữ hiện dần);
    cần số liệu thật hoặc lệnh -> Holmes + công cụ. Trong phiên /ai: dùng trí nhớ dùng chung cho cả hai đường."""
    sid = payload.get("sid") or ""
    text = payload["text"]
    if not has_bedrock_creds():
        tg_send("⚠️ AI chưa cấu hình Bedrock nên chưa trả lời được.")
        return
    with chat_lock:
        sess = chat_sessions.get(sid) if sid else None
        history = list(sess["hist"]) if sess else []
        last_route = (sess or {}).get("route", "")
    t0 = time.time()
    route = classify_route(text, last_route)
    if route == "chat":
        try:
            outcome, reply = stream_direct_reply(build_direct_messages(text, history))
        except Exception as e:                            # lỗi gọi trực tiếp thì vẫn còn đường Holmes
            log.warning("Chat trực tiếp lỗi (%s) -> chuyển sang Holmes", type(e).__name__)
            outcome, reply = "ops", ""
        if outcome == "chat":
            log.info("Chat trực tiếp xong trong %.1fs", time.time() - t0)
            remember_chat(sid, text, reply, "chat")
            return
        log.info("Chat -> chuyển sang Holmes (cần dữ liệu thật hoặc lệnh) sau %.1fs", time.time() - t0)
    fake = {"labels": {"alertname": "Chat", "severity": "info"}, "annotations": {}}
    context = chat_context() if sid else ""
    result, err = run_holmes(fake, prompt=build_chat_prompt(text, history, context), fp=fp, rnd=1, max_steps=CHAT_MAX_STEPS)
    if not result:
        log.error("Chat thất bại: %s", err)
        tg_send(f"⚠️ AI chưa xử lý được yêu cầu: {esc(err)[:300]}")
        return
    result, _ = extract_action(result)
    log.info("Chat (Holmes) xong trong %.0fs", time.time() - t0)
    queued = tool_session_queued(fp, 1) if sid else None
    answer = result[:1500] + ("\n\n<i>(đã gửi đề xuất kèm nút Duyệt)</i>" if queued else "")
    tg_send("🤖 <b>AI trả lời</b>\n" + md_to_html(result[:1500]) + ("\n\n<i>(đã gửi đề xuất kèm nút Duyệt)</i>" if queued else ""))
    remember_chat(sid, text, answer[:600] + (" [đã đề xuất lệnh chờ duyệt]" if queued else ""), "ops")


def chat_worker():
    while True:
        fp, payload = chat_jobs.get()
        try:
            process_chat(fp, payload)
        except Exception:
            log.exception("Lỗi khi xử lý tin chat")
        finally:
            chat_jobs.task_done()


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
                result, err = run_holmes(a, fp=fp, rnd=1)
                secs = time.time() - t0
                if result:
                    log.info("Phân tích xong %s trong %.0fs (%s)", name, secs, "công cụ" if USE_TOOLS else "dòng chữ")
                    result, proposal = extract_action(result)
                    st["analysis"] = summarize_analysis(result)
                    st["msg_id"] = tg_send(format_analysis(a, result, secs))
                    if proposal and not USE_TOOLS:   # cách 2: đề xuất đã đi thẳng qua công cụ trong phiên AI
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
                result, err = run_holmes(alert, build_followup_prompt(alert, a, rnd, st.get("analysis", "")), fp=fp, rnd=rnd)
                secs = time.time() - t0
                if result:
                    log.info("Phân tích vòng %d xong %s trong %.0fs", rnd, name, secs)
                    result, proposal = extract_action(result)
                    st["round"] = rnd
                    st["analysis"] = summarize_analysis(result) or st.get("analysis", "")
                    tg_send(format_analysis(alert, result, secs, title=f"AI TIẾP TỤC (vòng {rnd}/{MAX_ROUNDS})"), reply_to=st.get("msg_id"))
                    if USE_TOOLS:
                        queued = tool_session_queued(fp, rnd)
                        has_proposal = bool(queued) or queued is None   # không hỏi được thì đừng báo "hết lệnh" nhầm
                    else:
                        has_proposal = bool(proposal)
                        if proposal:
                            send_proposal(alert, proposal, result, rnd)
                    if not has_proposal:
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
    timeout = 20      # giây: kết nối treo (gửi chậm từng byte) bị cắt, không giữ luồng mãi
    def _reply(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        self._reply(200, "ok") if self.path == "/health" else self._reply(404, "not found")

    def do_POST(self):
        if self.path not in ("/alert", "/continue", "/chat", "/chat/end"):
            return self._reply(404, "not found")
        # /chat luôn cần token (chỉ remediator gọi). Khi ĐÃ đặt token thì /alert (Alertmanager gửi kèm token) và /continue
        # (remediator) cũng phải có: container khác trong mạng không giả được cảnh báo (tốn tiền Bedrock, gài prompt qua nhãn).
        if self.path in ("/chat", "/chat/end") or TOOL_TOKEN:
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not TOOL_TOKEN or not hmac.compare_digest(given.encode(), TOOL_TOKEN.encode()):
                try:
                    self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000))   # đọc bỏ thân yêu cầu rồi mới trả 401
                except (ValueError, OSError):
                    pass
                return self._reply(401, "unauthorized")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._reply(413, "too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path in ("/continue", "/chat", "/chat/end"):
                if not isinstance(payload, dict):
                    return self._reply(400, "bad json")
                code, msg = {"/continue": handle_continue, "/chat": handle_chat, "/chat/end": end_chat_session}[self.path](payload)
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
    log.info("holmes-bridge chạy ở :%d · Telegram: %s · Bedrock: %s · mức: %s · tối đa %d lần/giờ · tối đa %d vòng/sự cố · chat trực tiếp: %s · đề xuất lệnh: %s",
             PORT, "BẬT" if TG_TOKEN and TG_CHAT else "TẮT (thiếu token/chat id)",
             "có khoá" if has_bedrock_creds() else "CHƯA CÓ KHOÁ",
             ",".join(sorted(SEVERITIES)), MAX_PER_HOUR, MAX_ROUNDS, "BẬT" if (CHAT_DIRECT and USE_TOOLS) else "TẮT",
             "CÔNG CỤ (cách 2)" if USE_TOOLS else ("dòng chữ (cách cũ)" if REMEDIATOR_URL else "TẮT"))
    if not TOOL_TOKEN:
        log.warning("CHƯA đặt REMEDIATOR_TOOL_TOKEN: /alert, /continue KHÔNG xác thực (container khác trong mạng gọi được). Nên đặt token.")
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=chat_worker, daemon=True).start()
    if USE_TOOLS and CHAT_DIRECT:
        threading.Thread(target=warm_llm, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
