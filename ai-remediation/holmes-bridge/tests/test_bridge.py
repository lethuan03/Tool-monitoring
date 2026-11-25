"""Test holmes-bridge hoàn toàn cục bộ: không mạng, không Bedrock, không Telegram thật.
Chạy:  python -m unittest discover -s ai-remediation/holmes-bridge/tests -v
"""
import importlib.util
import json
import os
import re
import sys
import time
import unittest
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REMEDIATOR_DIR = os.path.join(os.path.dirname(ROOT), "remediator")

os.environ.update({"TELEGRAM_BOT_TOKEN": "TEST_TOKEN", "TELEGRAM_CHAT_ID": "111",
                   "REMEDIATOR_URL": "http://remediator:8090", "AI_MAX_ROUNDS": "3", "AI_MAX_PER_HOUR": "6"})
spec = importlib.util.spec_from_file_location("bridge", os.path.join(ROOT, "bridge.py"))
B = importlib.util.module_from_spec(spec)
sys.modules["bridge"] = B
spec.loader.exec_module(B)

ALERT = {"fingerprint": "fp1", "startsAt": "2026-01-01T00:00:00Z", "status": "firing",
         "labels": {"alertname": "TargetDown", "severity": "critical"}, "annotations": {"summary": "s"}}


class ExtractActionTests(unittest.TestCase):
    def test_valid_line(self):
        text, p = B.extract_action("VẤN ĐỀ: x\nHÀNH ĐỘNG ĐỀ XUẤT: `restart_container ecommerce-backend`")
        self.assertEqual(p, {"action_id": "restart_container", "container": "ecommerce-backend"})
        self.assertNotIn("HÀNH ĐỘNG", text)

    def test_no_action_is_not_a_proposal(self):
        for v in ("không có", "Không có", "`không có`", "none", "N/A", "-", ""):
            self.assertIsNone(B.extract_action(f"HÀNH ĐỘNG ĐỀ XUẤT: {v}")[1], v)

    def test_unparseable_line_is_forwarded_as_raw_so_policy_can_report(self):
        _, p = B.extract_action("HÀNH ĐỘNG ĐỀ XUẤT: docker exec -it ecommerce-db mysql -e 'drop database shop'")
        self.assertIsNotNone(p)
        self.assertEqual(p["container"], "")
        self.assertIn("docker exec", p["raw"])
        self.assertLessEqual(len(p["raw"]), 100)

    def test_other_container_prefix_is_forwarded_not_executed(self):
        _, p = B.extract_action("HÀNH ĐỘNG ĐỀ XUẤT: restart_container other-app")
        self.assertEqual(p["action_id"], "khong_hop_le")

    def test_only_first_action_line_counts(self):
        _, p = B.extract_action("HÀNH ĐỘNG ĐỀ XUẤT: logs_tail ecommerce-nginx\nHÀNH ĐỘNG ĐỀ XUẤT: restart_container ecommerce-backend")
        self.assertEqual(p["container"], "ecommerce-nginx")


class PromptTests(unittest.TestCase):
    def test_followup_contains_history_round_and_runbook(self):
        payload = {"history": [{"round": 1, "action_id": "restart_container", "container": "ecommerce-backend",
                                "ok": True, "http": 204, "output": ""}],
                   "status": "chưa ổn", "down_jobs": ["backend"]}
        prompt = B.build_followup_prompt(ALERT, payload, 2)
        self.assertIn("VÒNG 2/3", prompt)
        self.assertIn("restart_container ecommerce-backend", prompt)
        self.assertIn("backend", prompt)
        self.assertIn("KHÁC", prompt)
        self.assertIn("HÀNH ĐỘNG ĐỀ XUẤT", prompt)  # REMEDIATOR_URL đang bật

    def test_followup_history_is_capped(self):
        hist = [{"round": i, "action_id": "logs_tail", "container": "c", "ok": True, "http": 200, "output": "x" * 5000} for i in range(20)]
        prompt = B.build_followup_prompt(ALERT, {"history": hist}, 2)
        self.assertLess(len(prompt), 8000 + B.KNOWLEDGE_MAX_CHARS)   # lịch sử lệnh bị cắt; phần còn lại là runbook + bản đồ hệ thống


class ContinueTests(unittest.TestCase):
    def setUp(self):
        B.state.clear()
        while not B.jobs.empty():
            B.jobs.get_nowait()
        B.recent_runs.clear()
        self.sent = []
        B.tg_send = lambda text, reply_to=None: self.sent.append(text) or 1
        B.state["fp1"] = {"starts": ALERT["startsAt"], "msg_id": 5, "resolved": False, "ts": 1e18, "alert": ALERT, "round": 1}

    def test_queues_next_round(self):
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": 1})[0], 200)
        self.assertEqual(B.jobs.qsize(), 1)
        self.assertEqual(B.jobs.get_nowait()[0], "followup")

    def test_double_continue_queues_once(self):
        B.handle_continue({"fingerprint": "fp1", "round": 1})
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": 1}), (200, "already queued"))
        self.assertEqual(B.jobs.qsize(), 1)

    def test_stops_at_max_rounds(self):
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": B.MAX_ROUNDS})[0], 429)
        self.assertEqual(B.jobs.qsize(), 0)

    def test_unknown_or_resolved_incident_rejected(self):
        self.assertEqual(B.handle_continue({"fingerprint": "nope", "round": 1})[0], 404)
        B.state["fp1"]["resolved"] = True
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": 1})[0], 409)

    def test_bad_round(self):
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": "abc"})[0], 400)

    def test_hourly_limit_applies_to_followups(self):
        for _ in range(B.MAX_PER_HOUR):
            B.recent_runs.append(__import__("time").time())
        self.assertEqual(B.handle_continue({"fingerprint": "fp1", "round": 1})[0], 429)
        self.assertEqual(B.jobs.qsize(), 0)
        self.assertTrue(self.sent)  # có báo lý do dừng lên Telegram


