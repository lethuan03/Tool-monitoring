"""Test holmes-bridge hoàn toàn cục bộ: không mạng, không Bedrock, không Telegram thật.
Chạy:  python -m unittest discover -s ai-remediation/holmes-bridge/tests -v
"""
import importlib.util
import json
import os
import re
import sys
import unittest

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
        self.assertLess(len(prompt), 8000)


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


if __name__ == "__main__":
    unittest.main()
