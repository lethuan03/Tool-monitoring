#!/usr/bin/env python3
"""
Chạy sandbox test cục bộ cho "AI đề xuất lệnh sửa lỗi" (README2.md, mục Sandbox).

  python ai-remediation/sandbox/run_sandbox.py           # dựng sandbox + chạy kịch bản dry-run, execute, vòng lặp
  python ai-remediation/sandbox/run_sandbox.py up        # chỉ dựng
  python ai-remediation/sandbox/run_sandbox.py test      # chỉ chạy kịch bản (sandbox đã dựng)
  python ai-remediation/sandbox/run_sandbox.py down      # dọn sạch (container, mạng, volume của sandbox)

Chỉ đụng tới container ecommerce-sbx-* và sbx-* của chính sandbox (project compose "ai-sandbox").
Không AWS, không Bedrock, không đọc .env. Chạy từ thư mục gốc repo. Chỉ dùng thư viện chuẩn.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = ["docker", "compose", "-p", "ai-sandbox", "-f", os.path.join(ROOT, "sandbox", "docker-compose.sandbox.yml")]
MOCK, BRIDGE = "http://127.0.0.1:18081", "http://127.0.0.1:18080"
BACKEND, NGINX, DB = "ecommerce-sbx-backend", "ecommerce-sbx-nginx", "ecommerce-sbx-db"

results = []
known_gaps = []


def sh(args, env=None, check=True):
    e = dict(os.environ, **(env or {}))
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=e, cwd=ROOT)
    if check and p.returncode != 0:
        raise SystemExit(f"Lệnh lỗi: {' '.join(args)}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def http(method, url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw}  # bridge trả "ok" dạng text


def state():
    return http("GET", MOCK + "/_state")


def wait_for(pred, timeout=40, what=""):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    return None


def started_at(name):
    return sh(["docker", "inspect", "-f", "{{.State.StartedAt}}", name])


def audit():
    raw = sh(["docker", "exec", "sbx-remediator", "cat", "/audit/audit.jsonl"], check=False)
    return [json.loads(l) for l in raw.splitlines() if l.strip().startswith("{")]


def audit_has(event, fp=None, **kw):
    return any(e["event"] == event and (fp is None or e.get("fingerprint") == fp) and
               all(k in e.get("why", "") if k == "why_has" else True for k in kw) for e in audit())


def msgs(substr):
    return [m for m in state()["messages"] if substr in m["text"]]


def fire(fp, action, container, extra=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    alert = {"status": "firing", "fingerprint": fp, "startsAt": now,
             "labels": dict({"alertname": "TargetDown", "severity": "critical", "job": "backend", "instance": "sbx:3000",
                             "sbx_action": action, "sbx_container": container}, **(extra or {})),
             "annotations": {"summary": "(sandbox) target không trả lời", "description": "giả lập"}}
    http("POST", BRIDGE + "/alert", {"alerts": [alert]})
    return alert


def resolve(alert):
    http("POST", BRIDGE + "/alert", {"alerts": [dict(alert, status="resolved", endsAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))]})


def say(text, user=999, chat=111):
    http("POST", MOCK + "/_say", {"text": text, "user_id": user, "chat_id": chat})


def click(message_id, button, user=999, chat=111):
    http("POST", MOCK + "/_click", {"message_id": message_id, "button": button, "user_id": user, "chat_id": chat})


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


def wait_remediator():
    def ok():
        p = subprocess.run(["docker", "exec", "sbx-remediator", "python", "-c",
                            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=2)"],
                           capture_output=True)
        return p.returncode == 0
    if not wait_for(ok, 60):
        raise SystemExit("remediator không lên được: docker logs sbx-remediator")


def up(mode="dry-run"):
    print(f"==> Dựng sandbox (chế độ {mode})...")
    sh(COMPOSE + ["up", "-d", "--build", "--force-recreate", "remediator"], env={"SBX_MODE": mode})
    sh(COMPOSE + ["up", "-d", "--build"], env={"SBX_MODE": mode})
    wait_remediator()
    wait_for(lambda: http("GET", MOCK + "/_state"), 30)
    http("POST", MOCK + "/_reset", {})


def propose_flow(fp, action, container):
    """Bắn cảnh báo, đợi tin phân tích + tin đề xuất. Trả (tin_phân_tích, tin_đề_xuất|None)."""
    http("POST", MOCK + "/_reset", {})
    fire(fp, action, container)
    analysis = wait_for(lambda: next(iter(msgs("AI PHÂN TÍCH")), None), 40)
    proposal = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 8) if analysis else None
    return analysis, proposal


# ------------------------------------------------------------------ kịch bản
def phase_dry_run():
    print("\n=== PHA 1: DRY-RUN (duyệt xong chỉ ghi nhận, KHÔNG chạy) ===")
    before = started_at(BACKEND)

    print("- A1: AI đề xuất restart backend -> Telegram có tin phân tích + tin xin duyệt")
    analysis, proposal = propose_flow("a1", "restart_container", BACKEND)
    check("A1 có tin phân tích", analysis)
    check("A1 prompt đã chèn runbook (mock AI báo CÓ)", analysis and "runbook: CÓ" in analysis["text"])
    check("A1 bridge đã yêu cầu dòng hành động", analysis and "dòng hành động: CÓ" in analysis["text"])
    check("A1 dòng 'HÀNH ĐỘNG ĐỀ XUẤT' đã bị bóc khỏi tin phân tích", analysis and "HÀNH ĐỘNG ĐỀ XUẤT" not in analysis["text"])
    check("A1 có tin xin duyệt kèm 3 nút (Duyệt / Từ chối / Dừng)", proposal and len(proposal["buttons"]) == 3)
    check("A1 tin xin duyệt nêu đúng container và DRY-RUN",
          proposal and BACKEND in proposal["text"] and "DRY-RUN" in proposal["text"])
    if proposal:
        click(proposal["message_id"], 0)
        ok = wait_for(lambda: msgs("DRY-RUN</b>: đã duyệt"), 20)
        check("A2 bấm Duyệt -> báo 'DRY-RUN: đã duyệt nhưng KHÔNG chạy'", ok)
        check("A2 container KHÔNG bị restart", started_at(BACKEND) == before)
        check("A2 audit có approved_dry_run, executed=false",
              any(e["event"] == "approved_dry_run" and e.get("fingerprint") == "a1" and e.get("executed") is False for e in audit()))
        check("A2 audit ghi người duyệt = 999", any(e.get("approver") == "999" for e in audit() if e["event"] == "approved_dry_run"))

    print("- A3: AI đòi restart DB (container bị CẤM) -> policy chặn")
    db_before = started_at(DB)
    analysis, proposal = propose_flow("a3", "restart_container", DB)
    blocked = wait_for(lambda: msgs("Policy chặn"), 20)
    check("A3 policy chặn, báo Telegram", blocked)
    check("A3 KHÔNG có nút duyệt cho DB", not proposal)
    alt = next(iter(msgs("Lệnh thay thế")), None)
    check("A3 báo kèm LỆNH THAY THẾ có nút duyệt riêng", alt and len(alt["buttons"]) >= 2)
    check("A3 không gợi ý nào đụng tới DB", alt and all(DB not in b["text"] for b in alt["buttons"]))
    check("A3 audit denied", wait_for(lambda: any(e["event"] == "denied" and e.get("container") == DB for e in audit()), 10))
    check("A3 DB không bị đụng", started_at(DB) == db_before)

    print("- A4: người lạ / sai chat bấm Duyệt -> bị từ chối; rồi người đúng bấm Từ chối")
    analysis, proposal = propose_flow("a4", "restart_container", NGINX)
    check("A4 có tin xin duyệt", proposal)
    if proposal:
        click(proposal["message_id"], 0, user=123)
        click(proposal["message_id"], 0, chat=222)
        check("A4 bấm sai người/sai chat -> audit unauthorized_click x2",
              wait_for(lambda: sum(e["event"] == "unauthorized_click" for e in audit()) >= 2, 15))
        click(proposal["message_id"], 1)
        check("A4 người đúng bấm Từ chối -> báo đã từ chối", wait_for(lambda: msgs("Đã từ chối"), 15))
        check("A4 audit có rejected", wait_for(lambda: audit_has("rejected", "a4"), 10))

    print("- A5: AI kết luận 'không có' hành động -> chỉ có tin phân tích")
    analysis, proposal = propose_flow("a5", "none", "")
    check("A5 có tin phân tích, KHÔNG có tin xin duyệt", analysis and not proposal)


def phase_execute():
    print("\n=== PHA 2: EXECUTE (chạy thật qua docker-proxy, chỉ trên container sandbox) ===")
    up("execute")

    print("- B1: duyệt restart backend -> container thật sự được restart")
    before = started_at(BACKEND)
    analysis, proposal = propose_flow("b1", "restart_container", BACKEND)
    check("B1 có tin xin duyệt, ghi 'CHẠY THẬT'", proposal and "CHẠY THẬT" in proposal["text"])
    if proposal:
        click(proposal["message_id"], 0)
        done = wait_for(lambda: msgs("HTTP 204"), 40)
        check("B1 Telegram báo HTTP 204 (docker-proxy cho phép restart)", done)
        check("B1 container ĐÃ restart (StartedAt đổi)", wait_for(lambda: started_at(BACKEND) != before, 10))
        check("B1 báo kiểm tra lại Prometheus", done and wait_for(lambda: msgs("mọi thành phần đang trả lời"), 15))
        ex = [e for e in audit() if e["event"] == "executed" and e.get("fingerprint") == "b1"]
        check("B1 audit executed: success, đúng lệnh, đúng người duyệt",
              ex and ex[0].get("success") and ex[0]["command"] == f"POST /containers/{BACKEND}/restart" and ex[0]["approver"] == "999")

    print("- B2: đề xuất restart lại ngay -> chặn bởi cooldown")
    before2 = started_at(BACKEND)
    analysis, proposal = propose_flow("b2", "restart_container", BACKEND)
    check("B2 không có nút duyệt (đang cooldown)", analysis and not proposal)
    check("B2 audit denied do cooldown",
          wait_for(lambda: any(e["event"] == "denied" and e.get("fingerprint") == "b2" and "chờ" in e.get("why", "") for e in audit()), 10))
    check("B2 container không bị restart lần 2", started_at(BACKEND) == before2)

    print("- B3: logs_tail (chỉ đọc) -> trả log lên Telegram")
    analysis, proposal = propose_flow("b3", "logs_tail", NGINX)
    check("B3 có tin xin duyệt", proposal)
    if proposal:
        nginx_before = started_at(NGINX)
        click(proposal["message_id"], 0)
        check("B3 Telegram trả phần 'Log cuối của ...'", wait_for(lambda: msgs("Log cuối của"), 30))
        check("B3 không làm restart container", started_at(NGINX) == nginx_before)
        check("B3 log được gửi lại cho AI; AI (mock) không còn lệnh -> vòng kết thúc",
              wait_for(lambda: msgs("không còn lệnh an toàn"), 40))

    print("- B4: DB vẫn bị cấm ở chế độ execute")
    db_before = started_at(DB)
    propose_flow("b4", "restart_container", DB)
    check("B4 policy chặn", wait_for(lambda: msgs("Policy chặn"), 20))
    check("B4 DB không bị đụng", started_at(DB) == db_before)

    print("- B5: gọi thẳng remediator với container ngoài danh sách (container thật của project khác) -> chặn")
    victim = sh(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=freshmart"], check=False).splitlines()
    target = victim[0] if victim else "some-other-container"
    t_before = started_at(target) if victim else ""
    code = sh(["docker", "exec", "sbx-holmes-bridge", "python", "-c",
               "import urllib.request,urllib.error,json,sys\n"
               "req=urllib.request.Request('http://remediator:8090/propose',data=json.dumps({'alert':'x','fingerprint':'b5','action_id':'restart_container','container':sys.argv[1],'reason':'t'}).encode())\n"
               "try:\n print(urllib.request.urlopen(req,timeout=10).status)\n"
               "except urllib.error.HTTPError as e:\n print(e.code)", target])
    check(f"B5 container ngoài danh sách '{target}' bị từ chối (HTTP 403)", code.strip() == "403", code)
    if victim:
        check("B5 container của project khác không bị đụng", started_at(target) == t_before)

    print("- B6: docker-proxy chặn API nguy hiểm kể cả khi remediator bị chiếm (thử từ mạng dockernet)")
    probe = sh(["docker", "exec", "sbx-remediator", "python", "-c",
                "import urllib.request,urllib.error\n"
                "def code(m,u,b=None):\n"
                " r=urllib.request.Request('http://docker-proxy:2375'+u,data=b,method=m)\n"
                " try: return urllib.request.urlopen(r,timeout=10).status\n"
                " except urllib.error.HTTPError as e: return e.code\n"
                "print(code('GET','/images/json'),code('GET','/volumes'),code('GET','/info'),code('POST','/exec/abc/start',b'{}'))"])
    check("B6 /images, /volumes, /info, /exec/<id>/start đều bị proxy chặn (403)", probe.split() == ["403"] * 4, probe)

    # Thăm dò lỗ hổng đã biết: proxy bật CONTAINERS+POST nên cho TẠO container. Tạo xong xoá ngay.
    gap = sh(["docker", "exec", "sbx-remediator", "python", "-c",
              "import urllib.request,urllib.error,json\n"
              "r=urllib.request.Request('http://docker-proxy:2375/containers/create?name=sbx-probe-delete-me',"
              "data=json.dumps({'Image':'alpine:3.20'}).encode(),headers={'Content-Type':'application/json'})\n"
              "try: print(urllib.request.urlopen(r,timeout=10).status)\n"
              "except urllib.error.HTTPError as e: print(e.code)"])
    sh(["docker", "rm", "-f", "sbx-probe-delete-me"], check=False)
    if gap.strip() == "201":
        known_gaps.append("docker-proxy (CONTAINERS=1 + POST=1) CHO PHÉP TẠO container mới (POST /containers/create -> 201). "
                          "Nếu mã remediator bị chiếm thì có thể tạo + start container tuỳ ý. Cần proxy allowlist chặt hơn.")
        print("  GAP   B6 proxy cho phép tạo container (lỗ hổng đã biết, xem README2) — không tính là thất bại")
    else:
        check("B6 proxy chặn tạo container", True)


def phase_loop():
    print("\n=== PHA 3: VÒNG LẶP AI <-> NGƯỜI DUYỆT (chạy thật, chỉ container sandbox) ===")

    print("- C1: AI đòi restart DB (bị cấm) -> người bấm 1 lệnh THAY THẾ -> lệnh đó chạy thật")
    db_before = started_at(DB)
    http("POST", MOCK + "/_reset", {})
    fire("c1", "restart_container", DB)
    alt = wait_for(lambda: next(iter(msgs("Lệnh thay thế")), None), 40)
    check("C1 có tin 'Policy chặn' kèm lệnh thay thế", alt)
    if alt:
        btn = next((i for i, b in enumerate(alt["buttons"]) if "logs_tail" in b["text"]), None)
        check("C1 có nút gợi ý logs_tail", btn is not None)
        if btn is not None:
            click(alt["message_id"], btn)
            check("C1 bấm gợi ý -> lệnh thay thế chạy thật (trả log backend)", wait_for(lambda: msgs("Log cuối của"), 30))
            check("C1 audit: alternatives_offered + executed logs_tail",
                  audit_has("alternatives_offered", "c1") and
                  any(e["event"] == "executed" and e.get("fingerprint") == "c1" and e.get("action_id") == "logs_tail" for e in audit()))
    check("C1 DB không bị đụng", started_at(DB) == db_before)

    print("- C2: AI viết lệnh sai dạng (không phải action_id + container) -> policy chặn, KHÔNG im lặng")
    http("POST", MOCK + "/_reset", {})
    fire("c2", "restart_container", "docker-exec-bash")
    check("C2 admin được báo 'Policy chặn' kèm đề xuất thay thế",
          wait_for(lambda: next((m for m in msgs("Policy chặn") if "docker-exec-bash" in m["text"] and m["buttons"]), None), 40))

    print("- C3: vòng lặp 3 vòng: restart nginx (chưa ổn) -> AI xem kết quả -> logs_tail -> AI hết cách")
    http("POST", MOCK + "/_reset", {})
    http("POST", MOCK + "/_down", {"jobs": ["backend"]})            # Prometheus giả: backend vẫn chết sau lệnh
    nginx_before = started_at(NGINX)
    fire("c3", "restart_container", NGINX, {"sbx_action2": "logs_tail"})
    p1 = wait_for(lambda: next((m for m in msgs("ĐỀ XUẤT LỆNH") if "vòng 1/3" in m["text"]), None), 40)
    check("C3 vòng 1: có đề xuất ghi 'vòng 1/3'", p1)
    if p1:
        click(p1["message_id"], 0)
        check("C3 nginx đã restart thật", wait_for(lambda: started_at(NGINX) != nginx_before, 30))
        check("C3 báo 'còn chưa trả lời: backend'", wait_for(lambda: msgs("còn chưa trả lời: backend"), 30))
        r2 = wait_for(lambda: next(iter(msgs("AI TIẾP TỤC (vòng 2/3)")), None), 40)
        check("C3 AI được gọi lại ở vòng 2 và nhận được LỊCH SỬ lệnh vừa chạy", r2 and "lịch sử lệnh: CÓ" in r2["text"])
        p2 = wait_for(lambda: next((m for m in msgs("ĐỀ XUẤT LỆNH") if "vòng 2/3" in m["text"]), None), 20)
        check("C3 vòng 2: AI đề xuất lệnh mới (logs_tail), lại hỏi duyệt", p2 and "logs_tail" in p2["text"])
        if p2:
            http("POST", MOCK + "/_down", {"jobs": []})
            click(p2["message_id"], 0)
            check("C3 vòng 2: lệnh chạy, trả log", wait_for(lambda: msgs("Log cuối của"), 30))
            check("C3 vòng 3: AI không còn lệnh an toàn -> vòng kết thúc", wait_for(lambda: msgs("không còn lệnh an toàn"), 40))
        ex = [e.get("round") for e in audit() if e["event"] == "executed" and e.get("fingerprint") == "c3"]
        check("C3 audit ghi đúng số vòng của các lệnh đã chạy (1 rồi 2)", ex == [1, 2], str(ex))
    http("POST", MOCK + "/_down", {"jobs": []})

    print("- C4: nút ⏹ Dừng trên tin xin duyệt -> huỷ đề xuất, không chạy gì")
    http("POST", MOCK + "/_reset", {})
    fire("c4", "logs_tail", BACKEND)
    p = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 40)
    check("C4 có tin xin duyệt", p)
    if p:
        click(p["message_id"], 2)
        check("C4 báo 'Đã dừng xử lý tự động'", wait_for(lambda: msgs("Đã dừng xử lý tự động"), 20))
        check("C4 audit loop_stopped", wait_for(lambda: audit_has("loop_stopped", "c4"), 10))
        click(p["message_id"], 0)                                  # nút cũ đã bị huỷ -> bấm vô hiệu
        time.sleep(2)
        check("C4 bấm Duyệt sau khi Dừng không chạy gì", not msgs("Log cuối của"))

    print("- C5: lệnh /stop gõ trong chat (người lạ bị bỏ qua, đúng người thì dừng)")
    http("POST", MOCK + "/_reset", {})
    fire("c5", "logs_tail", BACKEND)
    p = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 40)
    check("C5 có tin xin duyệt", p)
    if p:
        say("/stop", user=123)
        check("C5 người không có quyền gõ /stop -> audit unauthorized_stop, vòng vẫn chạy",
              wait_for(lambda: any(e["event"] == "unauthorized_stop" for e in audit()), 15) and not msgs("Đã dừng xử lý tự động"))
        say("/stop", user=999)
        check("C5 người duyệt gõ /stop -> dừng", wait_for(lambda: msgs("Đã dừng xử lý tự động"), 20))

    print("- C6: cảnh báo RESOLVED -> đề xuất đang chờ bị huỷ, vòng đóng")
    http("POST", MOCK + "/_reset", {})
    alert = fire("c6", "logs_tail", BACKEND)
    p = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 40)
    check("C6 có tin xin duyệt", p)
    if p:
        resolve(alert)
        check("C6 audit loop_closed (bridge báo remediator khi RESOLVED)", wait_for(lambda: audit_has("loop_closed", "c6"), 20))
        check("C6 bridge vẫn gửi tin 'đã hoạt động bình thường'", wait_for(lambda: msgs("ĐÃ HOẠT ĐỘNG BÌNH THƯỜNG"), 20))
        click(p["message_id"], 0)
        time.sleep(2)
        check("C6 bấm Duyệt trên đề xuất đã bị huỷ không chạy gì", not msgs("Log cuối của"))


def summary():
    bad = [n for n, ok in results if not ok]
    print(f"\n=== KẾT QUẢ: {len(results) - len(bad)}/{len(results)} đạt ===")
    for n in bad:
        print("  THẤT BẠI:", n)
    for g in known_gaps:
        print("  LỖ HỔNG ĐÃ BIẾT:", g)
    return 1 if bad else 0


def down():
    print("==> Dọn sandbox...")
    sh(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "down":
        down()
    elif cmd == "up":
        up()
    elif cmd == "test":
        phase_dry_run(); phase_execute(); phase_loop(); sys.exit(summary())
    else:
        up(); phase_dry_run(); phase_execute(); phase_loop()
        code = summary()
        print("\nSandbox vẫn đang chạy để bạn xem log. Dọn: python ai-remediation/sandbox/run_sandbox.py down")
        sys.exit(code)