class RunbookConsistencyTests(unittest.TestCase):
    """Runbook chỉ được nhắc action_id có trong policy.json (tránh AI được dạy lệnh mà policy sẽ chặn)."""

    def test_runbook_actions_exist_in_policy(self):
        with open(os.path.join(REMEDIATOR_DIR, "policy.json"), encoding="utf-8") as f:
            policy = json.load(f)
        known = set(policy["actions"])
        forbidden = policy["containers_forbidden"]
        rb_dir = os.path.join(ROOT, "runbooks")
        for name in os.listdir(rb_dir):
            with open(os.path.join(rb_dir, name), encoding="utf-8") as f:
                text = f.read()
            for a in re.findall(r"`((?:restart|start|stop|exec|rm|remove|kill|prune|logs)_[a-z_]+)`", text):
                self.assertIn(a, known, f"{name}: action '{a}' không có trong policy.json")
            for line in text.splitlines():  # dòng đề xuất action trên container bị cấm mà không có chữ KHÔNG/không phải/chặn/cấm
                allowed_here = [c for a, spec in policy["actions"].items() if a in line for c in spec.get("allow_on_forbidden", [])]
                if any(a in line for a in known) and any(c in line and c not in allowed_here for c in forbidden):
                    self.assertTrue(re.search(r"KHÔNG|không phải|chặn|cấm", line), f"{name}: dạy AI đề xuất lệnh trên container bị cấm: {line}")


class ToolsModeTests(unittest.TestCase):
    """Cách 2: AI gọi công cụ. Kiểm tra prompt, tóm tắt phân tích, danh tính sự cố qua env, client công cụ, cấu hình Holmes."""

    def setUp(self):
        self._use_tools = B.USE_TOOLS
        self.addCleanup(lambda: setattr(B, "USE_TOOLS", self._use_tools))

    def test_action_hint_switches_between_tools_and_text_line(self):
        B.USE_TOOLS = True
        h = B.action_hint()
        self.assertIn("propose_fix", h)
        self.assertIn("read_status", h)
        self.assertNotIn("<đúng 1 dòng dạng", h)               # không còn dạy AI ghi dòng chữ
        B.USE_TOOLS = False
        self.assertIn("<đúng 1 dòng dạng", B.action_hint())    # đường lùi giữ nguyên cách cũ

    def test_prompts_use_tools_hint_in_tools_mode(self):
        B.USE_TOOLS = True
        self.assertIn("propose_fix", B.build_prompt(ALERT))
        self.assertIn("propose_fix", B.build_followup_prompt(ALERT, {"history": []}, 2))

    def test_summarize_analysis_keeps_sections_and_caps_length(self):
        text = "VẤN ĐỀ: db dừng\nNGUYÊN NHÂN: shutdown\nBẰNG CHỨNG:\n- log " + "x" * 5000 + "\nHƯỚNG XỬ LÝ:\n1. bật lại"
        s = B.summarize_analysis(text)
        self.assertIn("VẤN ĐỀ: db dừng", s)
        self.assertIn("NGUYÊN NHÂN: shutdown", s)
        self.assertNotIn("HƯỚNG XỬ LÝ", s)
        self.assertLessEqual(len(s), B.ANALYSIS_SUMMARY_CHARS)
        self.assertEqual(B.summarize_analysis(""), "")

    def test_followup_prompt_carries_previous_analysis(self):
        p = B.build_followup_prompt(ALERT, {"history": []}, 2, "VẤN ĐỀ: db dừng\nNGUYÊN NHÂN: shutdown")
        self.assertIn("db dừng", p)
        self.assertIn("không nhớ phiên cũ", p)
        self.assertNotIn("Phân tích của chính bạn", B.build_followup_prompt(ALERT, {"history": []}, 2))

    def test_run_holmes_sets_incident_identity_in_environment(self):
        captured = {}

        class P:
            returncode, stderr, stdout = 1, "", ""
        orig = B.subprocess.run
        B.subprocess.run = lambda cmd, **kw: (captured.update(kw) or P())
        try:
            B.run_holmes(ALERT, prompt="hi", fp="fp-123", rnd=2)
        finally:
            B.subprocess.run = orig
        env = captured["env"]
        self.assertEqual((env["AI_INCIDENT_FP"], env["AI_INCIDENT_ROUND"], env["AI_INCIDENT_ALERT"]), ("fp-123", "2", "TargetDown"))

    def test_tool_session_queued_handles_failure_without_raising(self):
        B.TOOL_TOKEN = "tok"
        orig = B.urllib.request.urlopen
        B.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        try:
            self.assertIsNone(B.tool_session_queued("fp", 1))
        finally:
            B.urllib.request.urlopen = orig
            B.TOOL_TOKEN = ""


