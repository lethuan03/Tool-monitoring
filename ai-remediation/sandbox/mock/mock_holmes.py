#!/usr/bin/env python3
"""
"AI" giả cho sandbox: thay cho `holmes ask` (không gọi Bedrock, KHÔNG tốn tiền).
holmes-bridge gọi nó đúng như gọi holmes thật:  mock_holmes.py ask <prompt> ... --json-output-file <file>

Đọc nhãn sbx_action / sbx_container trong prompt (do script test đặt vào cảnh báo) rồi trả lời
đúng khuôn mà bridge mong đợi, kèm dòng HÀNH ĐỘNG ĐỀ XUẤT. Ghi luôn việc prompt có chứa
runbook hay không, để test kiểm tra được việc chèn runbook.
"""
import json
import re
import sys

args = sys.argv[1:]
prompt = args[1] if len(args) > 1 and args[0] == "ask" else ""
out = args[args.index("--json-output-file") + 1] if "--json-output-file" in args else None

alert = (re.search(r"- Tên: (\S+)", prompt) or [None, "?"])[1]
action = (re.search(r"sbx_action=([^,\s]+)", prompt) or [None, "none"])[1]
container = (re.search(r"sbx_container=([^,\s]+)", prompt) or [None, ""])[1]
has_runbook = "Runbook của cảnh báo này" in prompt
asks_action = "HÀNH ĐỘNG ĐỀ XUẤT" in prompt

# Vòng sau (prompt follow-up của bridge có "VÒNG n/m"): hành động lấy từ nhãn sbx_action<n> / sbx_container<n>
rnd = re.search(r"VÒNG (\d+)/", prompt)
if rnd:
    n = rnd.group(1)
    action = (re.search(rf"sbx_action{n}=([^,\s]+)", prompt) or [None, "none"])[1]
    container = (re.search(rf"sbx_container{n}=([^,\s]+)", prompt) or [None, container])[1]
    has_history = "Các lệnh đã được người vận hành duyệt" in prompt and "- vòng " in prompt

lines = [
    f"VẤN ĐỀ: (mock) cảnh báo {alert} trên {container or 'hệ thống'}" + (f", vòng {rnd.group(1)}, lịch sử lệnh: {'CÓ' if has_history else 'KHÔNG'}." if rnd else "."),
    "NGUYÊN NHÂN: (mock) giả lập trong sandbox, không phải kết luận thật.",
    "BẰNG CHỨNG:",
    f"- (mock) prompt có runbook: {'CÓ' if has_runbook else 'KHÔNG'} · bridge đã yêu cầu dòng hành động: {'CÓ' if asks_action else 'KHÔNG'}",
    "HƯỚNG XỬ LÝ:",
    "1. (mock) xem log rồi khởi động lại container nếu cần.",
]
if asks_action:
    lines.append("HÀNH ĐỘNG ĐỀ XUẤT: " + (f"{action} {container}" if action != "none" and container else "không có"))

if out:
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"result": "\n".join(lines)}, f, ensure_ascii=False)
