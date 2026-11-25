#!/usr/bin/env python3
"""
"AI" giả cho sandbox: thay cho `holmes ask` (không gọi Bedrock, KHÔNG tốn tiền).
holmes-bridge gọi nó đúng như gọi holmes thật:  mock_holmes.py ask <prompt> ... --json-output-file <file>

Chế độ CÔNG CỤ (cách 2, mặc định trong sandbox): mock gọi CHÍNH script công cụ thật
(/app/tools/remediation_tool.py) như HolmesGPT sẽ gọi, nên toàn bộ đường
bridge -> (env danh tính sự cố) -> công cụ -> remediator /tool/* -> docker-proxy được chạy thật.
Hành động lấy từ nhãn cảnh báo do script test đặt: sbx_action / sbx_container (vòng 1),
sbx_actionN / sbx_containerN (vòng N), sbx_retry=1 (nếu bị chặn, chọn lại gợi ý đầu tiên đi qua propose_fix).
Chế độ cũ (dòng chữ HÀNH ĐỘNG ĐỀ XUẤT) vẫn giữ cho AI_USE_TOOLS=false.
Mock báo lại trong tin phân tích: prompt có runbook / bản đồ hệ thống / tóm tắt vòng trước / lịch sử lệnh không.
"""
import json
import re
import subprocess
import sys

TOOL = "/app/tools/remediation_tool.py"
READ_ACTIONS = ("logs_tail", "inspect_container", "container_stats")

args = sys.argv[1:]
prompt = args[1] if len(args) > 1 and args[0] == "ask" else ""
out = args[args.index("--json-output-file") + 1] if "--json-output-file" in args else None


def label(name, default=""):
    m = re.search(rf"{name}=([^,\s]+)", prompt)
    return m.group(1) if m else default


def tool(*a):
    p = subprocess.run([sys.executable, TOOL, *a], capture_output=True, text=True, timeout=90)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"status": "error", "message": (p.stderr or p.stdout or "không có đầu ra")[:200]}


def write(text):
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"result": text}, f, ensure_ascii=False)


alert = (re.search(r"- Tên: (\S+)", prompt) or [None, "?"])[1]
rnd = re.search(r"VÒNG (\d+)/", prompt)
n = rnd.group(1) if rnd else ""
action = label(f"sbx_action{n}", "none") if rnd else label("sbx_action", "none")
container = label(f"sbx_container{n}", label("sbx_container")) if rnd else label("sbx_container")
has_runbook = "Runbook" in prompt and "(không có runbook)" not in prompt
tools_mode = "propose_fix" in prompt and "CÔNG CỤ:" in prompt
chat_mode = "vừa nhắn trên Telegram" in prompt
flags = (f"runbook: {'CÓ' if has_runbook else 'KHÔNG'} · công cụ: {'CÓ' if tools_mode else 'KHÔNG'} · "
         f"bản đồ hệ thống: {'CÓ' if 'Chuỗi phụ thuộc' in prompt else 'KHÔNG'} · "
         f"tóm tắt vòng trước: {'CÓ' if 'Phân tích của chính bạn' in prompt else 'KHÔNG'} · "
         f"lịch sử lệnh: {'CÓ' if rnd and '- vòng ' in prompt else 'KHÔNG'}")

# ------------------------------------------------------------------ yêu cầu gõ trên Telegram (đường B)
if chat_mode:
    asked = (re.search(r"«(.*?)»", prompt, re.S) or [None, ""])[1]
    target = (re.search(r"ecommerce-sbx-[a-z0-9]+", asked) or [None, ""])[0]
    mem = (f"lịch sử hội thoại: {'CÓ' if 'Hội thoại gần đây' in prompt else 'KHÔNG'} · "
           f"tình trạng hiện tại: {'CÓ' if 'Tình trạng hiện tại' in prompt else 'KHÔNG'}")
    if target:
        r = tool("read", "inspect_container", target)
        running = (r.get("data") or {}).get("running")
        extra = ""
        if re.search(r"(chạy|khởi động|restart|bật|xử lý|treo)", asked, re.I):     # AI tự chọn start hay restart theo trạng thái thật
            action = "restart_container" if running else "start_container"
            p = tool("propose", action, target, "mock: người vận hành muốn chạy container này")
            extra = f" Đã gọi propose_fix {action}: {p.get('status')}."
        write(f"(mock) đã đọc trạng thái {target}: công cụ trả `{r.get('status')}`, running={running}.{extra} ({flags}) ({mem})")
    elif "Hội thoại gần đây" in prompt or "Tình trạng hiện tại" in prompt:
        write(f"(mock) tôi nhớ cuộc trò chuyện trước. ({mem})")
    else:
        write("(mock) tôi chưa thấy tên container trong câu hỏi, bạn nói rõ hơn nhé.")
    sys.exit(0)

lines = [
    f"VẤN ĐỀ: (mock) cảnh báo {alert} trên {container or 'hệ thống'}" + (f", vòng {n}." if rnd else "."),
    "NGUYÊN NHÂN: (mock) giả lập trong sandbox, không phải kết luận thật.",
    "BẰNG CHỨNG:",
    f"- (mock) {flags}",
]

# ------------------------------------------------------------------ chế độ công cụ (cách 2)
if tools_mode and action != "none" and container:
    if action in READ_ACTIONS:
        r = tool("read", action, container)
        lines.append(f"- (mock) công cụ read_status trả: {r.get('status')}")
    else:
        r = tool("propose", action, container, "mock: lý do dựa trên bằng chứng đã đọc")
        lines.append(f"- (mock) công cụ propose_fix trả: {r.get('status')}; gợi ý thay thế: {len(r.get('alternatives') or [])}")
        if r.get("status") == "denied" and r.get("alternatives") and label("sbx_retry") == "1":
            alt = next((a for a in r["alternatives"] if a.get("via") == "propose_fix"), None)
            if alt:
                r2 = tool("propose", alt["action_id"], alt["container"], "mock: chọn lại theo gợi ý")
                lines.append(f"- (mock) chọn lại {alt['action_id']} {alt['container']}: {r2.get('status')}")
    lines += ["HƯỚNG XỬ LÝ:", "1. (mock) đã xử lý bằng công cụ; chờ người duyệt nếu có đề xuất."]
    write("\n".join(lines))
    sys.exit(0)

# ------------------------------------------------------------------ chế độ cũ (dòng chữ) hoặc không có hành động
lines += ["HƯỚNG XỬ LÝ:", "1. (mock) xem log rồi khởi động lại container nếu cần."]
if not tools_mode and "HÀNH ĐỘNG ĐỀ XUẤT" in prompt:
    lines.append("HÀNH ĐỘNG ĐỀ XUẤT: " + (f"{action} {container}" if action != "none" and container else "không có"))
write("\n".join(lines))