def load_tool_client():
    spec = importlib.util.spec_from_file_location("remediation_tool", os.path.join(ROOT, "tools", "remediation_tool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class ToolClientTests(unittest.TestCase):
    """tools/remediation_tool.py: danh tính chỉ lấy từ env; token chỉ gửi trong header; tên sai bị chặn trước khi gọi mạng."""

    def run_client(self, argv, env=None, reply=None):
        import contextlib
        import io
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.update(path=self.path, auth=self.headers.get("Authorization"),
                            body=json.loads(self.rfile.read(n) or b"{}") if n else None)
                raw = json.dumps(reply or {"status": "ok"}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            do_GET = do_POST = _go

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        saved = dict(os.environ)
        os.environ.update({"REMEDIATOR_URL": f"http://127.0.0.1:{srv.server_address[1]}", "REMEDIATOR_TOOL_TOKEN": "tok-abc",
                           "AI_INCIDENT_FP": "fp-9", "AI_INCIDENT_ROUND": "2", "AI_INCIDENT_ALERT": "MySQLDown"})
        os.environ.update(env or {})
        mod = load_tool_client()
        buf, code = io.StringIO(), 0
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    mod.main(argv)
                except SystemExit as e:
                    code = e.code or 0
        finally:
            srv.shutdown()
            os.environ.clear(); os.environ.update(saved)
        return code, (json.loads(buf.getvalue()) if buf.getvalue().strip() else None), seen

    def test_read_sends_token_header_and_identity_from_env(self):
        code, res, seen = self.run_client(["read", "inspect_container", "ecommerce-db"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["path"], "/tool/read")
        self.assertEqual(seen["auth"], "Bearer tok-abc")
        self.assertEqual(seen["body"], {"fingerprint": "fp-9", "round": 2, "alert": "MySQLDown",
                                        "action_id": "inspect_container", "container": "ecommerce-db"})
        self.assertNotIn("tok-abc", json.dumps(seen["body"]))   # token không bao giờ nằm trong thân yêu cầu

    def test_propose_joins_reason_words(self):
        _, _, seen = self.run_client(["propose", "start_container", "ecommerce-db", "DB", "đã", "dừng"])
        self.assertEqual(seen["path"], "/tool/propose")
        self.assertEqual(seen["body"]["reason"], "DB đã dừng")

    def test_catalog_uses_get(self):
        _, _, seen = self.run_client(["catalog"])
        self.assertEqual(seen["path"], "/tool/catalog")

    def test_bad_names_rejected_before_any_network_call(self):
        for argv in (["read", "logs_tail", "backend"], ["read", "logs_tail", "ecommerce-x; rm -rf /"],
                     ["propose", "Restart", "ecommerce-backend", "x"], ["read", "logs_tail", "../etc"]):
            code, res, seen = self.run_client(argv)
            self.assertEqual(res["status"], "denied", argv)
            self.assertEqual(seen, {}, argv)                      # không có yêu cầu nào tới remediator

    def test_missing_config_fails_closed(self):
        code, res, seen = self.run_client(["catalog"], env={"REMEDIATOR_TOOL_TOKEN": ""})
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "error")
        self.assertEqual(seen, {})

    def test_unknown_command_usage(self):
        code, res, _ = self.run_client(["rm", "-rf"])
        self.assertEqual(code, 2)


class HolmesToolsetConfigTests(unittest.TestCase):
    """monitoring/holmes/config.yaml khai báo công cụ: phải khớp policy.json và đường dẫn trong Dockerfile."""

    @classmethod
    def setUpClass(cls):
        mon = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "monitoring", "holmes", "config.yaml")
        with open(mon, encoding="utf-8") as f:
            text = f.read()
        cls.section = text[text.index("remediation/tools:"):]
        with open(os.path.join(REMEDIATOR_DIR, "policy.json"), encoding="utf-8") as f:
            cls.policy = json.load(f)
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
            cls.dockerfile = f.read()

    def enums(self, tool):
        block = self.section[self.section.index(f'- name: "{tool}"'):]
        nxt = block.find("- name:", 5)
        block = block[:nxt] if nxt > 0 else block
        m = re.search(r"enum: \[(.*?)\]", block)
        return [x.strip().strip('"') for x in m.group(1).split(",")] if m else []

    def test_read_status_enum_only_contains_read_only_actions(self):
        for a in self.enums("read_status"):
            self.assertIn(a, self.policy["actions"], a)
            self.assertEqual(self.policy["actions"][a].get("approval", "required"), "none", a)
            self.assertFalse(self.policy["actions"][a].get("mutating"), a)

    def test_propose_fix_enum_only_contains_approval_required_actions(self):
        enum = self.enums("propose_fix")
        self.assertTrue(enum)
        for a in enum:
            self.assertIn(a, self.policy["actions"], a)
            self.assertEqual(self.policy["actions"][a].get("approval", "required"), "required", a)

    def test_no_shell_or_docker_exposed_to_ai(self):
        for forbidden in ("docker ", "bash", "sh -c", "exec "):
            for line in self.section.splitlines():
                if line.strip().startswith("command:"):
                    self.assertNotIn(forbidden, line.replace("test -f", ""), line)

    def test_every_command_runs_only_the_fixed_client_script(self):
        cmds = [l.split("command:", 1)[1].strip().strip('"') for l in self.section.splitlines() if l.strip().startswith("command:")]
        tool_cmds = [c for c in cmds if "remediation_tool.py" in c]
        self.assertEqual(len(tool_cmds), 4)         # catalog, overview, read, propose
        for c in tool_cmds:
            self.assertTrue(c.startswith("python /app/tools/remediation_tool.py "), c)

    def test_bridge_is_built_on_the_same_holmes_tag_the_build_script_uses(self):
        """Lỗi thật đã gặp: Dockerfile ghi cứng :0.45.0 nên bridge dựng trên image holmes CŨ (không có toolset) -> AI nói 'công cụ không khả dụng'."""
        m = re.search(r"ARG HOLMES_IMAGE=(\S+)", self.dockerfile)
        self.assertIsNotNone(m)
        repo = os.path.dirname(os.path.dirname(ROOT))
        with open(os.path.join(repo, "scripts", "push-images.ps1"), encoding="utf-8") as f:
            script = f.read()
        tag = re.search(r'\[string\]\$Tag = "([^"]+)"', script).group(1)
        self.assertEqual(m.group(1), f"thuanperry/ecommerce-holmes:{tag}")

    def test_script_path_matches_dockerfile_and_toolset_self_disables_without_it(self):
        self.assertIn("COPY tools /app/tools", self.dockerfile)
        self.assertIn('test -f /app/tools/remediation_tool.py', self.section)
        self.assertIn("REMEDIATOR_TOOL_TOKEN", self.section)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tools", "remediation_tool.py")))


class ChatTests(unittest.TestCase):
    """Đường B của câu nói tự nhiên: remediator -> bridge /chat (có token) -> phiên AI nhỏ dùng công cụ."""

    def setUp(self):
        self._saved = (B.USE_TOOLS, B.TOOL_TOKEN)
        B.USE_TOOLS, B.TOOL_TOKEN = True, "tok-chat"
        B.chat_runs.clear()
        while not B.chat_jobs.empty():
            B.chat_jobs.get_nowait()
        self.addCleanup(lambda: (setattr(B, "USE_TOOLS", self._saved[0]), setattr(B, "TOOL_TOKEN", self._saved[1])))

    def test_handle_chat_queues_job_with_clean_text(self):
        code, _ = B.handle_chat({"text": "  chạy   cho tôi container db  ", "uid": "999"})
        self.assertEqual(code, 200)
        fp, payload = B.chat_jobs.get_nowait()
        self.assertTrue(fp.startswith("chat-"))
        self.assertEqual(payload["text"], "chạy cho tôi container db")

    def test_chat_off_without_tools_and_rejects_empty(self):
        B.USE_TOOLS = False
        self.assertEqual(B.handle_chat({"text": "xin chào"})[0], 503)
        B.USE_TOOLS = True
        self.assertEqual(B.handle_chat({"text": " "})[0], 400)
        self.assertEqual(B.chat_jobs.qsize(), 0)

    def test_chat_has_its_own_hourly_limit(self):
        for _ in range(B.CHAT_MAX_PER_HOUR):
            B.chat_runs.append(__import__("time").time())
        self.assertEqual(B.handle_chat({"text": "vì sao backend chậm?"})[0], 429)
        self.assertEqual(B.chat_jobs.qsize(), 0)

    def test_chat_prompt_contains_request_tools_and_refusal_rule(self):
        p = B.build_chat_prompt("chạy cho tôi container db")
        self.assertIn("chạy cho tôi container db", p)
        self.assertIn("propose_fix", p)
        self.assertIn("ngoài kho lệnh", p)
        self.assertLessEqual(len(B.build_chat_prompt("x" * 5000)) - len(B.build_chat_prompt("x" * 500)), 0)   # câu dài bị cắt ở 500 ký tự, không phình prompt

    def test_http_chat_requires_token(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), B.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/chat"

        def post(token=None):
            req = urllib.request.Request(url, data=json.dumps({"text": "xem log nginx"}).encode(),
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            self.assertEqual(post(), 401)
            self.assertEqual(post("sai"), 401)
            self.assertEqual(B.chat_jobs.qsize(), 0)             # không có token thì không bao giờ gọi AI
            self.assertEqual(post("tok-chat"), 200)
            self.assertEqual(B.chat_jobs.qsize(), 1)
            B.TOOL_TOKEN = ""
            self.assertEqual(post("tok-chat"), 401)               # token trống = đóng hẳn
        finally:
            srv.shutdown()


class KnowledgeTests(unittest.TestCase):
    """knowledge/system.md: AI đọc để hiểu hệ thống. Nội dung phải khớp thực tế (policy, compose, prometheus.yml) để không dạy sai."""

    @classmethod
    def setUpClass(cls):
        repo = os.path.dirname(os.path.dirname(ROOT))
        with open(os.path.join(ROOT, "knowledge", "system.md"), encoding="utf-8") as f:
            cls.text = f.read()
        with open(os.path.join(REMEDIATOR_DIR, "policy.json"), encoding="utf-8") as f:
            cls.policy = json.load(f)
        with open(os.path.join(repo, "monitoring", "prometheus", "prometheus.yml"), encoding="utf-8") as f:
            cls.prom = f.read()
        containers = set()
        for compose in (os.path.join(repo, "webapp", "docker-compose.app.yml"), os.path.join(repo, "monitoring", "docker-compose.monitoring.yml")):
            with open(compose, encoding="utf-8") as f:
                containers |= set(re.findall(r"container_name:\s*(\S+)", f.read()))
        cls.containers = containers

    def test_fits_in_prompt_budget_without_truncation(self):
        self.assertLessEqual(len(self.text), B.KNOWLEDGE_MAX_CHARS)

    def test_every_container_mentioned_really_exists(self):
        for name in set(re.findall(r"ecommerce-[a-z][a-z-]*[a-z]", self.text)) - {"ecommerce-shared"}:   # ecommerce-shared là tên mạng, không phải container
            self.assertIn(name, self.containers, f"system.md nhắc container không có trong compose: {name}")

    def test_every_prometheus_job_mentioned_exists(self):
        jobs = set(re.findall(r"job_name:\s*(\S+)", self.prom))
        for job in re.findall(r"^\|[^|]*\|\s*`([a-z]+)`[^|]*\(?", self.text, re.M):
            if job in ("backend", "nginx", "mysql", "node", "prometheus", "alertmanager"):
                self.assertIn(job, jobs, job)

    def test_every_action_mentioned_exists_in_policy(self):
        for action in set(re.findall(r"`((?:restart|start|inspect|logs|container)_[a-z]+)`", self.text)):
            self.assertIn(action, self.policy["actions"], action)

    def test_db_rules_in_text_match_policy(self):
        acts = self.policy["actions"]
        self.assertIn("ecommerce-db", acts["start_container"].get("allow_on_forbidden", []))
        self.assertIn("ecommerce-db", acts["inspect_container"].get("allow_on_forbidden", []))
        self.assertNotIn("ecommerce-db", acts["restart_container"].get("allow_on_forbidden", []))
        self.assertNotIn("ecommerce-db", acts["logs_tail"].get("allow_on_forbidden", []))
        for c in ("ecommerce-holmes-bridge", "ecommerce-remediator", "ecommerce-docker-proxy"):
            self.assertIn(c, self.policy["containers_forbidden"])
            self.assertIn(c, self.text)

    def test_knowledge_is_injected_into_every_prompt_kind(self):
        B.USE_TOOLS = True
        try:
            for prompt in (B.build_prompt(ALERT), B.build_followup_prompt(ALERT, {"history": []}, 2), B.build_chat_prompt("xem log nginx")):
                self.assertIn("Chuỗi phụ thuộc", prompt)
        finally:
            B.USE_TOOLS = False

    def test_missing_file_does_not_break_prompts(self):
        orig = B.KNOWLEDGE_FILE
        B.KNOWLEDGE_FILE = "/khong/co/file.md"
        try:
            self.assertEqual(B.knowledge_text(), "")
            self.assertIn("Alertmanager vừa báo", B.build_prompt(ALERT))
        finally:
            B.KNOWLEDGE_FILE = orig


class ChatSessionTests(unittest.TestCase):
    """Phiên /ai: trí nhớ hội thoại theo sid, ngữ cảnh hiện tại, ngân sách chat riêng, luồng riêng."""

    def setUp(self):
        self._saved = (B.USE_TOOLS, B.TOOL_TOKEN, B.run_holmes, B.tg_send, B.chat_context, B.tool_session_queued, B.has_bedrock_creds)
        B.USE_TOOLS, B.TOOL_TOKEN = True, "tok-chat"
        B.chat_runs.clear(); B.chat_sessions.clear(); B.recent_runs.clear()
        while not B.chat_jobs.empty():
            B.chat_jobs.get_nowait()
        self.sent, self.prompts = [], []
        B.has_bedrock_creds = lambda: True
        self._llm = B.llm_stream
        B.llm_stream = lambda msgs: iter([B.OPS_MARKER])              # các test này kiểm tra đường Holmes: mô hình luôn báo cần công cụ
        self.addCleanup(lambda: setattr(B, "llm_stream", self._llm))
        B.tg_send = lambda text, reply_to=None: self.sent.append(text) or 1
        B.chat_context = lambda: "- Cảnh báo đang bật: MySQLDown\n- Đề xuất đang chờ người duyệt: không có"
        B.tool_session_queued = lambda fp, rnd: 0
        self.answer = "Đã đọc trạng thái, backend đang chạy."

        def fake_holmes(a, prompt=None, fp="", rnd=1, max_steps=None):
            self.prompts.append((prompt, fp, max_steps))
            return self.answer, None
        B.run_holmes = fake_holmes
        self.addCleanup(lambda: (setattr(B, "USE_TOOLS", self._saved[0]), setattr(B, "TOOL_TOKEN", self._saved[1]),
                                 setattr(B, "run_holmes", self._saved[2]), setattr(B, "tg_send", self._saved[3]),
                                 setattr(B, "chat_context", self._saved[4]), setattr(B, "tool_session_queued", self._saved[5]),
                                 setattr(B, "has_bedrock_creds", self._saved[6])))

    def run_one(self, text, sid="abc123"):
        self.assertEqual(B.handle_chat({"text": text, "uid": "9", "sid": sid})[0], 200)
        fp, payload = B.chat_jobs.get_nowait()
        B.process_chat(fp, payload)
        return fp

    def test_each_message_gets_unique_incident_identity_in_a_session(self):
        fps = {self.run_one("tôi muốn chạy lại backend"), self.run_one("ok"), self.run_one("db thì sao")}
        self.assertEqual(len(fps), 3)                              # 1 đề xuất cho MỖI tin, không bị chặn bởi tin trước
        self.assertTrue(all(f.startswith("chat-abc123-") for f in fps))

    def test_second_message_sees_the_first_exchange(self):
        self.run_one("tôi muốn chạy lại container của backend")
        self.run_one("cái đó xong chưa?")
        second_prompt = self.prompts[1][0]
        self.assertIn("Hội thoại gần đây", second_prompt)
        self.assertIn("tôi muốn chạy lại container của backend", second_prompt)
        self.assertIn("backend đang chạy", second_prompt)
        self.assertNotIn("Hội thoại gần đây", self.prompts[0][0])   # tin đầu chưa có lịch sử

    def test_current_state_is_injected_only_in_sessions(self):
        self.run_one("db sao rồi?")
        self.assertIn("MySQLDown", self.prompts[0][0])
        B.handle_chat({"text": "xem log nginx", "uid": "9"})        # không có sid = câu hỏi lẻ, không nạp ngữ cảnh/trí nhớ
        fp, payload = B.chat_jobs.get_nowait()
        B.process_chat(fp, payload)
        self.assertNotIn("Tình trạng hiện tại", self.prompts[1][0])

    def test_sessions_are_isolated_and_ended_sessions_forget(self):
        self.run_one("bí mật của phiên A", sid="aaa111")
        self.run_one("hello", sid="bbb222")
        self.assertNotIn("bí mật của phiên A", self.prompts[1][0])
        B.end_chat_session({"sid": "aaa111"})
        self.run_one("còn nhớ không?", sid="aaa111")
        self.assertNotIn("bí mật của phiên A", self.prompts[2][0])

    def test_history_is_capped(self):
        for i in range(B.CHAT_HISTORY_TURNS + 5):
            self.run_one(f"tin số {i}")
        last = self.prompts[-1][0]
        self.assertNotIn("tin số 0\n", last)
        self.assertIn(f"tin số {B.CHAT_HISTORY_TURNS + 3}", last)

    def test_pending_proposal_is_noted_in_memory_and_reply(self):
        B.tool_session_queued = lambda fp, rnd: 1
        self.run_one("chạy lại backend")
        self.assertIn("kèm nút Duyệt", self.sent[-1])
        self.run_one("ok làm đi")
        self.assertIn("đã đề xuất lệnh chờ duyệt", self.prompts[1][0])

    def test_chat_budget_is_separate_from_incident_analysis_budget(self):
        for _ in range(B.MAX_PER_HOUR):
            B.recent_runs.append(__import__("time").time())        # đã hết lượt phân tích sự cố
        self.assertEqual(B.handle_chat({"text": "xem log nginx", "sid": "abc123"})[0], 200)   # chat vẫn chạy
        B.chat_runs.clear()
        for _ in range(B.CHAT_MAX_PER_HOUR):
            B.chat_runs.append(__import__("time").time())
        self.assertEqual(B.handle_chat({"text": "xem log nginx", "sid": "abc123"})[0], 429)
        self.assertEqual(B.rate_ok(), False)                       # ngân sách sự cố không bị chat làm đầy hay giải phóng

    def test_chat_uses_its_own_queue_and_step_limit(self):
        self.run_one("vì sao chậm?")
        self.assertEqual(B.jobs.qsize(), 0)                        # không đi qua hàng đợi phân tích alert
        self.assertEqual(self.prompts[0][2], B.CHAT_MAX_STEPS)

    def test_prompt_tells_ai_it_cannot_approve_and_how_to_pick_start_or_restart(self):
        p = B.build_chat_prompt("ok làm đi", [{"u": "chạy backend", "a": "đã đề xuất"}], "- Cảnh báo đang bật: không có")
        self.assertIn("KHÔNG duyệt thay được", p)
        self.assertIn("start_container", p)
        self.assertIn("restart_container", p)
        self.assertIn("system_overview", p)

    def test_http_chat_end_requires_token(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        B.chat_sessions["abc123"] = {"hist": deque(maxlen=4), "n": 1, "ts": __import__("time").time()}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), B.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/chat/end"

        def post(token=None):
            req = urllib.request.Request(url, data=json.dumps({"sid": "abc123"}).encode(),
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            self.assertEqual(post(), 401)
            self.assertIn("abc123", B.chat_sessions)               # không token thì không xoá được trí nhớ của ai
            self.assertEqual(post("tok-chat"), 200)
            self.assertNotIn("abc123", B.chat_sessions)
        finally:
            srv.shutdown()


class DirectChatTests(unittest.TestCase):
    """Chat 2 lớp: nói chuyện/kiến thức -> gọi thẳng mô hình đã cấu hình (nhanh, chữ hiện dần, KHÔNG công cụ);
    cần dữ liệu thật hoặc lệnh -> Holmes. Mô hình tự báo [[OPS]] khi cần nâng cấp."""

    def setUp(self):
        self._saved = {k: getattr(B, k) for k in ("USE_TOOLS", "CHAT_DIRECT", "TOOL_TOKEN", "run_holmes", "tg_send", "tg_edit", "llm_stream",
                                                  "chat_context", "tool_session_queued", "has_bedrock_creds", "CHAT_STREAM_EDIT_INTERVAL")}
        B.USE_TOOLS, B.CHAT_DIRECT, B.TOOL_TOKEN = True, True, "tok-chat"
        B.chat_sessions.clear()
        self.sent, self.edits, self.holmes, self.llm_calls = [], [], [], []
        B.has_bedrock_creds = lambda: True
        B.tg_send = lambda text, reply_to=None: (self.sent.append(text) or len(self.sent))
        B.tg_edit = lambda mid, text: (self.edits.append((mid, text)) or True)
        B.chat_context = lambda: ""
        B.tool_session_queued = lambda fp, rnd: 0
        B.CHAT_STREAM_EDIT_INTERVAL = 0.0
        self.chunks = ["Tôi là ", "trợ lý vận hành. ", "Tôi giúp xem trạng thái."]

        def fake_llm(messages):
            self.llm_calls.append(messages)
            yield from self.chunks
        B.llm_stream = fake_llm

        def fake_holmes(a, prompt=None, fp="", rnd=1, max_steps=None):
            self.holmes.append(prompt)
            return "Holmes trả lời có số liệu thật.", None
        B.run_holmes = fake_holmes
        self.addCleanup(lambda: [setattr(B, k, v) for k, v in self._saved.items()])

    def chat(self, text, sid="abc123"):
        B.chat_sessions.setdefault(sid, {"hist": deque(maxlen=8), "n": 0, "ts": time.time()})
        B.process_chat("chat-x", {"text": text, "sid": sid})

    # ---- định tuyến
    def test_routing_table(self):
        cases = {
            "bạn làm được những gì?": "chat", "đề xuất lệnh hoạt động thế nào?": "chat", "restart policy của docker là gì?": "chat",
            "giải thích prometheus giúp tôi": "chat", "nginx là gì": "chat", "mysql dùng để làm gì": "chat", "xin chào": "chat", "cảm ơn nhé": "chat", "chạy như thế nào?": "chat",
            "container backend sao rồi": "ops", "nginx sao rồi": "ops", "prometheus bị treo": "ops", "vì sao ecommerce-nginx chậm": "ops", "db sao rồi": "ops", "trạng thái hệ thống": "ops",
            "xem log gần nhất": "ops", "MySQLDown là gì": "ops", "cpu máy đang bao nhiêu": "ops", "grafana bị treo": "ops",
        }
        for text, expect in cases.items():
            self.assertEqual(B.classify_route(text), expect, text)

    def test_short_follow_up_of_ops_stays_ops_but_chitchat_does_not(self):
        self.assertEqual(B.classify_route("ok làm đi", "ops"), "ops")
        self.assertEqual(B.classify_route("cái đó thì sao", "ops"), "ops")
        self.assertEqual(B.classify_route("ok làm đi", "chat"), "chat")
        self.assertEqual(B.classify_route("cảm ơn", "ops"), "chat")
        self.assertEqual(B.classify_route("bạn có thể giải thích chi tiết cách hệ thống sao lưu dữ liệu hằng ngày không", "ops"), "chat")

    def test_direct_chat_disabled_routes_everything_to_holmes(self):
        B.CHAT_DIRECT = False
        self.assertEqual(B.classify_route("bạn làm được gì?"), "ops")

    # ---- đường chat trực tiếp
    def final_text(self):
        return self.edits[-1][1] if self.edits else self.sent[-1]

    def test_conversational_question_is_answered_directly_without_holmes(self):
        self.chat("bạn làm được những gì?")
        self.assertEqual(self.holmes, [])                          # không khởi động Holmes
        self.assertEqual(len(self.llm_calls), 1)
        self.assertIn("trợ lý vận hành", self.final_text())
        self.assertIn("AI trả lời", self.sent[0])

    def test_text_streams_into_one_message_via_edits(self):
        self.chat("bạn là ai?")
        self.assertEqual(len(self.sent), 1)                        # 1 tin duy nhất, sau đó sửa dần
        self.assertGreaterEqual(len(self.edits), 1)
        self.assertTrue(all(mid == 1 for mid, _ in self.edits))
        self.assertIn("Tôi giúp xem trạng thái", self.edits[-1][1])   # bản cuối đầy đủ

    def test_edits_are_throttled(self):
        B.CHAT_STREAM_EDIT_INTERVAL = 5.0
        self.chunks = [f"đoạn{i} " for i in range(30)]
        self.chat("bạn là ai?")
        self.assertLessEqual(len(self.edits), 1)                   # chống vượt giới hạn sửa tin của Telegram (chỉ còn bản cuối)

    def test_llm_gets_no_tools_and_has_rules_capabilities_and_knowledge(self):
        self.chat("bạn làm được gì?")
        msgs = self.llm_calls[0]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        system = msgs[0]["content"]
        self.assertIn(B.OPS_MARKER, system)                        # luật: cần dữ liệu thật thì trả [[OPS]]
        self.assertIn("KHÔNG có số liệu trực tiếp", system)
        self.assertIn("/status", system)                           # mô tả khả năng của bot
        self.assertIn("Chuỗi phụ thuộc", system)                   # bản đồ hệ thống để giải thích cấu trúc
        self.assertNotIn("tok-chat", system)                       # không rò token
        self.assertEqual(len(msgs), 2)

    def test_real_litellm_call_passes_no_tools_parameter(self):
        import types
        seen = {}
        fake = types.ModuleType("litellm")

        def completion(**kw):
            seen.update(kw)
            return iter([])
        fake.completion = completion
        sys.modules["litellm"] = fake
        os.environ["MODEL"] = "bedrock/test-model"
        B.llm_stream = self._saved["llm_stream"]
        try:
            list(B.llm_stream([{"role": "user", "content": "hi"}]))
        finally:
            del sys.modules["litellm"]
        self.assertEqual(seen["model"], "bedrock/test-model")      # cùng mô hình đã cấu hình, không có mô hình phụ
        self.assertTrue(seen["stream"])
        self.assertNotIn("tools", seen)
        self.assertNotIn("tool_choice", seen)
        self.assertLessEqual(seen["max_tokens"], 1500)

    # ---- nâng cấp sang Holmes
    def test_ops_marker_escalates_to_holmes_and_is_never_shown(self):
        self.chunks = ["[[OPS]]"]
        self.chat("cho tôi biết có chuyện gì bất thường không")
        self.assertEqual(len(self.holmes), 1)
        self.assertTrue(all("[[OPS]]" not in t for t in self.sent))
        self.assertIn("Holmes trả lời", self.sent[-1])

    def test_marker_split_across_chunks_and_leading_whitespace(self):
        for chunks in (["[[", "OPS", "]]"], ["  [[O", "PS]]"], ["\n[[OPS]]", " thêm chữ"]):
            self.holmes.clear(); self.sent.clear()
            self.chunks = chunks
            self.chat("cho tôi biết có chuyện gì bất thường không")
            self.assertEqual(len(self.holmes), 1, chunks)
            self.assertEqual(len(self.sent), 1, chunks)             # chỉ có tin trả lời của Holmes, không lộ dấu hiệu

    def test_text_that_merely_starts_with_brackets_is_not_marker(self):
        self.chunks = ["[[ghi chú]] ", "đây là câu trả lời bình thường"]
        self.chat("bạn là ai?")
        self.assertEqual(self.holmes, [])
        self.assertIn("câu trả lời bình thường", self.edits[-1][1] if self.edits else self.sent[-1])

    def test_llm_failure_falls_back_to_holmes(self):
        def boom(messages):
            raise RuntimeError("bedrock down")
            yield
        B.llm_stream = boom
        self.chat("bạn là ai?")
        self.assertEqual(len(self.holmes), 1)
        self.assertIn("Holmes trả lời", self.sent[-1])

    def test_empty_llm_reply_falls_back_to_holmes(self):
        self.chunks = ["   "]
        self.chat("bạn là ai?")
        self.assertEqual(len(self.holmes), 1)

    # ---- trí nhớ dùng chung cho hai đường
    def test_memory_is_shared_between_direct_chat_and_holmes(self):
        self.chat("bạn làm được những gì?")                         # đường chat
        self.chat("container backend sao rồi")                      # đường Holmes
        self.assertIn("bạn làm được những gì?", self.holmes[0])     # Holmes thấy câu chat trước đó
        self.assertIn("trợ lý vận hành", self.holmes[0])
        self.chat("giải thích thêm giúp tôi")                        # quay lại chat (không phải câu nối tiếp việc vận hành)
        users = [m["content"] for m in self.llm_calls[-1] if m["role"] == "user"]
        assistants = [m["content"] for m in self.llm_calls[-1] if m["role"] == "assistant"]
        self.assertIn("container backend sao rồi", users)           # chat thấy câu đã hỏi Holmes
        self.assertTrue(any("Holmes trả lời" in a for a in assistants))

    def test_follow_up_after_ops_goes_to_holmes_with_history(self):
        self.chat("container backend sao rồi")
        self.chat("ok làm đi")
        self.assertEqual(len(self.holmes), 2)
        self.assertEqual(len(self.llm_calls), 0)

    def test_no_session_means_no_memory_but_still_routes(self):
        B.process_chat("chat-y", {"text": "bạn là ai?", "sid": ""})
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(len(self.llm_calls[0]), 2)
        self.assertEqual(len(B.chat_sessions), 0)

    # ---- hạ tầng
    def test_plug_in_module_is_used_for_testing(self):
        import types
        mod = types.ModuleType("fake_llm_mod")
        mod.complete = lambda messages: iter(["từ module giả"])
        sys.modules["fake_llm_mod"] = mod
        B.CHAT_LLM_MODULE = "fake_llm_mod"
        B.llm_stream = self._saved["llm_stream"]
        try:
            self.assertEqual("".join(B.llm_stream([])), "từ module giả")
        finally:
            B.CHAT_LLM_MODULE = ""
            del sys.modules["fake_llm_mod"]

    def test_aws_env_is_cleaned_like_the_holmes_entrypoint(self):
        saved = {k: os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_BEARER_TOKEN_BEDROCK", "AWS_SESSION_TOKEN")}
        os.environ.update({"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": " ' ", "AWS_SESSION_TOKEN": "",
                           "AWS_BEARER_TOKEN_BEDROCK": ' "ABSKxyz"\r'})
        try:
            B._clean_aws_env()
            self.assertNotIn("AWS_ACCESS_KEY_ID", os.environ)       # biến trống bị bỏ để không chọn nhầm cách đăng nhập
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", os.environ)
            self.assertNotIn("AWS_SESSION_TOKEN", os.environ)
            self.assertEqual(os.environ["AWS_BEARER_TOKEN_BEDROCK"], "ABSKxyz")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_capabilities_text_matches_real_commands(self):
        for cmd in ("/status", "/logs", "/restart", "/start", "/stop", "/ai", "/help"):
            self.assertIn(cmd, B.CHAT_CAPABILITIES)
        self.assertIn("không được restart", B.CHAT_CAPABILITIES)   # ngoại lệ DB nói đúng với policy


class BridgeSecurityTests(unittest.TestCase):
    """/alert và /continue cần token khi đã đặt token; bridge gọi remediator kèm token; toolset nguy hiểm bị tắt."""

    def setUp(self):
        self._saved = (B.TOOL_TOKEN, B.REMEDIATOR_URL)
        self.addCleanup(lambda: (setattr(B, "TOOL_TOKEN", self._saved[0]), setattr(B, "REMEDIATOR_URL", self._saved[1])))
        B.state.clear()
        while not B.jobs.empty():
            B.jobs.get_nowait()

    def serve(self):
        import threading
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), B.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, url, body, token=None):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=h), timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_alert_and_continue_require_token_when_set(self):
        B.TOOL_TOKEN = "T" * 32
        base = self.serve()
        alert = {"alerts": [dict(ALERT, labels={"alertname": "TargetDown", "severity": "info"})]}
        self.assertEqual(self.post(base + "/alert", alert), 401)
        self.assertEqual(self.post(base + "/alert", alert, "sai"), 401)
        self.assertEqual(self.post(base + "/continue", {"fingerprint": "x", "round": 1}), 401)
        self.assertEqual(self.post(base + "/alert", alert, "T" * 32), 200)

    def test_legacy_mode_without_token_accepts_alerts(self):
        B.TOOL_TOKEN = ""
        base = self.serve()
        self.assertEqual(self.post(base + "/alert", {"alerts": []}), 200)
        self.assertEqual(self.post(base + "/chat", {"text": "hi"}), 401)      # chat vẫn luôn cần token

    def test_handler_has_timeout(self):
        self.assertTrue(0 < B.Handler.timeout <= 60)

    def test_calls_to_remediator_carry_token(self):
        B.TOOL_TOKEN, B.REMEDIATOR_URL = "T" * 32, "http://remediator:8090"
        seen = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def close(s): pass
        orig = B.urllib.request.urlopen
        B.urllib.request.urlopen = lambda req, timeout=0: seen.append((req.full_url, req.headers.get("Authorization"))) or Resp()
        try:
            B.send_proposal(ALERT, {"action_id": "restart_container", "container": "ecommerce-backend"}, "VẤN ĐỀ: x")
            B.close_remediator_loop("fp1")
        finally:
            B.urllib.request.urlopen = orig
        self.assertEqual([a for _, a in seen], ["Bearer " + "T" * 32] * 2)

    def test_dangerous_builtin_toolsets_are_disabled_in_holmes_config(self):
        mon = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "monitoring", "holmes", "config.yaml")
        with open(mon, encoding="utf-8") as f:
            text = f.read()
        for name in ("internet", "connectivity_check", "bash"):
            self.assertRegex(text, rf"\n  {name}:\n    enabled: false\n", name)


if __name__ == "__main__":
    unittest.main()
