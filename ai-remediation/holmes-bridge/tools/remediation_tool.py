#!/usr/bin/env python3
"""
Công cụ của AI để làm việc với hệ thống (cách 2). HolmesGPT gọi file này qua toolset `remediation`
trong monitoring/holmes/config.yaml; file chỉ nói chuyện với remediator qua HTTP nội bộ.

  remediation_tool.py catalog                              # kho lệnh + container được phép
  remediation_tool.py overview                             # báo cáo tổng quan: container, CPU/RAM máy, web, cảnh báo
  remediation_tool.py read    <action> <container>         # ĐỌC trạng thái/log/CPU-RAM (chạy ngay, kết quả đã lọc)
  remediation_tool.py propose <action> <container> <lý do> # ĐỀ XUẤT lệnh thay đổi (cần người duyệt trên Telegram)

AI KHÔNG chạy được lệnh nào qua file này: `propose` chỉ tạo nút Duyệt trên Telegram; luật nằm ở remediator
(policy.json), file này chỉ kiểm tra tên cho gọn. Danh tính sự cố (fingerprint/vòng/cảnh báo) đọc từ biến môi
trường do holmes-bridge đặt, AI không tự đặt được. Chỉ dùng thư viện chuẩn. Không in token.
In ra JSON (1 dòng) để AI đọc; luôn thoát mã 0 khi remediator trả lời (kể cả bị chặn) để AI đọc được lý do.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = (os.environ.get("REMEDIATOR_URL") or "").rstrip("/")
TOKEN = re.sub(r"[\s\"']", "", os.environ.get("REMEDIATOR_TOOL_TOKEN", ""))
ACTION_RE = re.compile(r"[a-z_]{3,40}")
CONTAINER_RE = re.compile(r"ecommerce-[A-Za-z0-9_.-]{1,60}")


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def identity():
    try:
        rnd = int(os.environ.get("AI_INCIDENT_ROUND", "1"))
    except ValueError:
        rnd = 1
    return {"fingerprint": os.environ.get("AI_INCIDENT_FP", "")[:80], "round": rnd,
            "alert": os.environ.get("AI_INCIDENT_ALERT", "?")[:80]}


def call(method, path, body=None):
    if not URL or not TOKEN:
        out({"status": "error", "message": "công cụ chưa được cấu hình (thiếu REMEDIATOR_URL / REMEDIATOR_TOOL_TOKEN)"}, 1)
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:   # 4xx từ remediator vẫn là câu trả lời hợp lệ cho AI (bị chặn, giới hạn...)
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"status": "error", "message": f"remediator trả HTTP {e.code}"}
    except Exception as e:
        out({"status": "error", "message": f"không gọi được remediator: {type(e).__name__}"}, 1)


def check_names(action, container):
    if not ACTION_RE.fullmatch(action or ""):
        out({"status": "denied", "message": "action sai dạng; xem list_allowed_actions"})
    if not CONTAINER_RE.fullmatch(container or ""):
        out({"status": "denied", "message": "container sai dạng; phải là tên đầy đủ dạng ecommerce-xxx (xem list_allowed_actions)"})


def main(argv):
    if not argv:
        out({"status": "error", "message": "thiếu lệnh: catalog | overview | read | propose"}, 2)
    cmd = argv[0]
    if cmd == "catalog":
        out(call("GET", "/tool/catalog"))
    if cmd == "overview":
        out(call("GET", "/tool/overview"))
    if cmd == "read" and len(argv) == 3:
        check_names(argv[1], argv[2])
        out(call("POST", "/tool/read", dict(identity(), action_id=argv[1], container=argv[2])))
    if cmd == "propose" and len(argv) >= 3:
        check_names(argv[1], argv[2])
        reason = " ".join(argv[3:])[:300]
        out(call("POST", "/tool/propose", dict(identity(), action_id=argv[1], container=argv[2], reason=reason)))
    out({"status": "error", "message": "dùng: catalog | read <action> <container> | propose <action> <container> <lý do>"}, 2)


if __name__ == "__main__":
    main(sys.argv[1:])
