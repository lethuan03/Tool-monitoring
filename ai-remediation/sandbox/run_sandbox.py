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
SBX_TOKEN = "sandbox-tool-token-not-real-0123456789abcdef"      # giống REMEDIATOR_TOOL_TOKEN trong docker-compose.sandbox.yml
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
    headers = {"Content-Type": "application/json"}
    if url.startswith(BRIDGE):
        headers["Authorization"] = f"Bearer {SBX_TOKEN}"        # bridge bắt token cho /alert (giống Alertmanager thật)
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers=headers)
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
def audit_find(event, **kw):
    return [e for e in audit() if e["event"] == event and all(e.get(k) == v for k, v in kw.items())]


def phase_dry_run():
    print("\n=== PHA 1: DRY-RUN (duyệt xong chỉ ghi nhận, KHÔNG chạy) — AI (giả) gọi CÔNG CỤ thật ===")
    before = started_at(BACKEND)

    print("- A1: AI đề xuất restart backend qua công cụ propose_fix -> Telegram có tin phân tích + tin xin duyệt")
    analysis, proposal = propose_flow("a1", "restart_container", BACKEND)
    check("A1 có tin phân tích", analysis)
    check("A1 prompt đã chèn runbook (mock AI báo CÓ)", analysis and "runbook: CÓ" in analysis["text"])
    check("A1 bridge chỉ dẫn AI dùng công cụ (cách 2)", analysis and "công cụ: CÓ" in analysis["text"])
    check("A1 prompt đã chèn bản đồ hệ thống", analysis and "bản đồ hệ thống: CÓ" in analysis["text"])
    check("A1 không có dòng 'HÀNH ĐỘNG ĐỀ XUẤT' trong tin phân tích", analysis and "HÀNH ĐỘNG ĐỀ XUẤT" not in analysis["text"])
    check("A1 công cụ trả 'queued'", analysis and "propose_fix trả: queued" in analysis["text"])
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
        check("A2 audit có tool_propose của phiên AI", audit_find("tool_propose", fingerprint="a1"))

    print("- A3: AI đòi restart DB (container bị CẤM) -> policy chặn, AI được báo lý do + gợi ý NGAY trong phiên")
    db_before = started_at(DB)
    analysis, proposal = propose_flow("a3", "restart_container", DB)
    blocked = wait_for(lambda: msgs("Policy chặn"), 20)
    check("A3 policy chặn, báo Telegram", blocked)
    check("A3 tin báo nói rõ AI đã nhận lý do (không chìa nút chạy lệnh khác)",
          blocked and "AI đã được báo lý do" in blocked[0]["text"] and not blocked[0]["buttons"])
    check("A3 KHÔNG có nút duyệt cho DB", not proposal)
    check("A3 AI (mock) nhận 'denied' kèm 2 gợi ý thay thế ngay trong phiên",
          analysis and "propose_fix trả: denied; gợi ý thay thế: 2" in analysis["text"])
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

    print("- B2: đề xuất restart lại ngay -> chặn bởi cooldown, AI được báo 'rate_limited'")
    before2 = started_at(BACKEND)
    analysis, proposal = propose_flow("b2", "restart_container", BACKEND)
    check("B2 không có nút duyệt (đang cooldown)", analysis and not proposal)
    check("B2 AI (mock) nhận 'rate_limited' từ công cụ", analysis and "propose_fix trả: rate_limited" in analysis["text"])
    check("B2 audit denied do cooldown",
          wait_for(lambda: any(e["event"] == "denied" and e.get("fingerprint") == "b2" and "chờ" in e.get("why", "") for e in audit()), 10))
    check("B2 container không bị restart lần 2", started_at(BACKEND) == before2)

    print("- B3: logs_tail là hành động CHỈ ĐỌC -> AI đọc thẳng qua công cụ (không cần duyệt); người gõ /logs cũng chạy ngay")
    http("POST", MOCK + "/_reset", {})
    nginx_before = started_at(NGINX)
    fire("b3", "logs_tail", NGINX)
    check("B3 AI đọc log trực tiếp: audit tool_read", wait_for(lambda: audit_find("tool_read", fingerprint="b3"), 30))
    check("B3 không có tin xin duyệt cho việc đọc log", not wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 3))
    check("B3 không làm restart container", started_at(NGINX) == nginx_before)
    say("/logs ecommerce-sbx-nginx")
    check("B3 người gõ /logs -> Telegram trả ngay 'Log cuối của ...' (chỉ đọc, không cần nút Duyệt)",
          wait_for(lambda: msgs("Log cuối của"), 30) and not msgs("LỆNH BẠN VỪA GÕ"))

    print("- B4: DB vẫn bị cấm ở chế độ execute")
    db_before = started_at(DB)
    propose_flow("b4", "restart_container", DB)
    check("B4 policy chặn", wait_for(lambda: msgs("Policy chặn"), 20))
    check("B4 DB không bị đụng", started_at(DB) == db_before)

    print("- B5: gọi thẳng remediator với container ngoài danh sách (container thật của project khác) -> chặn")
    victim = sh(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=freshmart"], check=False).splitlines()
    target = victim[0] if victim else "some-other-container"
    t_before = started_at(target) if victim else ""
    probe = ("import urllib.request,urllib.error,json,sys\n"
             "h={'Content-Type':'application/json'}\n"
             "if sys.argv[2]: h['Authorization']='Bearer '+sys.argv[2]\n"
             "req=urllib.request.Request('http://remediator:8090/propose',data=json.dumps({'alert':'x','fingerprint':'b5','action_id':'restart_container','container':sys.argv[1],'reason':'t'}).encode(),headers=h)\n"
             "try:\n print(urllib.request.urlopen(req,timeout=10).status)\n"
             "except urllib.error.HTTPError as e:\n print(e.code)")
    code = sh(["docker", "exec", "sbx-holmes-bridge", "python", "-c", probe, target, ""])
    check("B5 gọi /propose KHÔNG có token -> 401 (container khác trong mạng không giả đề xuất được)", code.strip() == "401", code)
    code = sh(["docker", "exec", "sbx-holmes-bridge", "python", "-c", probe, target, SBX_TOKEN])
    check(f"B5 có token nhưng container ngoài danh sách '{target}' -> bị từ chối (HTTP 403)", code.strip() == "403", code)
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
    w_before = started_at("ecommerce-sbx-worker1")
    probe2 = sh(["docker", "exec", "sbx-remediator", "python", "-c",
                 "import urllib.request,urllib.error\n"
                 "def code(m,u,b=None):\n"
                 " r=urllib.request.Request('http://docker-proxy:2375'+u,data=b,method=m)\n"
                 " try: return urllib.request.urlopen(r,timeout=10).status\n"
                 " except urllib.error.HTTPError as e: return e.code\n"
                 "print(code('DELETE','/containers/ecommerce-sbx-worker1?force=1'),"
                 "code('PUT','/containers/ecommerce-sbx-worker1/archive?path=/tmp',b'x'),"
                 "code('POST','/containers/ecommerce-sbx-worker1/stop'),"
                 "code('POST','/containers/ecommerce-sbx-worker1/kill'),"
                 "code('POST','/containers/ecommerce-sbx-db/restart'),"
                 "code('POST','/containers/ecommerce-nonexistent-xyz/restart'),"
                 "code('GET','/containers/ecommerce-sbx-backend/logs?follow=1&stdout=1'),"
                 "code('GET','/containers/sbx-remediator/json'))"])
    check("B6 proxy allowlist chặn XOÁ, ghi file, stop, kill, restart DB, container ngoài sandbox, logs follow, đọc container hệ thống",
          probe2.split() == ["403"] * 8, probe2)
    check("B6 container sandbox không bị xoá/dừng sau khi dò", started_at("ecommerce-sbx-worker1") == w_before)

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
    print("\n=== PHA 3: CÔNG CỤ, VÒNG LẶP, GÕ LỆNH VÀ CÂU NÓI TỰ NHIÊN (chạy thật, chỉ container sandbox) ===")
    W1, W2, W3 = "ecommerce-sbx-worker1", "ecommerce-sbx-worker2", "ecommerce-sbx-worker3"

    print("- C1: AI đòi restart DB (bị cấm) -> được báo + gợi ý ngay trong phiên -> tự chọn lại -> nút Duyệt cho lệnh thay thế")
    db_before = started_at(DB)
    http("POST", MOCK + "/_reset", {})
    fire("c1", "restart_container", DB, {"sbx_retry": "1"})
    check("C1 có tin 'Policy chặn'", wait_for(lambda: msgs("Policy chặn"), 40))
    alt = wait_for(lambda: next((m for m in msgs("ĐỀ XUẤT LỆNH") if NGINX in m["text"] and "restart_container" in m["text"]), None), 30)
    check("C1 AI chọn lại đúng gợi ý (restart nginx) và có nút Duyệt", alt and len(alt["buttons"]) == 3)
    check("C1 audit: denied rồi tool_propose queued cho lệnh thay thế",
          audit_find("denied", fingerprint="c1") and audit_find("tool_propose", fingerprint="c1", result="queued"))
    if alt:
        click(alt["message_id"], 1)       # từ chối: không để lệnh chạy (tránh dính cooldown cho kịch bản sau)
        check("C1 người từ chối lệnh thay thế -> không chạy", wait_for(lambda: msgs("Đã từ chối"), 15))
    check("C1 DB không bị đụng", started_at(DB) == db_before)

    print("- C2: AI gọi công cụ với tên container sai dạng -> script công cụ chặn trước khi gọi remediator")
    http("POST", MOCK + "/_reset", {})
    fire("c2", "restart_container", "docker-exec-bash")
    an = wait_for(lambda: next(iter(msgs("AI PHÂN TÍCH")), None), 40)
    check("C2 AI nhận 'denied' từ công cụ", an and "propose_fix trả: denied" in an["text"])
    check("C2 không có tin xin duyệt, không có đề xuất nào trong audit",
          not msgs("ĐỀ XUẤT LỆNH") and not audit_find("proposed", fingerprint="c2") and not audit_find("tool_propose", fingerprint="c2"))

    print("- C3: vòng lặp: restart nginx (Prometheus giả: backend vẫn chết) -> AI vòng 2 nhận tóm tắt + lịch sử + bản đồ; lặp lại lệnh cũ thì được báo duplicate")
    http("POST", MOCK + "/_reset", {})
    http("POST", MOCK + "/_down", {"jobs": ["backend"]})
    nginx_before = started_at(NGINX)
    fire("c3", "restart_container", NGINX, {"sbx_action2": "restart_container", "sbx_container2": NGINX})
    p1 = wait_for(lambda: next((m for m in msgs("ĐỀ XUẤT LỆNH") if "vòng 1/3" in m["text"]), None), 40)
    check("C3 vòng 1: có đề xuất ghi 'vòng 1/3'", p1)
    if p1:
        click(p1["message_id"], 0)
        check("C3 nginx đã restart thật", wait_for(lambda: started_at(NGINX) != nginx_before, 30))
        check("C3 báo 'còn chưa trả lời: backend'", wait_for(lambda: msgs("còn chưa trả lời: backend"), 30))
        r2 = wait_for(lambda: next(iter(msgs("AI TIẾP TỤC (vòng 2/3)")), None), 60)
        check("C3 vòng 2: prompt có TÓM TẮT phân tích vòng 1 (AI không nhớ phiên cũ)", r2 and "tóm tắt vòng trước: CÓ" in r2["text"])
        check("C3 vòng 2: prompt có lịch sử lệnh đã chạy và bản đồ hệ thống",
              r2 and "lịch sử lệnh: CÓ" in r2["text"] and "bản đồ hệ thống: CÓ" in r2["text"])
        check("C3 vòng 2: AI lặp lại đúng lệnh vòng 1, công cụ báo 'duplicate' ngay trong phiên",
              r2 and "propose_fix trả: duplicate" in r2["text"])
        check("C3 AI không còn lệnh khác -> bridge báo vòng kết thúc, không có nút Duyệt mới",
              wait_for(lambda: msgs("không còn lệnh an toàn"), 20) and
              not [m for m in msgs("ĐỀ XUẤT LỆNH") if "vòng 2/3" in m["text"]])
        rounds = [e.get("round") for e in audit() if e["event"] == "executed" and e.get("fingerprint") == "c3"]
        check("C3 audit: chỉ vòng 1 được chạy", rounds == [1], str(rounds))
    http("POST", MOCK + "/_down", {"jobs": []})

    print("- C4: nút ⏹ Dừng trên tin xin duyệt -> huỷ đề xuất, không chạy gì")
    http("POST", MOCK + "/_reset", {})
    fire("c4", "restart_container", W1)
    p = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 40)
    check("C4 có tin xin duyệt", p)
    if p:
        click(p["message_id"], 2)
        check("C4 báo 'Đã dừng xử lý tự động'", wait_for(lambda: msgs("Đã dừng xử lý tự động"), 20))
        check("C4 audit loop_stopped", wait_for(lambda: audit_has("loop_stopped", "c4"), 10))
        click(p["message_id"], 0)
        time.sleep(2)
        check("C4 bấm Duyệt sau khi Dừng không chạy gì", not audit_find("executed", fingerprint="c4"))

    print("- C5: lệnh /stop gõ trong chat (người lạ bị bỏ qua, đúng người thì dừng)")
    http("POST", MOCK + "/_reset", {})
    fire("c5", "restart_container", W2)
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
    alert = fire("c6", "restart_container", W3)
    p = wait_for(lambda: next(iter(msgs("ĐỀ XUẤT LỆNH")), None), 40)
    check("C6 có tin xin duyệt", p)
    if p:
        resolve(alert)
        check("C6 audit loop_closed (bridge báo remediator khi RESOLVED)", wait_for(lambda: audit_has("loop_closed", "c6"), 20))
        check("C6 bridge vẫn gửi tin 'đã hoạt động bình thường'", wait_for(lambda: msgs("ĐÃ HOẠT ĐỘNG BÌNH THƯỜNG"), 20))
        click(p["message_id"], 0)
        time.sleep(2)
        check("C6 bấm Duyệt trên đề xuất đã bị huỷ không chạy gì", not audit_find("executed", fingerprint="c6"))

    print("- C7: gõ câu nói tự nhiên (đường A, từ khoá) -> hiện đúng lệnh đã hiểu kèm nút -> bấm Duyệt thì chạy thật")
    http("POST", MOCK + "/_reset", {})
    w1_before = started_at(W1)
    say(f"khởi động lại {W1}")
    p = wait_for(lambda: next(iter(msgs("LỆNH BẠN VỪA GÕ")), None), 15)
    check("C7 bot hiểu câu nói: hiện lệnh restart_container đúng container, 3 nút",
          p and "restart_container" in p["text"] and W1 in p["text"] and len(p["buttons"]) == 3)
    check("C7 chưa bấm Duyệt thì container chưa restart", started_at(W1) == w1_before)
    if p:
        click(p["message_id"], 0)
        check("C7 bấm Duyệt -> container THẬT sự restart", wait_for(lambda: started_at(W1) != w1_before, 30))
        check("C7 audit ghi manual_command và executed", audit_find("manual_command", approver="999") and
              any(e["event"] == "executed" and e.get("container") == W1 for e in audit()))

    print("- C8: câu nói / lệnh nguy hiểm (xoá container) -> KHÔNG ĐƯỢC PHÉP, không chạy gì")
    http("POST", MOCK + "/_reset", {})
    db_before = started_at(DB)
    say(f"xóa {DB} đi")
    check("C8 'xóa <container>' bị chặn", wait_for(lambda: msgs("KHÔNG ĐƯỢC PHÉP"), 15))
    http("POST", MOCK + "/_reset", {})
    say(f"docker rm -f {DB}")
    check("C8 'docker rm -f <container>' bị chặn", wait_for(lambda: msgs("KHÔNG ĐƯỢC PHÉP"), 15))
    check("C8 DB không bị đụng và không có nút nào", started_at(DB) == db_before and not msgs("LỆNH BẠN VỪA GÕ"))

    print("- C9: câu hỏi tự do (đường B): remediator nhờ AI qua bridge /chat; AI (mock) đọc trạng thái bằng công cụ")
    http("POST", MOCK + "/_reset", {})
    say(f"vì sao {NGINX} chậm vậy?")
    check("C9 bot báo đang nhờ AI", wait_for(lambda: msgs("Đang nhờ AI"), 15))
    ans = wait_for(lambda: next(iter(msgs("AI trả lời")), None), 60)
    check("C9 AI trả lời, đã đọc trạng thái container qua công cụ", ans and "đã đọc trạng thái" in ans["text"] and NGINX in ans["text"])
    check("C9 audit: chat_request queued + tool_read của phiên chat",
          audit_find("chat_request", result="queued") and
          any(e["event"] == "tool_read" and str(e.get("fingerprint", "")).startswith("chat-") for e in audit()))

    print("- C12: /ai: phiên làm việc với AI có trí nhớ; AI tự biết container nào cần chạy lại, đề xuất kèm nút Duyệt")
    http("POST", MOCK + "/_reset", {})
    say("/ai")
    check("C12 /ai mở phiên và hướng dẫn cách dùng", wait_for(lambda: msgs("Đã mở phiên làm việc với AI"), 15))
    say(f"{W2} bị treo rồi, xử lý giúp tôi")           # không có động từ rõ ràng -> phải nhờ AI hiểu và tự chọn lệnh
    reply = wait_for(lambda: next(iter(msgs("AI trả lời")), None), 60)
    check("C12 AI đọc trạng thái thật rồi tự chọn restart_container (container đang chạy)",
          reply and "running=True" in reply["text"] and "propose_fix restart_container: queued" in reply["text"])
    check("C12 AI báo đã gửi đề xuất kèm nút Duyệt", reply and "kèm nút Duyệt" in reply["text"])
    prop = wait_for(lambda: next((m for m in msgs("ĐỀ XUẤT LỆNH") if W2 in m["text"] and "restart_container" in m["text"]), None), 20)
    check("C12 có tin đề xuất đúng container, 3 nút (AI không tự chạy)", prop and len(prop["buttons"]) == 3)
    check("C12 chưa bấm Duyệt thì container chưa bị đụng", not audit_find("executed", container=W2))
    check("C12 tin đầu của phiên: AI nhận tình trạng hiện tại, chưa có lịch sử",
          reply and "tình trạng hiện tại: CÓ" in reply["text"] and "lịch sử hội thoại: KHÔNG" in reply["text"])
    if prop:
        click(prop["message_id"], 1)
    http("POST", MOCK + "/_reset", {})
    say("cái đó thì sao?")
    reply2 = wait_for(lambda: next(iter(msgs("AI trả lời")), None), 60)
    check("C12 tin thứ hai: AI nhớ lượt trước (có lịch sử hội thoại)", reply2 and "lịch sử hội thoại: CÓ" in reply2["text"])
    http("POST", MOCK + "/_reset", {})
    chats_before = len(audit_find("chat_request"))
    t0 = time.time()
    say(f"khởi động lại {W3}")                                 # câu lệnh RÕ RÀNG trong phiên: hiểu ngay bằng từ khoá, không chờ AI
    fast = wait_for(lambda: next(iter(msgs("LỆNH BẠN VỪA GÕ")), None), 15)
    check("C12 câu lệnh rõ ràng trong phiên được hiểu NGAY (dưới 5 giây) và không gọi AI",
          fast and W3 in fast["text"] and time.time() - t0 < 5 and len(audit_find("chat_request")) == chats_before)
    if fast:
        click(fast["message_id"], 1)
    http("POST", MOCK + "/_reset", {})
    say("/ai off")
    check("C12 /ai off kết thúc phiên", wait_for(lambda: msgs("Đã kết thúc phiên AI"), 15))
    check("C12 audit có ai_session_start và ai_session_end", audit_find("ai_session_start") and audit_find("ai_session_end"))
    http("POST", MOCK + "/_reset", {})
    say("ok")
    time.sleep(3)
    check("C12 sau khi đóng phiên, tin ngắn 'ok' bị bỏ qua (không gọi AI)", not msgs("AI trả lời"))

    print("- C13: chat TRỰC TIẾP (không Holmes, không công cụ): hỏi về bot / kiến thức chung -> trả lời nhanh, chữ hiện dần")
    http("POST", MOCK + "/_reset", {})
    say("/ai")
    wait_for(lambda: msgs("Đã mở phiên làm việc với AI"), 15)
    http("POST", MOCK + "/_reset", {})
    tools_before = len([e for e in audit() if e["event"] in ("tool_read", "tool_propose") and str(e.get("fingerprint", "")).startswith("chat-")])
    t0 = time.time()
    say("bạn làm được những gì?")
    ans = wait_for(lambda: next(iter(msgs("(mock-chat)")), None), 30)
    elapsed = time.time() - t0
    check("C13 trả lời bằng chat trực tiếp (mô hình giả), không qua Holmes", ans and "(mock) " not in ans["text"])
    check("C13 nhanh (dưới 8 giây tới chữ đầu tiên, không phải khởi động Holmes)", ans and elapsed < 8)
    full = wait_for(lambda: next((m for m in msgs("Lịch sử:")), None), 15)         # tin được sửa dần: đợi đủ câu rồi mới kiểm nội dung
    check("C13 chữ hiện dần: 1 tin được sửa nhiều lần cho tới khi đủ câu", full and len(state()["text_edits"]) >= 1)
    check("C13 prompt có luật [[OPS]], mô tả khả năng bot và bản đồ hệ thống", full and "Luật OPS: CÓ" in full["text"] and "bản đồ: CÓ" in full["text"])
    check("C13 tin đầu của phiên chưa có lịch sử", full and "Lịch sử: KHÔNG" in full["text"])
    check("C13 không dùng công cụ nào (không tool_read/tool_propose mới)",
          len([e for e in audit() if e["event"] in ("tool_read", "tool_propose") and str(e.get("fingerprint", "")).startswith("chat-")]) == tools_before)
    http("POST", MOCK + "/_reset", {})
    say("giải thích restart policy của docker")
    ans2 = wait_for(lambda: next(iter(msgs("(mock-chat)")), None), 30)
    check("C13 tin thứ hai trong phiên: chat trực tiếp nhớ lượt trước", ans2 and wait_for(lambda: next((m for m in msgs("Lịch sử: CÓ")), None), 15))

    print("- C14: câu cần dữ liệu thật: mô hình tự báo [[OPS]] -> bridge chuyển sang Holmes (không lộ dấu hiệu cho người dùng)")
    http("POST", MOCK + "/_reset", {})
    say("cho tôi biết hiện tại có gì bất thường không")
    esc = wait_for(lambda: next((m for m in msgs("AI trả lời") if "(mock)" in m["text"]), None), 60)
    check("C14 Holmes (mock) trả lời sau khi mô hình chat báo cần dữ liệu thật", esc)
    check("C14 người dùng không bao giờ thấy dấu hiệu [[OPS]]", not [m for m in state()["messages"] if "[[OPS]]" in m["text"]])
    lg = subprocess.run(["docker", "logs", "sbx-holmes-bridge"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    logs = lg.stdout + lg.stderr                       # log Python ghi ra stderr
    check("C14 log bridge ghi việc chuyển sang Holmes", "chuyển sang Holmes" in logs)
    http("POST", MOCK + "/_reset", {})
    say("/ai off")
    wait_for(lambda: msgs("Đã kết thúc phiên AI"), 15)

    print("- C10: /status <container>: chạy ngay, chỉ đọc, KHÔNG lộ biến môi trường")
    http("POST", MOCK + "/_reset", {})
    say(f"/status {DB}")
    shown = wait_for(lambda: next(iter(msgs("sbx-db ·")), None), 30)
    check("C10 /status db được phép (ngoại lệ chỉ đọc), trả ngay trạng thái đã lọc (có 'running'), không cần nút",
          shown and "running" in shown["text"] and not msgs("LỆNH BẠN VỪA GÕ"))
    leaked = (shown or {}).get("text", "") + json.dumps(audit())
    check("C10 KHÔNG lộ biến môi trường (PATH=...) trong Telegram và audit", "PATH=" not in leaked and "Env" not in leaked)

    print("- C11: hỏi tổng quan bằng câu nói tự nhiên -> bot lấy số liệu Docker + Prometheus và báo lại ngay (không AI)")
    http("POST", MOCK + "/_reset", {})
    chats_before = len(audit_find("chat_request"))       # audit giữ cả lịch sử (C9 đã tạo 1 bản ghi), nên so số lượng trước/sau
    say("các container hiện tại thế nào?")
    ov = wait_for(lambda: next(iter(msgs("Tình trạng hệ thống")), None), 40)
    check("C11 báo cáo tổng quan liệt kê container, đếm đủ 6/6 đang chạy", ov and "6/6" in ov["text"])
    check("C11 có dòng máy chủ, web và cảnh báo đang bật", ov and "Máy:" in ov["text"] and "Cảnh báo đang bật" in ov["text"])
    check("C11 không có nút, không gọi AI, không lộ biến môi trường",
          ov and not ov["buttons"] and "PATH=" not in ov["text"] and len(audit_find("chat_request")) == chats_before)
    http("POST", MOCK + "/_reset", {})
    sh(["docker", "stop", "ecommerce-sbx-worker3"])
    say("/status")
    ov2 = wait_for(lambda: next(iter(msgs("Tình trạng hệ thống")), None), 40)
    check("C11 container vừa dừng hiện 🔴 và đếm 5/6", ov2 and "🔴 sbx-worker3" in ov2["text"] and "5/6" in ov2["text"])
    sh(["docker", "start", "ecommerce-sbx-worker3"], check=False)


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
