#!/usr/bin/env python3
"""
Cầu nối AI: Alertmanager (webhook) -> HolmesGPT (Bedrock) -> Telegram.

Luồng:
  1. Cảnh báo FIRING   -> Alertmanager gọi POST /alert (song song với tin Telegram gốc)
  2. Cầu nối chạy `holmes ask` (đọc Prometheus + Loki, nhờ Bedrock suy luận)
  3. PHÂN TÍCH XONG mới gửi 1 tin Telegram: Vấn đề · Nguyên nhân · Bằng chứng · Hướng xử lý
  4. Cảnh báo RESOLVED -> hỏi lại Prometheus (không tốn tiền Bedrock), gửi tin
     "đã hoạt động bình thường", trả lời (reply) ngay dưới tin phân tích ở bước 3.

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
def build_prompt(a):
    labels = a.get("labels", {})
    ann = a.get("annotations", {})
    label_text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"""Alertmanager vừa báo một cảnh báo chuyển sang FIRING trong hệ thống web bán hàng "Nova Store" chạy bằng Docker Compose.

Cảnh báo:
- Tên: {labels.get('alertname', '?')}
- Mức độ: {labels.get('severity', '?')}
- Bắt đầu: {vn_time(a.get('startsAt'))} (giờ Việt Nam)
- Tóm tắt: {ann.get('summary', '')}
- Mô tả: {ann.get('description', '')}
- Nhãn: {label_text}

Bối cảnh: người dùng -> nginx -> backend Node.js -> MySQL (service "db"). Số liệu nằm ở Prometheus (job: backend, nginx, mysql, node, prometheus, alertmanager). Log nằm ở Loki với nhãn service (backend, nginx, db, ...), level (error/warn/info) và container.

Hãy điều tra bằng Prometheus và Loki để tìm nguyên nhân gốc. Chỉ đọc dữ liệu, không thay đổi hệ thống.

Trả lời bằng tiếng Việt, ĐÚNG khuôn dưới đây, không thêm tiêu đề markdown, tổng không quá 1200 ký tự:
VẤN ĐỀ: <1-2 câu: chuyện gì đang xảy ra, ảnh hưởng tới người dùng ra sao>
NGUYÊN NHÂN: <1-3 câu: nguyên nhân gốc; nếu chưa chắc chắn thì ghi rõ là phỏng đoán>
BẰNG CHỨNG:
- <số liệu hoặc dòng log cụ thể đã tìm được, tối đa 3 ý>
HƯỚNG XỬ LÝ:
1. <bước cụ thể, ưu tiên lệnh docker compose, tối đa 4 bước>"""


def run_holmes(a):
    """Trả về (kết_quả, lỗi). Chạy holmes ask, lấy câu trả lời từ file JSON."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "result.json")
        cmd = shlex.split(HOLMES_CMD) + ["ask", build_prompt(a), "--no-interactive", "--no-echo",
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


def header(a, title):
    labels = a.get("labels", {})
    return (f"{title}: {esc(labels.get('alertname', '?'))}</b>\n"
            f"Mức độ: {esc(labels.get('severity', '?').upper())} · Bắt đầu: {vn_time(a.get('startsAt'))}\n{LINE}")


def format_analysis(a, result, seconds):
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
    return (f"🔎 <b>{header(a, 'AI PHÂN TÍCH')}\n{body}\n{LINE}\n"
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
            state[fp] = {"starts": starts, "msg_id": None, "resolved": False, "ts": time.time()}
            log.info("Nhận %s FIRING -> xếp hàng phân tích", name)
            jobs.put(("analyze", fp, a))
        elif status == "resolved":
            if not st or st["resolved"] or st["starts"] != starts:
                return  # sự cố này chưa từng được AI phân tích -> không gửi xác nhận
            st["resolved"] = True
            log.info("Nhận %s RESOLVED -> xếp hàng xác nhận", name)
            jobs.put(("resolved", fp, a))


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
                    st["msg_id"] = tg_send(format_analysis(a, result, secs))
                else:
                    log.error("Phân tích %s thất bại: %s", name, err)
                    st["msg_id"] = tg_send(format_failure(a, err))
            elif kind == "resolved":
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
        if self.path != "/alert":
            return self._reply(404, "not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._reply(413, "too large")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            for a in payload.get("alerts", []):
                handle_alert(a)
        except ValueError:
            return self._reply(400, "bad json")
        # Trả lời ngay để Alertmanager không chờ; phân tích chạy ở luồng riêng
        self._reply(200, "ok")

    def log_message(self, fmt, *args):
        pass  # không ghi log mỗi request (healthcheck)


def main():
    log.info("holmes-bridge chạy ở :%d · Telegram: %s · Bedrock: %s · mức: %s · tối đa %d lần/giờ",
             PORT, "BẬT" if TG_TOKEN and TG_CHAT else "TẮT (thiếu token/chat id)",
             "có khoá" if has_bedrock_creds() else "CHƯA CÓ KHOÁ",
             ",".join(sorted(SEVERITIES)), MAX_PER_HOUR)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
