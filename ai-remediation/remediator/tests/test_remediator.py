"""Test remediator hoàn toàn cục bộ: không mạng, không Docker, không Telegram thật.
Chạy:  python -m unittest discover -s ai-remediation/remediator/tests -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TMP = tempfile.mkdtemp()
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "TEST_TOKEN", "TELEGRAM_CHAT_ID": "111", "REMEDIATOR_SECRET": "test-secret",
    "AUDIT_FILE": os.path.join(TMP, "audit.jsonl"), "REMEDIATION_MODE": "dry-run",
    "REMEDIATOR_POST_CHECK_DELAY_SECONDS": "0",
})
spec = importlib.util.spec_from_file_location("remediator", os.path.join(ROOT, "remediator.py"))
R = importlib.util.module_from_spec(spec)
sys.modules["remediator"] = R
spec.loader.exec_module(R)

POLICY = R.load_policy(os.path.join(ROOT, "policy.json"))


def audit_events():
    with open(os.environ["AUDIT_FILE"], encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        R.pending.clear(); R.seen.clear(); R.history.clear(); R.loops.clear()
        R.MODE, R.ENABLED, R.APPROVERS = "dry-run", True, set()
        R.BRIDGE_URL, R.MAX_ROUNDS = "", 3
        R.tool_sessions.clear()
        R.TOOL_TOKEN = ""
        open(os.environ["AUDIT_FILE"], "w").close()
        self.sent, self.docker_calls, self.tg_calls = [], [], []
        R.tg_send = lambda text, reply_to=None, buttons=None: (self.sent.append((text, buttons)) or 42)
        R.tg_call = lambda method, body, timeout=40: (self.tg_calls.append((method, body)) or {})
        R.docker_call = lambda policy, a, c: (self.docker_calls.append((a, c)) or (204, ""))
        R.prom_down_jobs = lambda: []

    def propose(self, action="restart_container", container="ecommerce-backend", fp="f1"):
        return R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": fp, "action_id": action,
                                         "container": container, "reason": "test"})

    def click(self, kind, pid, chat="111", uid="999", sig=None):
        data = R.callback_data(kind, pid) if sig is None else f"{kind}:{pid}:{sig}"
        R.handle_callback(POLICY, {"id": "cb", "data": data, "from": {"id": uid}, "message": {"chat": {"id": chat}}})


class PolicyTests(Base):
    def test_allowed(self):
        self.assertTrue(R.check_proposal(POLICY, "restart_container", "ecommerce-backend")[0])

    def test_forbidden_container_always_denied(self):
        for c in ("ecommerce-db", "ecommerce-holmes-bridge", "ecommerce-remediator", "ecommerce-docker-proxy"):
            self.assertFalse(R.check_proposal(POLICY, "restart_container", c)[0], c)
            self.assertFalse(R.check_proposal(POLICY, "logs_tail", c)[0], c)

    def test_db_can_only_be_started_never_restarted_or_logged(self):
        self.assertTrue(R.check_proposal(POLICY, "start_container", "ecommerce-db")[0])
        self.assertFalse(R.check_proposal(POLICY, "restart_container", "ecommerce-db")[0])
        self.assertFalse(R.check_proposal(POLICY, "logs_tail", "ecommerce-db")[0])
        for c in ("ecommerce-holmes-bridge", "ecommerce-remediator", "ecommerce-docker-proxy"):
            self.assertFalse(R.check_proposal(POLICY, "start_container", c)[0], c)

    def test_exception_list_only_names_known_forbidden_containers(self):
        for name, a in POLICY["actions"].items():
            for c in a.get("allow_on_forbidden", []):
                self.assertIn(c, POLICY["containers_forbidden"], name)
                self.assertIn(name, ("start_container", "inspect_container"))  # chỉ action không phá hoại mới được ngoại lệ

    def test_unknown_container_and_action_denied(self):
        self.assertFalse(R.check_proposal(POLICY, "restart_container", "some-other")[0])
        self.assertFalse(R.check_proposal(POLICY, "rm_everything", "ecommerce-backend")[0])
        self.assertFalse(R.check_proposal(POLICY, "restart_container", "ecommerce-backend; rm -rf /")[0])
        self.assertFalse(R.check_proposal(POLICY, "restart_container", "../etc")[0])
        self.assertFalse(R.check_proposal(POLICY, None, None)[0])

    def test_policy_has_no_forbidden_overlap(self):
        self.assertFalse(set(POLICY["containers_allowed"]) & set(POLICY["containers_forbidden"]))

    def test_policy_has_no_destructive_docker_paths(self):
        for name, a in POLICY["actions"].items():
            path = a["docker"]["path"]
            self.assertTrue(path.startswith("/containers/{container}/"), name)
            self.assertNotIn("exec", path); self.assertNotIn("create", path); self.assertNotIn("kill", path)
            self.assertNotEqual(a["docker"]["method"], "DELETE", name)

    def test_rate_limit_and_cooldown(self):
        now = 1000.0
        self.assertTrue(R.check_rate(POLICY, "restart_container", "ecommerce-backend", now)[0])
        R.record_run("restart_container", "ecommerce-backend", now)
        self.assertFalse(R.check_rate(POLICY, "restart_container", "ecommerce-backend", now + 10)[0])   # cooldown 300s
        self.assertTrue(R.check_rate(POLICY, "restart_container", "ecommerce-backend", now + 400)[0])
        R.record_run("restart_container", "ecommerce-backend", now + 400)
        R.record_run("restart_container", "ecommerce-backend", now + 800)
        self.assertFalse(R.check_rate(POLICY, "restart_container", "ecommerce-backend", now + 1200)[0])  # 3/giờ
        self.assertTrue(R.check_rate(POLICY, "restart_container", "ecommerce-backend", now + 4000)[0])   # qua 1 giờ


class SigningTests(Base):
    def test_signature(self):
        self.assertEqual(R.parse_callback(R.callback_data("a", "abc")), ("a", "abc"))
        self.assertIsNone(R.parse_callback("a:abc:0000000000000000"))
        self.assertIsNone(R.parse_callback("x:abc:" + R.sign("abc")))
        self.assertIsNone(R.parse_callback("garbage"))
        self.assertLessEqual(len(R.callback_data("a", "0123456789ab")), 64)


class FlowTests(Base):
    def pid(self):
        return next(iter(R.pending))

    def test_propose_sends_buttons_and_audits(self):
        self.assertEqual(self.propose()[0], 200)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([b["text"][:1] for b in self.sent[0][1]], ["✅", "❌", "⏹"])
        self.assertEqual(audit_events()[-1]["event"], "proposed")

    def test_denied_by_policy_never_asks_approval(self):
        self.assertEqual(self.propose(container="ecommerce-db")[0], 403)
        self.assertFalse(any(p["container"] == "ecommerce-db" and p["action_id"] != "start_container" for p in R.pending.values()))
        self.assertIn("denied", [e["event"] for e in audit_events()])

    def test_kill_switch(self):
        R.ENABLED = False
        self.assertEqual(self.propose()[0], 403)
        self.assertEqual(len(R.pending), 0)

    def test_duplicate_proposal_ignored(self):
        self.propose(); code, msg = self.propose()
        self.assertEqual(msg, "duplicate")
        self.assertEqual(len(R.pending), 1)

    def test_approve_in_dry_run_does_not_call_docker(self):
        self.propose(); pid = self.pid()
        self.click("a", pid)
        time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        ev = [e["event"] for e in audit_events()]
        self.assertIn("approved_dry_run", ev)
        self.assertFalse(any(e["event"] == "executed" for e in audit_events()))

    def test_approve_in_execute_mode_calls_docker_once(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        self.click("a", pid); self.click("a", pid)  # bấm 2 lần: chỉ chạy 1 lần
        time.sleep(0.3)
        self.assertEqual(self.docker_calls, [("restart_container", "ecommerce-backend")])
        e = [x for x in audit_events() if x["event"] == "executed"][0]
        self.assertEqual(e["approver"], "999")
        self.assertEqual(e["command"], "POST /containers/ecommerce-backend/restart")

    def test_reject_does_not_run(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        self.click("r", pid)
        time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(audit_events()[-1]["event"], "rejected")

    def test_wrong_chat_or_approver_rejected(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        self.click("a", pid, chat="222")
        R.APPROVERS = {"1"}
        self.click("a", pid, uid="999")
        time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        self.assertIn(pid, R.pending)  # vẫn chờ duyệt đúng người
        self.assertEqual([e["event"] for e in audit_events()].count("unauthorized_click"), 2)

    def test_forged_signature_rejected(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        self.click("a", pid, sig="deadbeefdeadbeef")
        time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(audit_events()[-1]["event"], "invalid_click")

    def test_expired_not_run(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        R.pending[pid]["expires"] = time.time() - 1
        self.click("a", pid)
        time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(audit_events()[-1]["event"], "expired")

    def test_rate_limit_blocks_second_run(self):
        R.MODE = "execute"
        self.propose(fp="a"); self.click("a", self.pid()); time.sleep(0.3)
        code, _ = self.propose(fp="b")  # cooldown 300s
        self.assertEqual(code, 429)
        self.assertEqual(len(self.docker_calls), 1)

    def test_audit_has_no_secrets(self):
        self.propose(); self.click("a", self.pid()); time.sleep(0.2)
        with open(os.environ["AUDIT_FILE"], encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("TEST_TOKEN", raw)
        self.assertNotIn("test-secret", raw)


class AlternativesTests(Base):
    def test_every_alternative_in_policy_passes_policy(self):
        """Bảng gợi ý không được chứa thứ policy sẽ chặn."""
        for key, items in POLICY["alternatives"].items():
            for it in items:
                c = it["container"].replace("{container}", "ecommerce-backend")
                self.assertTrue(R.check_proposal(POLICY, it["action_id"], c)[0], f"{key}: {it}")

    def test_denied_db_offers_alternatives_with_buttons(self):
        self.assertEqual(self.propose(container="ecommerce-db")[0], 403)
        text, buttons = self.sent[-1]
        self.assertIn("Policy chặn", text)
        self.assertIn("Lệnh thay thế", text)
        rows = buttons if isinstance(buttons[0], list) else [buttons]
        self.assertGreaterEqual(len(rows), 2)               # >=1 gợi ý + hàng Dừng
        self.assertEqual(rows[-1][0]["text"], "⏹ Dừng")
        self.assertTrue(all(p["container"] != "ecommerce-db" or p["action_id"] == "start_container" for p in R.pending.values()))
        self.assertTrue(any(p["action_id"] == "start_container" and p["container"] == "ecommerce-db" for p in R.pending.values()))
        self.assertEqual(audit_events()[-1]["event"], "alternatives_offered")

    def test_alternative_is_approved_like_a_normal_proposal_and_siblings_dropped(self):
        R.MODE = "execute"
        self.propose(container="ecommerce-db")
        self.assertGreaterEqual(len(R.pending), 2)
        first = next(p for p in R.pending.values() if p["action_id"] == "logs_tail")
        self.click("a", first["id"]); time.sleep(0.3)
        self.assertEqual(self.docker_calls, [("logs_tail", "ecommerce-backend")])
        self.assertEqual(len(R.pending), 0)                  # gợi ý còn lại bị huỷ

    def test_forged_alternative_click_rejected(self):
        R.MODE = "execute"
        self.propose(container="ecommerce-db")
        self.click("a", next(iter(R.pending)), sig="deadbeefdeadbeef"); time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])

    def test_unparseable_ai_line_reported_with_alternatives(self):
        code, _ = R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": "x", "action_id": "khong_hop_le",
                                           "container": "", "raw": "docker exec -it ecommerce-db sh", "reason": ""})
        self.assertEqual(code, 403)
        self.assertIn("docker exec", self.sent[-1][0])

    def test_no_alternative_when_nothing_safe(self):
        saved = POLICY["alternatives"]
        try:
            POLICY["alternatives"] = {}
            self.assertEqual(self.propose(container="ecommerce-db")[0], 403)
            self.assertEqual(len(R.pending), 0)
            self.assertIn("xử lý tay", self.sent[-1][0])
        finally:
            POLICY["alternatives"] = saved


class LoopTests(Base):
    """Vòng lặp AI <-> người duyệt: remediator -> /continue của bridge (giả), nút Dừng, /stop, /close."""

    def setUp(self):
        super().setUp()
        R.BRIDGE_URL = "http://bridge:8080"
        self.continues = []
        outer = self

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False

        def fake_urlopen(req, timeout=0):
            outer.continues.append(json.loads(req.data))
            return Resp()
        self._orig_urlopen = R.urllib.request.urlopen
        R.urllib.request.urlopen = fake_urlopen

    def tearDown(self):
        R.urllib.request.urlopen = self._orig_urlopen

    def pid(self):
        return next(p["id"] for p in R.pending.values() if p["state"] == "proposed")

    def run_round(self, rnd=1, container="ecommerce-backend", action="restart_container", fp="f1", down=("backend",)):
        R.prom_down_jobs = lambda: list(down)
        R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": fp, "action_id": action,
                                  "container": container, "reason": "t", "round": rnd})
        self.click("a", self.pid()); time.sleep(0.4)

    def test_not_ok_after_command_calls_bridge_with_history(self):
        R.MODE = "execute"
        self.run_round()
        self.assertEqual(len(self.continues), 1)
        c = self.continues[0]
        self.assertEqual((c["fingerprint"], c["round"], c["down_jobs"]), ("f1", 1, ["backend"]))
        self.assertEqual(c["history"][0]["action_id"], "restart_container")
        self.assertTrue(c["history"][0]["ok"])

    def test_ok_after_command_ends_loop_without_calling_bridge(self):
        R.MODE = "execute"
        self.run_round(down=())
        self.assertEqual(self.continues, [])
        self.assertEqual(R.loops["f1"]["state"], "done")

    def test_logs_tail_always_goes_back_to_ai(self):
        R.MODE = "execute"
        self.run_round(action="logs_tail", down=())
        self.assertEqual(len(self.continues), 1)

    def test_dry_run_never_loops(self):
        self.run_round()
        self.assertEqual(self.continues, [])
        self.assertEqual(R.loops["f1"]["state"], "done")

    def test_loop_ends_at_max_rounds(self):
        R.MODE = "execute"; R.MAX_ROUNDS = 2
        self.run_round(rnd=2)
        self.assertEqual(self.continues, [])
        self.assertTrue(any("hết" in t for t, _ in self.sent))
        self.assertEqual(audit_events()[-1]["event"], "loop_ended")

    def test_proposal_beyond_max_rounds_denied(self):
        R.MAX_ROUNDS = 2
        code, _ = R.handle_propose(POLICY, {"alert": "A", "fingerprint": "f", "action_id": "logs_tail",
                                            "container": "ecommerce-backend", "round": 3})
        self.assertEqual(code, 429)

    def test_stop_button_cancels_pending_and_blocks_further_rounds(self):
        R.MODE = "execute"
        self.propose(fp="f1"); pid = self.pid()
        self.click("s", pid)
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(R.loops["f1"]["state"], "stopped")
        code, _ = R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": "f1", "action_id": "logs_tail",
                                            "container": "ecommerce-backend", "round": 2})
        self.assertEqual(code, 409)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual([e["event"] for e in audit_events()].count("loop_stopped"), 1)

    def test_stop_during_command_prevents_continue(self):
        R.MODE = "execute"
        self.propose(); pid = self.pid()
        R.loops["f1"]["state"] = "stopped"
        self.click("a", pid); time.sleep(0.3)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(self.continues, [])

    def test_new_incident_with_same_fingerprint_resets_stop(self):
        self.propose(fp="f1"); self.click("s", self.pid())
        self.assertEqual(self.propose(fp="f1", container="ecommerce-nginx")[0], 200)   # round 1 = sự cố mới

    def test_stop_command_from_right_chat_only(self):
        self.propose(fp="f1")
        R.handle_message(POLICY, {"text": "/stop", "chat": {"id": 222}, "from": {"id": 999}})
        self.assertEqual(R.loops["f1"]["state"], "active")
        R.APPROVERS = {"1"}
        R.handle_message(POLICY, {"text": "/stop", "chat": {"id": 111}, "from": {"id": 999}})
        self.assertEqual(R.loops["f1"]["state"], "active")
        self.assertEqual([e["event"] for e in audit_events()].count("unauthorized_stop"), 2)
        R.APPROVERS = set()
        R.handle_message(POLICY, {"text": "/stop@my_bot", "chat": {"id": 111}, "from": {"id": 999}})
        self.assertEqual(R.loops["f1"]["state"], "stopped")

    def test_stop_by_alert_name_and_unknown_name(self):
        self.propose(fp="f1")
        R.handle_message(POLICY, {"text": "/stop Khac", "chat": {"id": 111}, "from": {"id": 1}})
        self.assertEqual(R.loops["f1"]["state"], "active")
        R.handle_message(POLICY, {"text": "/stop targetdown", "chat": {"id": 111}, "from": {"id": 1}})
        self.assertEqual(R.loops["f1"]["state"], "stopped")

    def test_non_stop_text_ignored(self):
        self.propose(fp="f1")
        R.handle_message(POLICY, {"text": "/stopall", "chat": {"id": 111}, "from": {"id": 1}})
        R.handle_message(POLICY, {"text": "xin chào", "chat": {"id": 111}, "from": {"id": 1}})
        self.assertEqual(R.loops["f1"]["state"], "active")

    def test_reject_offers_ask_again_or_stop_and_followup_calls_bridge(self):
        self.propose(); pid = self.pid()
        self.click("r", pid)
        text, buttons = self.sent[-1]
        self.assertEqual([b["text"][:1] for b in buttons], ["🔁", "⏹"])
        menu = next(p for p in R.pending.values() if p["state"] == "rejected")
        self.click("n", menu["id"]); time.sleep(0.3)
        self.assertEqual(len(self.continues), 1)
        self.assertIn("TỪ CHỐI", self.continues[0]["status"])
        self.assertEqual(self.docker_calls, [])

    def test_rejected_menu_cannot_be_approved(self):
        R.MODE = "execute"
        self.propose(); self.click("r", self.pid())
        menu = next(p for p in R.pending.values() if p["state"] == "rejected")
        self.click("a", menu["id"]); time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])

    def test_close_from_bridge_cancels_pending(self):
        self.propose(fp="f1")
        R.close_loop("f1")
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(R.loops["f1"]["state"], "done")
        self.assertEqual(audit_events()[-1]["event"], "loop_closed")

    def test_loop_disabled_without_bridge_url(self):
        R.BRIDGE_URL = ""; R.MODE = "execute"
        self.run_round()
        self.assertEqual(self.continues, [])

    def test_rate_limited_proposal_tells_admin(self):
        R.MODE = "execute"
        self.run_round(down=())
        code, _ = R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": "f2", "action_id": "restart_container",
                                            "container": "ecommerce-backend", "round": 1})
        self.assertEqual(code, 429)
        self.assertIn("chưa chạy được", self.sent[-1][0])


class ManualCommandTests(Base):
    """Người gõ /restart|/start|/logs <container> trong chat: đi qua policy + nút Duyệt, không có vòng lặp AI."""

    def say(self, text, chat=111, uid=999):
        R.handle_message(POLICY, {"text": text, "chat": {"id": chat}, "from": {"id": uid}})

    def pending_one(self):
        return next(iter(R.pending.values()))

    def test_restart_creates_confirmation_with_buttons_and_audit(self):
        self.say("/restart ecommerce-backend")
        p = self.pending_one()
        self.assertEqual((p["action_id"], p["container"], p.get("manual")), ("restart_container", "ecommerce-backend", True))
        text, buttons = self.sent[-1]
        self.assertIn("LỆNH BẠN VỪA GÕ", text)
        self.assertEqual([b["text"][:1] for b in buttons], ["✅", "❌", "⏹"])
        ev = [e["event"] for e in audit_events()]
        self.assertIn("manual_command", ev)
        self.assertIn("proposed", ev)
        self.assertEqual(self.docker_calls, [])               # chưa bấm Duyệt thì chưa chạy

    def test_short_name_expands_to_ecommerce_prefix(self):
        self.say("/restart nginx")
        self.assertEqual((self.pending_one()["action_id"], self.pending_one()["container"]), ("restart_container", "ecommerce-nginx"))

    def test_typed_command_runs_only_after_approve(self):
        R.MODE = "execute"
        self.say("/restart backend")
        self.click("a", self.pending_one()["id"]); time.sleep(0.4)
        self.assertEqual(self.docker_calls, [("restart_container", "ecommerce-backend")])

    def test_policy_still_applies_to_typed_commands(self):
        self.say("/restart ecommerce-db")                     # restart DB vẫn bị cấm
        self.assertFalse(any(p["action_id"] == "restart_container" and p["container"] == "ecommerce-db" for p in R.pending.values()))
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])
        self.say("/restart ../etc")
        self.say("/restart backend;rm")
        self.assertEqual(self.docker_calls, [])

    def test_start_db_allowed_by_exception(self):
        self.say("/start db")
        self.assertEqual((self.pending_one()["action_id"], self.pending_one()["container"]), ("start_container", "ecommerce-db"))

    def test_stranger_or_wrong_chat_ignored_and_audited(self):
        R.APPROVERS = {"1"}
        self.say("/restart backend", uid=999)
        self.say("/restart backend", chat=222, uid=1)
        self.assertEqual(len(R.pending), 0)
        self.assertEqual([e["event"] for e in audit_events()].count("unauthorized_command"), 2)

    def test_cooldown_applies(self):
        R.MODE = "execute"
        self.say("/restart backend"); self.click("a", self.pending_one()["id"]); time.sleep(0.4)
        R.pending.clear()
        self.say("/restart backend")
        self.assertEqual(len(R.pending), 0)
        self.assertIn("chưa chạy được", self.sent[-1][0])

    def test_missing_name_shows_help_and_help_command(self):
        self.say("/restart")
        self.assertIn("Thiếu tên container", self.sent[-1][0])
        self.say("/help")
        self.assertIn("/logs", self.sent[-1][0])
        self.assertEqual(len(R.pending), 0)

    def test_typed_command_never_calls_ai_bridge(self):
        R.MODE = "execute"; R.BRIDGE_URL = "http://bridge:8080"
        calls = []
        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("không được gọi bridge"))
        try:
            R.prom_down_jobs = lambda: ["backend"]            # hệ thống "chưa ổn" cũng không kích hoạt vòng AI
            self.say("/restart backend"); self.click("a", self.pending_one()["id"]); time.sleep(0.4)
        finally:
            R.urllib.request.urlopen = orig
        self.assertEqual(calls, [])
        self.assertEqual(self.docker_calls, [("restart_container", "ecommerce-backend")])

    def test_reject_typed_command_has_no_ask_ai_menu(self):
        R.BRIDGE_URL = "http://bridge:8080"
        self.say("/restart backend"); self.click("r", self.pending_one()["id"])
        self.assertIsNone(self.sent[-1][1])                   # không có nút "Hỏi AI cách khác"

    def test_stop_command_still_works(self):
        self.propose(fp="f1")
        self.say("/stop")
        self.assertEqual(R.loops["f1"]["state"], "stopped")


class ManualDeniedTests(Base):
    """Lệnh gõ tay nằm ngoài kho lệnh / bị rule chặn: báo KHÔNG ĐƯỢC PHÉP và tuyệt đối không chạy."""

    def say(self, text, chat=111, uid=999):
        R.handle_message(POLICY, {"text": text, "chat": {"id": chat}, "from": {"id": uid}})

    def test_forbidden_container_says_not_allowed_without_buttons(self):
        R.MODE = "execute"
        self.say("/restart ecommerce-db")
        text, buttons = self.sent[-1]
        self.assertIn("KHÔNG ĐƯỢC PHÉP", text)
        self.assertIsNone(buttons)                         # không chìa nút chạy lệnh khác
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.docker_calls, [])
        self.assertIn("denied", [e["event"] for e in audit_events()])

    def test_forbidden_logs_on_db_not_allowed(self):
        self.say("/logs db")
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])
        self.assertEqual(len(R.pending), 0)

    def test_unknown_container_not_allowed(self):
        self.say("/restart some-other-app")
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])
        self.assertEqual(len(R.pending), 0)

    def test_docker_style_known_verbs_work(self):
        self.say("docker restart ecommerce-backend")
        self.assertEqual([(p["action_id"], p["container"]) for p in R.pending.values()], [("restart_container", "ecommerce-backend")])
        R.pending.clear()
        self.docker_calls.clear()
        self.say("docker logs --tail 50 nginx")           # cờ bị bỏ qua; xem log là đọc nên chạy ngay, lệnh thật lấy từ mẫu cố định
        self.assertEqual(self.docker_calls, [("logs_tail", "ecommerce-nginx")])
        self.assertEqual(len(R.pending), 0)

    def test_dangerous_docker_verbs_refused_and_never_run(self):
        R.MODE = "execute"
        for cmd in ("docker stop ecommerce-backend", "docker rm -f ecommerce-db", "docker kill ecommerce-nginx",
                    "docker exec -it ecommerce-db sh", "docker system prune -af", "docker compose down -v"):
            self.sent.clear()
            self.say(cmd)
            self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0], cmd)
            self.assertIsNone(self.sent[-1][1], cmd)
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.docker_calls, [])
        self.assertTrue(any(e["event"] == "denied" for e in audit_events()))

    def test_docker_style_from_stranger_ignored(self):
        R.APPROVERS = {"1"}
        self.say("docker rm -f ecommerce-db", uid=999)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.docker_calls, [])
        self.assertEqual([e["event"] for e in audit_events()], ["unauthorized_command"])

    def test_chitchat_ignored(self):
        for t in ("xin chào", "ok", "cảm ơn nhé", "hôm nay trời đẹp"):
            self.say(t)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(R.pending), 0)

    def test_injection_in_name_not_allowed(self):
        for cmd in ("/restart backend;reboot", "/restart $(id)", "/restart ../etc"):
            self.sent.clear()
            self.say(cmd)
            self.assertEqual(len(R.pending), 0, cmd)
        self.assertEqual(self.docker_calls, [])


class ToolApiTests(Base):
    """Cách 2: AI gọi công cụ qua /tool/*. Luật + người duyệt vẫn là lớp chặn; AI không chạy được lệnh."""

    TOKEN = "ToolTokenForTests1234567890abcdef"

    def setUp(self):
        super().setUp()
        R.TOOL_TOKEN, R.TOOL_MAX_READS, R.TOOL_MAX_DENIED = self.TOKEN, 12, 4
        R.tool_sessions.clear()
        self.reads = self.docker_calls   # cùng 1 danh sách: mọi lần gọi Docker (đọc lẫn ghi) đều ghi vào đây
        secret_env = {"Env": ["AWS_SECRET_ACCESS_KEY=SUPERSECRET123", "MYSQL_ROOT_PASSWORD=hunter2"]}
        self.inspect_json = json.dumps(dict(secret_env, RestartCount=2, Config={"Image": "img:1", "Env": secret_env["Env"]},
                                            State={"Status": "running", "Running": True, "ExitCode": 0, "OOMKilled": False,
                                                   "StartedAt": "t0", "FinishedAt": "t1", "Health": {"Status": "healthy"}}))
        R.docker_call = lambda policy, a, c: (self.reads.append((a, c)) or (200, self.inspect_json if a == "inspect_container" else "dòng log 1\ndòng log 2"))

    def read(self, action, container, fp="t1", rnd=1):
        return R.tool_read(POLICY, {"fingerprint": fp, "round": rnd, "alert": "X", "action_id": action, "container": container})

    def propose(self, action="restart_container", container="ecommerce-backend", fp="t1", rnd=1):
        return R.tool_propose(POLICY, {"fingerprint": fp, "round": rnd, "alert": "X", "action_id": action,
                                       "container": container, "reason": "thử"})

    # ---- xác thực
    def test_auth_requires_exact_token_and_is_off_when_empty(self):
        self.assertTrue(R.tool_auth_ok("Bearer " + self.TOKEN))
        self.assertFalse(R.tool_auth_ok("Bearer wrong"))
        self.assertFalse(R.tool_auth_ok(""))
        self.assertFalse(R.tool_auth_ok(None))
        R.TOOL_TOKEN = ""
        self.assertFalse(R.tool_auth_ok("Bearer "))            # token trống = tắt hẳn, không ai vào được
        self.assertFalse(R.tool_auth_ok("Bearer anything"))

    def test_http_endpoints_enforce_auth(self):
        import threading, urllib.error, urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), R.make_handler(POLICY))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        orig = R.urllib.request.urlopen

        def call(path, token=None, data=None):
            req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with orig(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        try:
            self.assertEqual(call("/tool/catalog")[0], 401)
            self.assertEqual(call("/tool/catalog", "bad")[0], 401)
            self.assertEqual(call("/tool/read", None, {"action_id": "logs_tail", "container": "ecommerce-nginx"})[0], 401)
            self.assertEqual(call("/tool/propose", "bad", {"action_id": "restart_container", "container": "ecommerce-nginx"})[0], 401)
            code, cat = call("/tool/catalog", self.TOKEN)
            self.assertEqual(code, 200)
            self.assertIn("inspect_container", cat["actions"])
            code, res = call("/tool/read", self.TOKEN, {"fingerprint": "h1", "action_id": "logs_tail", "container": "ecommerce-nginx"})
            self.assertEqual((code, res["status"]), (200, "ok"))
            R.TOOL_TOKEN = ""
            self.assertEqual(call("/tool/catalog", self.TOKEN)[0], 503)
            self.assertIn("tool_denied_auth", [e["event"] for e in audit_events()])
        finally:
            srv.shutdown()
        self.assertEqual([c for c in self.docker_calls if c[0] in ("restart_container", "start_container")], [])   # chưa lần nào chạy lệnh thay đổi

    # ---- /tool/read
    def test_inspect_never_leaks_environment(self):
        code, res = self.read("inspect_container", "ecommerce-backend")
        self.assertEqual(code, 200)
        dumped = json.dumps(res)
        for leak in ("SUPERSECRET123", "hunter2", "AWS_SECRET", "Env"):
            self.assertNotIn(leak, dumped)
        self.assertEqual(res["data"]["status"], "running")
        self.assertEqual(res["data"]["restart_count"], 2)
        self.assertEqual(res["data"]["health"], "healthy")

    def test_inspect_db_allowed_but_logs_and_restart_on_db_denied(self):
        self.assertEqual(self.read("inspect_container", "ecommerce-db")[0], 200)
        self.assertEqual(self.read("logs_tail", "ecommerce-db")[0], 403)
        code, res = self.read("restart_container", "ecommerce-db")
        self.assertEqual(code, 403)
        self.assertNotIn(("restart_container", "ecommerce-db"), self.reads)

    def test_read_rejects_mutating_actions_and_never_calls_docker(self):
        for action in ("restart_container", "start_container"):
            code, res = self.read(action, "ecommerce-backend")
            self.assertEqual(code, 403, action)
            self.assertIn("propose_fix", res["message"])
        self.assertEqual(self.reads, [])

    def test_read_unknown_action_and_bad_names_denied(self):
        for a, c in (("rm", "ecommerce-backend"), ("logs_tail", "../etc"), ("logs_tail", "x;rm"), (None, None), ("logs_tail", "other-app")):
            self.assertEqual(self.read(a, c)[0], 403, (a, c))
        self.assertEqual(self.reads, [])

    def test_read_limit_per_session(self):
        R.TOOL_MAX_READS = 3
        R.POLICY_RATE = None
        codes = []
        for _ in range(5):
            R.history.clear()                                   # bỏ giới hạn giờ theo (action, container) để thử riêng giới hạn phiên
            codes.append(self.read("inspect_container", "ecommerce-backend")[0])
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        R.history.clear()
        self.assertEqual(self.read("inspect_container", "ecommerce-backend", fp="khac")[0], 200)   # phiên khác có bộ đếm riêng

    def test_stats_shape(self):
        stats = {"cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 2000, "online_cpus": 2},
                 "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
                 "memory_stats": {"usage": 52428800, "limit": 104857600}, "pids_stats": {"current": 7}, "Env": ["SECRET=1"]}
        res = R.shape_read_output("container_stats", 200, json.dumps(stats))
        self.assertEqual(res["data"]["mem_percent"], 50.0)
        self.assertEqual(res["data"]["pids"], 7)
        self.assertNotIn("SECRET", json.dumps(res))

    def test_logs_output_is_truncated(self):
        res = R.shape_read_output("logs_tail", 200, "x" * 20000)
        self.assertLessEqual(len(res["data"]), R.READ_OUTPUT_MAX)

    # ---- /tool/propose
    def test_propose_creates_pending_with_buttons_and_does_not_run(self):
        R.MODE = "execute"
        code, res = self.propose()
        self.assertEqual((code, res["status"]), (200, "queued"))
        self.assertEqual(len(R.pending), 1)
        self.assertEqual([b["text"][:1] for b in self.sent[-1][1]], ["✅", "❌", "⏹"])
        self.assertEqual(self.docker_calls, [])                  # chưa duyệt thì AI không chạy được gì
        self.assertIn("tool_propose", [e["event"] for e in audit_events()])

    def test_propose_runs_only_after_human_clicks(self):
        R.MODE = "execute"
        self.propose()
        pid = next(iter(R.pending))
        self.click("a", pid); time.sleep(0.4)
        self.assertEqual(self.docker_calls, [("restart_container", "ecommerce-backend")])

    def test_one_proposal_per_session(self):
        self.assertEqual(self.propose()[1]["status"], "queued")
        code, res = self.propose(container="ecommerce-nginx")
        self.assertEqual(res["status"], "already_proposed")
        self.assertEqual(len(R.pending), 1)
        self.assertEqual(self.propose(rnd=2, container="ecommerce-nginx")[1]["status"], "queued")   # vòng sau là phiên mới

    def test_denied_returns_reason_and_alternatives_without_alt_buttons(self):
        code, res = self.propose(container="ecommerce-db")
        self.assertEqual((code, res["status"]), (403, "denied"))
        self.assertIn("cấm", res["message"])
        self.assertTrue(res["alternatives"])
        self.assertTrue(all(a["container"] != "ecommerce-db" or a["action_id"] == "start_container" for a in res["alternatives"]))
        for a in res["alternatives"]:                           # AI được chỉ rõ phải dùng công cụ nào cho từng gợi ý
            self.assertEqual(a["via"], "read_status" if a["action_id"] == "logs_tail" else "propose_fix")
        self.assertEqual(len(R.pending), 0)                      # không chìa nút: AI tự chọn lại rồi mới có nút
        self.assertIn("AI đã được báo lý do", self.sent[-1][0])
        self.assertEqual(self.sent[-1][1], None)

    def test_ai_can_retry_with_alternative_after_denial(self):
        self.propose(container="ecommerce-db", action="restart_container")
        code, res = self.propose(container="ecommerce-db", action="start_container")
        self.assertEqual((code, res["status"]), (200, "queued"))
        p = next(iter(R.pending.values()))
        self.assertEqual((p["action_id"], p["container"]), ("start_container", "ecommerce-db"))

    def test_too_many_denials_in_one_session(self):
        for _ in range(4):
            self.propose(action="rm", container="ecommerce-backend")
        code, res = self.propose(action="rm", container="ecommerce-backend")
        self.assertEqual((code, res["status"]), (429, "rate_limited"))

    def test_repeating_same_command_in_next_round_is_reported_as_duplicate(self):
        self.assertEqual(self.propose(rnd=1)[1]["status"], "queued")
        code, res = self.propose(rnd=2)                         # cùng action + container ở vòng sau
        self.assertEqual(res["status"], "duplicate")
        self.assertIn("KHÁC", res["message"])
        self.assertEqual(len(R.pending), 1)

    def test_stopped_incident_cannot_be_proposed(self):
        self.propose(); R.stop_loop("t1", "999")
        code, res = self.propose(rnd=2, container="ecommerce-nginx")
        self.assertEqual((code, res["status"]), (409, "stopped"))

    def test_cooldown_reported_to_ai(self):
        R.MODE = "execute"
        self.propose(); self.click("a", next(iter(R.pending))); time.sleep(0.4)
        code, res = self.propose(fp="t9")
        self.assertEqual((code, res["status"]), (429, "rate_limited"))

    def test_session_info_counts(self):
        self.propose(); self.read("logs_tail", "ecommerce-nginx")
        self.assertEqual(R.tool_session_info("t1", 1), {"queued": 1, "denied": 0, "reads": 1})
        self.assertEqual(R.tool_session_info("zzz", 1), {"queued": 0, "denied": 0, "reads": 0})

    def test_catalog_marks_approval_and_exceptions(self):
        cat = R.tool_catalog(POLICY)
        self.assertEqual(cat["actions"]["restart_container"]["approval"], "required")
        self.assertEqual(cat["actions"]["logs_tail"]["approval"], "none")
        self.assertIn("ecommerce-db", cat["actions"]["inspect_container"]["containers"])
        self.assertNotIn("ecommerce-db", cat["actions"]["restart_container"]["containers"])
        self.assertIn("ecommerce-remediator", cat["containers_forbidden"])


class NaturalLanguageTests(Base):
    """Câu nói tự nhiên: đường A (từ khoá, miễn phí) -> nút Duyệt hiện ĐÚNG lệnh đã hiểu; câu lạ -> nhờ AI (đường B)."""

    def say(self, text, chat=111, uid=999):
        R.handle_message(POLICY, {"text": text, "chat": {"id": chat}, "from": {"id": uid}})

    def only_pending(self):
        self.assertEqual(len(R.pending), 1, [(p["action_id"], p["container"]) for p in R.pending.values()])
        p = next(iter(R.pending.values()))
        return (p["action_id"], p["container"])

    # ---- parse_intent
    def test_parse_intent_examples(self):
        cases = {
            "chạy cho tôi container db": (["start_container"], ["ecommerce-db"]),
            "bật lại db đi": (["start_container"], ["ecommerce-db"]),
            "khởi động lại backend": (["restart_container"], ["ecommerce-backend"]),
            "Restart NGINX giúp mình": (["restart_container"], ["ecommerce-nginx"]),
            "xem log của nginx-exporter": (["logs_tail"], ["ecommerce-nginx-exporter"]),
            "trạng thái của database": (["inspect_container"], ["ecommerce-db"]),
            "chạy lại mysqld-exporter": (["restart_container"], ["ecommerce-mysqld-exporter"]),
        }
        for text, (acts, conts) in cases.items():
            it = R.parse_intent(POLICY, text)
            self.assertEqual((it["actions"], it["containers"]), (acts, conts), text)

    def test_restart_phrase_is_not_also_start(self):
        self.assertEqual(R.parse_intent(POLICY, "khởi động lại db")["actions"], ["restart_container"])
        self.assertEqual(R.parse_intent(POLICY, "khởi động db")["actions"], ["start_container"])

    def test_longest_alias_wins(self):
        self.assertEqual(R.parse_intent(POLICY, "restart nginx-exporter")["containers"], ["ecommerce-nginx-exporter"])
        self.assertEqual(R.parse_intent(POLICY, "restart nginx")["containers"], ["ecommerce-nginx"])

    def test_normalize_strips_accents(self):
        self.assertEqual(R.normalize_text("Khởi ĐỘNG lại, cơ sở dữ liệu!"), "khoi dong lai co so du lieu")

    # ---- đường A: một cách hiểu duy nhất -> nút Duyệt hiện lệnh cụ thể
    def test_start_db_sentence_creates_confirmation(self):
        self.say("chạy cho tôi container db")
        self.assertEqual(self.only_pending(), ("start_container", "ecommerce-db"))
        text, buttons = self.sent[-1]
        self.assertIn("LỆNH BẠN VỪA GÕ", text)
        self.assertEqual([b["text"][:1] for b in buttons], ["✅", "❌", "⏹"])
        self.assertEqual([c for c in self.docker_calls if c[0] in ("restart_container", "start_container")], [])   # chưa bấm Duyệt thì chưa chạy

    def test_sentence_runs_only_after_click(self):
        R.MODE = "execute"
        self.say("khởi động lại backend")
        self.click("a", next(iter(R.pending))); time.sleep(0.4)
        self.assertEqual(self.docker_calls, [("restart_container", "ecommerce-backend")])

    def test_policy_still_blocks_sentences(self):
        self.say("khởi động lại db")                                # restart DB bị cấm
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.docker_calls, [])

    def test_dangerous_sentences_refused(self):
        R.MODE = "execute"
        for t in ("xóa container db", "tắt backend đi", "stop nginx", "kill ecommerce-db", "dừng lại grafana", "prune hết đi backend"):
            self.sent.clear()
            self.say(t)
            self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0], t)
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.docker_calls, [])

    # ---- đường A: mơ hồ -> hỏi lại bằng nút
    def test_two_containers_offer_choices_and_click_drops_others(self):
        R.MODE = "execute"
        self.say("restart backend và nginx")
        self.assertEqual(len(R.pending), 2)
        text, buttons = self.sent[-1]
        self.assertIn("một trong các việc", text)
        rows = buttons if isinstance(buttons[0], list) else [buttons]
        self.assertEqual(len(rows), 2)
        first = next(iter(R.pending))
        self.click("a", first); time.sleep(0.4)
        self.assertEqual(len(self.docker_calls), 1)
        self.assertEqual(len(R.pending), 0)                          # lựa chọn còn lại bị huỷ

    def test_too_many_combinations_asks_to_be_specific(self):
        self.say("restart backend nginx grafana loki")
        self.assertEqual(len(R.pending), 0)
        self.assertIn("quá nhiều việc", self.sent[-1][0])

    # ---- đường B: nhờ AI
    def test_unknown_request_goes_to_ai_when_enabled(self):
        calls = []
        R.BRIDGE_URL, R.TOOL_TOKEN = "http://bridge:8080", "tok"

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda req, timeout=0: (calls.append((req.full_url, json.loads(req.data), req.headers.get("Authorization"))) or Resp())
        try:
            self.say("vì sao backend chậm vậy?")
        finally:
            R.urllib.request.urlopen = orig
            R.BRIDGE_URL, R.TOOL_TOKEN = "", ""
        self.assertEqual(len(calls), 1)
        url, body, auth = calls[0]
        self.assertEqual(url, "http://bridge:8080/chat")
        self.assertEqual(body["text"], "vì sao backend chậm vậy?")
        self.assertEqual(auth, "Bearer tok")
        self.assertIn("chat_request", [e["event"] for e in audit_events()])
        self.assertEqual(self.docker_calls, [])                      # chuyển cho AI chưa chạy gì

    def test_unknown_request_without_ai_gives_hint(self):
        self.say("vì sao backend chậm vậy?")
        self.assertIn("chưa hiểu", self.sent[-1][0])

    def test_strangers_never_reach_natural_language(self):
        R.APPROVERS = {"1"}
        R.BRIDGE_URL, R.TOOL_TOKEN = "http://bridge:8080", "tok"
        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("không được gọi AI"))
        try:
            self.say("khởi động lại backend", uid=999)
            self.say("khởi động lại backend", chat=222, uid=1)
        finally:
            R.urllib.request.urlopen = orig
            R.BRIDGE_URL, R.TOOL_TOKEN = "", ""
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.sent, [])

    # ---- /status và lọc trường
    def test_status_command_runs_immediately_and_never_leaks_env(self):
        R.MODE = "execute"
        secret = json.dumps({"Config": {"Env": ["AWS_SECRET_ACCESS_KEY=SUPERSECRET123"], "Image": "i"},
                             "State": {"Status": "exited", "Running": False, "ExitCode": 137}, "RestartCount": 1})
        R.docker_call = lambda policy, a, c: (self.docker_calls.append((a, c)) or (200, secret))
        self.say("/status db")
        self.assertEqual(len(R.pending), 0)                          # chỉ đọc: không cần nút Duyệt
        self.assertEqual(self.docker_calls, [("inspect_container", "ecommerce-db")])
        shown = self.sent[-1][0]
        self.assertIn("exited", shown)
        self.assertIn("mã thoát 137", shown)
        with open(os.environ["AUDIT_FILE"], encoding="utf-8") as f:
            audit_raw = f.read()
        for blob in (shown, audit_raw):
            self.assertNotIn("SUPERSECRET123", blob)
            self.assertNotIn("AWS_SECRET", blob)

    def test_ai_cannot_use_propose_for_read_only_actions(self):
        R.TOOL_TOKEN = "t" * 32
        code, res = R.tool_propose(POLICY, {"fingerprint": "x", "round": 1, "alert": "A", "action_id": "inspect_container",
                                           "container": "ecommerce-backend", "reason": "r"})
        R.TOOL_TOKEN = ""
        self.assertEqual(code, 403)
        self.assertIn("read_status", res["message"])
        self.assertEqual(len(R.pending), 0)


class MonitorChatTests(Base):
    """Monitor qua Telegram: hỏi tình trạng -> bot lấy số liệu (Docker đã lọc + Prometheus) và báo lại NGAY. Chỉ đọc, không AI, không nút."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.states = {}           # container -> dict trạng thái; thiếu = đang chạy bình thường
        self._orig = (R.prom_scalar, R.prom_names)
        def fake_scalar(q):
            if "idle" in q:
                return 12.0
            if "MemAvail" in q:
                return 48.0
            if "filesystem" in q:
                return 33.0
            return 0.0 if "status_code" in q else 2.5          # 5xx = 0, req/s = 2.5
        R.prom_scalar = fake_scalar
        R.prom_names = lambda q, label: [] if "ALERTS" in q or "up == 0" in q else None
        R.docker_call = self.fake_docker
        self.addCleanup(lambda: (setattr(R, "prom_scalar", self._orig[0]), setattr(R, "prom_names", self._orig[1])))

    def fake_docker(self, policy, action, container):
        self.calls.append((action, container))
        if action == "logs_tail":
            return 200, "dòng log 1\ndòng log 2"
        st = self.states.get(container, {"Status": "running", "Running": True, "ExitCode": 0, "StartedAt": "2026-01-01T00:00:00Z",
                                         "Health": {"Status": "healthy"}})
        body = {"State": st, "RestartCount": st.get("_restarts", 0), "Config": {"Image": "img:1", "Env": ["AWS_SECRET_ACCESS_KEY=SUPERSECRET123"]}}
        return 200, json.dumps(body)

    def say(self, text, chat=111, uid=999):
        R.handle_message(POLICY, {"text": text, "chat": {"id": chat}, "from": {"id": uid}})

    def last(self):
        return self.sent[-1][0]

    # ---- tổng quan
    def test_natural_question_returns_overview_immediately(self):
        self.say("các container hiện tại thế nào?")
        text = self.last()
        self.assertIn("Tình trạng hệ thống", text)
        self.assertIn("backend", text)
        self.assertIn("CPU 12%", text)
        self.assertIn("RAM 48%", text)
        self.assertIn("ổ đĩa 33%", text)
        self.assertEqual(len(R.pending), 0)                       # không nút, không chờ duyệt
        self.assertIsNone(self.sent[-1][1])
        self.assertEqual({a for a, _ in self.calls}, {"inspect_container"})   # chỉ đọc

    def test_overview_triggers(self):
        for t in ("/status", "docker ps", "tình trạng hệ thống thế nào", "hệ thống ổn không", "tổng quan", "mọi thứ ổn không?",
                  "các container thế nào"):
            self.sent.clear(); self.calls.clear()
            self.say(t)
            self.assertIn("Tình trạng hệ thống", self.sent[-1][0], t)

    def test_overview_covers_allowed_containers_and_db_exception(self):
        self.say("/status")
        names = {c for _, c in self.calls}
        self.assertEqual(names, set(POLICY["containers_allowed"]) | {"ecommerce-db"})
        self.assertNotIn("ecommerce-remediator", names)           # container hệ thống bị cấm không bị đọc

    def test_overview_marks_stopped_unhealthy_and_oom(self):
        self.states["ecommerce-db"] = {"Status": "exited", "Running": False, "ExitCode": 137, "StartedAt": "2026-01-01T00:00:00Z"}
        self.states["ecommerce-backend"] = {"Status": "running", "Running": True, "ExitCode": 0, "StartedAt": "2026-01-01T00:00:00Z",
                                            "Health": {"Status": "unhealthy"}, "_restarts": 3, "OOMKilled": True}
        self.say("/status")
        text = self.last()
        db_line = next(l for l in text.splitlines() if "db ·" in l)
        be_line = next(l for l in text.splitlines() if "backend ·" in l)
        self.assertTrue(db_line.startswith("🔴"))
        self.assertIn("mã thoát 137", db_line)
        self.assertTrue(be_line.startswith("🟡"))
        self.assertIn("unhealthy", be_line)
        self.assertIn("restart 3", be_line)
        self.assertIn("OOM", be_line)
        self.assertIn("Chạy: <b>11/12</b>", text)

    def test_overview_shows_firing_alerts_and_down_jobs(self):
        R.prom_names = lambda q, label: ["MySQLDown", "High5xxRate"] if "ALERTS" in q else ["mysql"]
        self.say("/status")
        self.assertIn("MySQLDown, High5xxRate", self.last())
        self.assertIn("Chưa trả lời Prometheus: mysql", self.last())

    def test_overview_survives_prometheus_failure(self):
        R.prom_scalar = lambda q: None
        R.prom_names = lambda q, label: None
        self.say("/status")
        text = self.last()
        self.assertIn("n/a", text)
        self.assertIn("không hỏi được Prometheus", text)
        self.assertIn("backend", text)                            # phần Docker vẫn có

    def test_overview_never_leaks_environment_and_is_audited(self):
        self.say("/status")
        with open(os.environ["AUDIT_FILE"], encoding="utf-8") as f:
            audit_raw = f.read()
        for blob in (self.last(), audit_raw):
            self.assertNotIn("SUPERSECRET123", blob)
            self.assertNotIn("AWS_SECRET", blob)
        self.assertIn("monitor_query", [e["event"] for e in audit_events()])

    def test_strangers_cannot_query_status(self):
        R.APPROVERS = {"1"}
        self.say("/status", uid=999)
        self.say("các container thế nào?", chat=222, uid=1)
        self.say("docker ps", uid=999)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.calls, [])

    # ---- đọc 1 container: chạy ngay, không nút
    def test_single_container_reads_run_immediately(self):
        self.say("/status db")
        self.assertIn("db ·", self.last())
        self.assertEqual(len(R.pending), 0)
        self.sent.clear()
        self.say("xem log nginx")
        self.assertIn("Log cuối của", self.last())
        self.assertIn("dòng log 2", self.last())
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.calls[-1], ("logs_tail", "ecommerce-nginx"))

    def test_status_of_single_container_hides_env(self):
        self.say("/status db")
        self.assertNotIn("SUPERSECRET123", self.last())
        self.assertNotIn("Env", self.last())

    def test_mixed_sentence_runs_read_and_asks_confirmation_for_write(self):
        self.say("xem log nginx rồi khởi động lại backend")
        self.assertTrue(any("Log cuối của" in t for t, _ in self.sent))     # phần đọc chạy ngay
        # câu có 2 việc x 2 container: bot không đoán việc nào đi với container nào -> phần ghi hỏi lại bằng nút (không tự chạy)
        self.assertEqual({(p["action_id"], p["container"]) for p in R.pending.values()},
                         {("restart_container", "ecommerce-backend"), ("restart_container", "ecommerce-nginx")})
        self.assertEqual([c for c in self.calls if c[0] != "logs_tail"], [])

    def test_reads_on_forbidden_containers_still_blocked(self):
        self.say("/logs db")                                      # log DB bị cấm (chỉ được inspect và start)
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.last())
        self.assertEqual(self.calls, [])

    def test_read_commands_do_not_consume_ai_rate_budget(self):
        for _ in range(15):                                       # vượt xa max_per_hour của logs_tail (10)
            self.say("/logs nginx")
        self.assertEqual(sum(1 for t, _ in self.sent if "Log cuối của" in t), 15)


class AiSessionTests(Base):
    """/ai: phiên làm việc với AI. Trong phiên mọi tin nhắn thường tới AI (kèm sid để bridge giữ trí nhớ); ngoài phiên thì như cũ."""

    def setUp(self):
        super().setUp()
        R.BRIDGE_URL, R.TOOL_TOKEN, R.AI_SESSION_IDLE = "http://bridge:8080", "tok" * 12, 900
        R.ai_session.update(sid=None, last=0.0)
        R.recent_actions.clear()
        self.bridge = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def close(s): pass
        outer = self
        self._orig_urlopen = R.urllib.request.urlopen

        def fake_urlopen(req, timeout=0):
            outer.bridge.append((req.full_url.replace("http://bridge:8080", ""), json.loads(req.data) if req.data else None))
            return Resp()
        R.urllib.request.urlopen = fake_urlopen
        self.typing = []
        R.tg_call = lambda method, body, timeout=40: (self.typing.append(method) if method == "sendChatAction" else None) or {}
        self.addCleanup(lambda: (setattr(R.urllib.request, "urlopen", self._orig_urlopen), setattr(R, "BRIDGE_URL", ""),
                                 setattr(R, "TOOL_TOKEN", ""), R.ai_session.update(sid=None, last=0.0)))

    def say(self, text, chat=111, uid=999):
        R.handle_message(POLICY, {"text": text, "chat": {"id": chat}, "from": {"id": uid}})

    def chats(self):
        return [b for p, b in self.bridge if p == "/chat"]

    def test_ai_command_opens_session_with_instructions(self):
        self.say("/ai")
        self.assertIn("Đã mở phiên làm việc với AI", self.sent[-1][0])
        self.assertIn("/ai off", self.sent[-1][0])
        self.assertIsNotNone(R.session_sid())
        self.assertIn("ai_session_start", [e["event"] for e in audit_events()])
        self.assertEqual(self.chats(), [])                          # mở phiên chưa gọi AI (chưa tốn token)

    def test_messages_in_session_go_to_ai_with_same_sid_even_when_short(self):
        self.say("/ai")
        sid = R.session_sid()
        for t in ("tôi muốn chạy lại cái này", "ok", "cái đó xong chưa"):       # không nêu container: để AI hiểu theo ngữ cảnh
            self.say(t)
        chats = self.chats()
        self.assertEqual([c["text"] for c in chats], ["tôi muốn chạy lại cái này", "ok", "cái đó xong chưa"])
        self.assertTrue(all(c["sid"] == sid for c in chats))
        self.assertEqual(len(R.pending), 0)                         # không tự đề xuất bằng từ khoá: để AI hiểu ngữ cảnh
        self.assertIn("sendChatAction", self.typing)                # báo "đang gõ", không spam tin

    def test_outside_session_short_chitchat_is_ignored_and_no_sid_sent(self):
        self.say("ok")
        self.assertEqual(self.chats(), [])
        self.say("vì sao backend chậm vậy?")
        self.assertEqual(len(self.chats()), 1)
        self.assertNotIn("sid", self.chats()[0])

    def test_ai_with_text_opens_session_and_asks_immediately(self):
        self.say("/ai container của backend sao rồi")
        self.assertIsNotNone(R.session_sid())
        self.assertEqual([c["text"] for c in self.chats()], ["container của backend sao rồi"])
        self.assertEqual(self.chats()[0]["sid"], R.session_sid())

    def test_ai_with_clear_command_is_answered_instantly_without_ai(self):
        self.say("/ai khởi động lại backend")
        self.assertIsNotNone(R.session_sid())
        self.assertEqual(self.chats(), [])
        self.assertEqual([(p["action_id"], p["container"]) for p in R.pending.values()], [("restart_container", "ecommerce-backend")])

    def test_off_and_exit_end_session_and_clear_bridge_memory(self):
        for cmd in ("/ai off", "/exit", "/thoat", "/ai thoát"):
            self.bridge.clear()
            self.say("/ai")
            sid = R.session_sid()
            self.say(cmd)
            self.assertIsNone(R.session_sid(), cmd)
            self.assertIn("Đã kết thúc phiên AI", self.sent[-1][0], cmd)
            self.assertIn(("/chat/end", {"sid": sid}), self.bridge, cmd)
        self.say("ok")                                              # sau khi đóng: tin ngắn lại bị bỏ qua
        self.assertEqual(self.chats(), [])

    def test_off_without_session_is_harmless(self):
        self.say("/ai off")
        self.assertIn("Không có phiên AI nào đang mở", self.sent[-1][0])

    def test_reset_keeps_session_but_changes_sid(self):
        self.say("/ai")
        old = R.session_sid()
        self.say("/ai reset")
        new = R.session_sid()
        self.assertTrue(new and new != old)
        self.assertIn(("/chat/end", {"sid": old}), self.bridge)

    def test_session_expires_after_idle_and_reports(self):
        self.say("/ai")
        R.ai_session["last"] = time.time() - R.AI_SESSION_IDLE - 5
        R.expire_pending(POLICY)                                    # vòng lặp nền gọi hàm này mỗi lượt poll
        self.assertIsNone(R.session_sid())
        self.assertIn("không hoạt động", self.sent[-1][0])
        self.assertIn("ai_session_end", [e["event"] for e in audit_events()])

    def test_message_activity_extends_session(self):
        self.say("/ai")
        R.ai_session["last"] = time.time() - R.AI_SESSION_IDLE + 30
        self.say("db sao rồi")
        self.assertGreater(R.ai_session["last"], time.time() - 5)
        self.assertIsNotNone(R.session_sid())

    def test_commands_still_work_inside_session(self):
        self.say("/ai")
        self.say("/restart backend")                                # lệnh có cấu trúc vẫn đi đường cũ (policy + nút Duyệt)
        self.assertEqual([(p["action_id"], p["container"]) for p in R.pending.values()], [("restart_container", "ecommerce-backend")])
        self.assertEqual(self.chats(), [])
        self.say("/restart db")
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])

    def test_generic_overview_question_is_answered_free_even_in_session(self):
        self.say("/ai")
        orig = (R.prom_scalar, R.prom_names)
        R.prom_scalar, R.prom_names = (lambda q: None), (lambda q, l: None)
        try:
            self.say("các container hiện tại thế nào?")
        finally:
            R.prom_scalar, R.prom_names = orig
        self.assertIn("Tình trạng hệ thống", self.sent[-1][0])
        self.assertEqual(self.chats(), [])                          # không tốn lượt AI

    def test_strangers_cannot_open_or_use_session(self):
        R.APPROVERS = {"1"}
        self.say("/ai", uid=999)
        self.say("/ai", chat=222, uid=1)
        self.assertIsNone(R.session_sid())
        self.assertEqual(self.chats(), [])
        self.assertEqual([e["event"] for e in audit_events()].count("unauthorized_command"), 2)
        self.say("/ai", uid=1)
        R.APPROVERS = {"1"}
        self.say("tôi muốn chạy backend", uid=999)                  # người lạ nói trong chat khi phiên đang mở: bị bỏ qua
        self.assertEqual(self.chats(), [])

    def test_session_needs_tool_token(self):
        R.TOOL_TOKEN = ""
        self.say("/ai")
        self.assertIsNone(R.session_sid())
        self.assertIn("REMEDIATOR_TOOL_TOKEN", self.sent[-1][0])

    def test_bridge_refusal_is_reported_not_silent(self):
        self.say("/ai")

        def refuse(req, timeout=0):
            raise R.urllib.error.HTTPError(req.full_url, 429, "x", {}, None)
        R.urllib.request.urlopen = refuse
        self.say("db sao rồi")
        self.assertIn("chưa nhận được", self.sent[-1][0])

    # ---- tool API cho chat: ngữ cảnh + tổng quan
    def test_tool_state_lists_pending_and_recent_without_secrets(self):
        self.propose = lambda **k: None
        R.handle_propose(POLICY, {"alert": "TargetDown", "fingerprint": "s1", "action_id": "restart_container",
                                  "container": "ecommerce-backend", "reason": "t", "round": 1})
        R.recent_actions.append({"ts": "10:00:00", "action_id": "start_container", "container": "ecommerce-db", "ok": True, "http": 204})
        st = R.tool_state()
        self.assertEqual([(x["action_id"], x["container"]) for x in st["pending"]], [("restart_container", "ecommerce-backend")])
        self.assertEqual(st["recent"][0]["container"], "ecommerce-db")
        self.assertNotIn("reason", json.dumps(st))

    def test_executed_mutating_command_is_remembered_for_chat_context(self):
        R.MODE = "execute"
        R.handle_propose(POLICY, {"alert": "A", "fingerprint": "s2", "action_id": "restart_container",
                                  "container": "ecommerce-backend", "reason": "t", "round": 1})
        self.click("a", next(iter(R.pending))); time.sleep(0.4)
        self.assertEqual([(x["action_id"], x["container"], x["ok"]) for x in R.recent_actions], [("restart_container", "ecommerce-backend", True)])

    def test_tool_overview_is_plain_text(self):
        orig = (R.prom_scalar, R.prom_names)
        R.prom_scalar, R.prom_names = (lambda q: None), (lambda q, l: None)
        try:
            res = R.tool_overview(POLICY)
        finally:
            R.prom_scalar, R.prom_names = orig
        self.assertEqual(res["status"], "ok")
        self.assertIn("Tình trạng hệ thống", res["text"])
        self.assertNotIn("<b>", res["text"])


class SessionFastPathTests(Base):
    """Trong phiên /ai, câu lệnh rõ ràng được hiểu NGAY bằng từ khoá (không chờ AI 15-20 giây); câu hỏi/mơ hồ mới gọi AI."""

    def setUp(self):
        super().setUp()
        R.BRIDGE_URL, R.TOOL_TOKEN = "http://bridge:8080", "tok" * 12
        R.ai_session.update(sid="abc123", last=time.time())
        self.bridge = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def close(s): pass
        outer = self
        self._orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda req, timeout=0: (outer.bridge.append(json.loads(req.data) if req.data else None) or Resp())
        self.running = {}
        R.docker_call = self.fake_docker
        self.addCleanup(lambda: (setattr(R.urllib.request, "urlopen", self._orig), setattr(R, "BRIDGE_URL", ""),
                                 setattr(R, "TOOL_TOKEN", ""), R.ai_session.update(sid=None, last=0.0)))

    def fake_docker(self, policy, action, container):
        self.docker_calls.append((action, container))
        run = self.running.get(container, True)
        return 200, json.dumps({"State": {"Status": "running" if run else "exited", "Running": run, "ExitCode": 0 if run else 137,
                                          "StartedAt": "2026-01-01T00:00:00Z"}, "RestartCount": 0, "Config": {"Image": "i", "Env": ["X=1"]}})

    def say(self, text):
        R.handle_message(POLICY, {"text": text, "chat": {"id": 111}, "from": {"id": 999}})

    def pend(self):
        return [(p["action_id"], p["container"]) for p in R.pending.values()]

    def test_clear_restart_with_spaced_name_is_instant(self):
        self.say("Khởi động lại container của back end")             # đúng câu trong ảnh chụp: trước đây mất ~19 giây chờ AI
        self.assertEqual(self.pend(), [("restart_container", "ecommerce-backend")])
        self.assertEqual(self.bridge, [])                            # không gọi AI

    def test_alias_variants_with_spaces(self):
        for text, expect in (("restart front end", "ecommerce-frontend"), ("khởi động lại node exporter", "ecommerce-node-exporter"),
                             ("restart nginx exporter", "ecommerce-nginx-exporter")):
            R.pending.clear()
            self.say(text)
            self.assertEqual(self.pend(), [("restart_container", expect)], text)

    def test_start_on_running_container_explains_and_offers_restart(self):
        self.say("chạy cho tôi container backend")                  # backend đang chạy: start vô ích
        self.assertEqual(self.pend(), [("restart_container", "ecommerce-backend")])
        self.assertTrue(any("đang chạy rồi" in t for t, _ in self.sent))
        self.assertEqual(self.bridge, [])

    def test_start_on_stopped_container_proposes_start(self):
        self.running["ecommerce-db"] = False
        self.say("chạy cho tôi container db")
        self.assertEqual(self.pend(), [("start_container", "ecommerce-db")])
        self.assertFalse(any("đang chạy rồi" in t for t, _ in self.sent))

    def test_question_with_container_still_goes_to_ai(self):
        for t in ("tại sao backend không chạy?", "có thể dùng container backend không", "backend bị gì vậy", "container backend sao rồi"):
            self.bridge.clear()
            self.say(t)
            self.assertEqual(len(self.bridge), 1, t)
        self.assertEqual(len(R.pending), 0)

    def test_dangerous_and_overview_are_instant_even_with_question_mark(self):
        self.say("xóa container db đi?")
        self.assertIn("KHÔNG ĐƯỢC PHÉP", self.sent[-1][0])
        orig = (R.prom_scalar, R.prom_names)
        R.prom_scalar, R.prom_names = (lambda q: None), (lambda q, l: None)
        try:
            self.say("các container hiện tại thế nào?")
        finally:
            R.prom_scalar, R.prom_names = orig
        self.assertIn("Tình trạng hệ thống", self.sent[-1][0])
        self.assertEqual(self.bridge, [])

    def test_vague_requests_go_to_ai(self):
        for t in ("tôi muốn chạy lại cái này", "xử lý giúp tôi container backend", "restart backend và nginx"):
            self.bridge.clear()
            R.pending.clear()
            self.say(t)
            if "và" in t:                                            # 2 container = mơ hồ giữa các nút (đường từ khoá), không tự chạy
                self.assertEqual(len(R.pending), 2)
            else:
                self.assertEqual(len(self.bridge), 1, t)


class SecurityHardeningTests(Base):
    """Các bản sửa sau đợt rà soát bảo mật."""

    def setUp(self):
        super().setUp()
        self._saved = (R.TG_CHAT, R.ALLOW_ANY_GROUP_MEMBER, R.TOOL_TOKEN, R.BRIDGE_URL)
        self.addCleanup(lambda: (setattr(R, "TG_CHAT", self._saved[0]), setattr(R, "ALLOW_ANY_GROUP_MEMBER", self._saved[1]),
                                 setattr(R, "TOOL_TOKEN", self._saved[2]), setattr(R, "BRIDGE_URL", self._saved[3])))

    # ---- S6: nhóm chat không khai báo người duyệt -> đóng chặt
    def test_group_chat_without_approvers_is_locked(self):
        R.TG_CHAT, R.APPROVERS, R.ALLOW_ANY_GROUP_MEMBER = "-100123", set(), False
        self.assertFalse(R.is_authorized("-100123", "999"))
        self.assertTrue(R.group_without_approvers())
        R.MODE = "execute"
        R.handle_propose(POLICY, {"alert": "A", "fingerprint": "g1", "action_id": "restart_container", "container": "ecommerce-backend"})
        pid = next(iter(R.pending))
        self.click("a", pid, chat="-100123", uid="999"); time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])                    # không ai trong nhóm duyệt được
        R.handle_message(POLICY, {"text": "/restart backend", "chat": {"id": -100123}, "from": {"id": 999}})
        self.assertEqual(len(R.pending), 1)                        # lệnh gõ cũng bị chặn

    def test_group_chat_with_approvers_or_explicit_opt_in(self):
        R.TG_CHAT = "-100123"
        R.APPROVERS = {"42"}
        self.assertTrue(R.is_authorized("-100123", "42"))
        self.assertFalse(R.is_authorized("-100123", "999"))
        R.APPROVERS, R.ALLOW_ANY_GROUP_MEMBER = set(), True
        self.assertTrue(R.is_authorized("-100123", "999"))

    def test_private_chat_and_wrong_chat(self):
        R.TG_CHAT, R.APPROVERS = "111", set()
        self.assertTrue(R.is_authorized("111", "111"))
        self.assertFalse(R.is_authorized("222", "111"))
        R.TG_CHAT = ""
        self.assertFalse(R.is_authorized("", ""))

    # ---- L4: chữ ký gồm cả loại nút
    def test_button_kind_cannot_be_swapped(self):
        data = R.callback_data("r", "abc123")
        _, pid, sig = data.split(":")
        self.assertEqual(R.parse_callback(data), ("r", "abc123"))
        for kind in ("a", "n", "s"):
            self.assertIsNone(R.parse_callback(f"{kind}:{pid}:{sig}"), kind)

    def test_swapped_reject_button_does_not_approve(self):
        R.MODE = "execute"
        self.propose(); pid = next(iter(R.pending))
        sig_r = R.callback_data("r", pid).split(":")[2]
        self.click("a", pid, sig=sig_r); time.sleep(0.2)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(audit_events()[-1]["event"], "invalid_click")

    # ---- S8: che dữ liệu nhạy cảm
    def test_redact_masks_secrets_and_emails(self):
        raw = ("login ok user=a@b.com password=hunter22 token=abc123456789 Authorization: Bearer abcdefghijklmnop12345 "
               "key AKIAABCDEFGHIJKLMNOP bedrock ABSKabcdefghijklmnopqrstuv tg 123456789:AAabcdefghijklmnopqrstuvwxyz123456789 "
               "jwt eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijklmn sid=s3cr3tsession")
        out = R.redact(raw)
        for secret in ("a@b.com", "hunter22", "abc123456789", "abcdefghijklmnop12345", "AKIAABCDEFGHIJKLMNOP",
                       "ABSKabcdefghijklmnopqrstuv", "AAabcdefghijklmnopqrstuvwxyz", "eyJhbGciOiJIUzI1", "s3cr3tsession"):
            self.assertNotIn(secret, out, secret)
        self.assertIn("***@b.com", out)
        self.assertIn("login ok", out)                             # chữ bình thường giữ nguyên

    def test_logs_are_redacted_for_ai_telegram_and_audit(self):
        R.docker_call = lambda policy, a, c: (self.docker_calls.append((a, c)) or (200, "POST /login password=hunter22 from a@b.com"))
        R.TOOL_TOKEN = "t" * 32
        code, res = R.tool_read(POLICY, {"fingerprint": "r1", "action_id": "logs_tail", "container": "ecommerce-nginx"})
        self.assertNotIn("hunter22", res["data"])
        R.handle_message(POLICY, {"text": "/logs nginx", "chat": {"id": 111}, "from": {"id": 999}})
        self.assertNotIn("hunter22", self.sent[-1][0])
        with open(os.environ["AUDIT_FILE"], encoding="utf-8") as f:
            self.assertNotIn("hunter22", f.read())

    # ---- L1: lệnh do AI chat đề xuất không đi vào vòng lặp sự cố
    def test_chat_proposal_has_no_incident_loop(self):
        R.MODE, R.BRIDGE_URL = "execute", "http://bridge:8080"
        calls = []
        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda req, timeout=0: calls.append(req.full_url) or (_ for _ in ()).throw(AssertionError("không được gọi bridge"))
        R.prom_down_jobs = lambda: ["backend"]                      # "chưa ổn" cũng không gọi /continue
        try:
            R.handle_propose(POLICY, {"alert": "Chat", "fingerprint": "chat-abc-1", "action_id": "restart_container",
                                      "container": "ecommerce-backend", "round": 1})
            self.assertNotIn("vòng 1/", self.sent[-1][0])
            self.click("a", next(iter(R.pending))); time.sleep(0.4)
        finally:
            R.urllib.request.urlopen = orig
        self.assertEqual(calls, [])
        self.assertFalse(any("AI không nhận được kết quả" in t or "Đợi tin" in t for t, _ in self.sent))

    def test_rejecting_chat_proposal_has_no_ask_ai_again_menu(self):
        R.BRIDGE_URL = "http://bridge:8080"
        R.handle_propose(POLICY, {"alert": "Chat", "fingerprint": "chat-abc-2", "action_id": "restart_container",
                                  "container": "ecommerce-backend", "round": 1})
        self.click("r", next(iter(R.pending)))
        self.assertIsNone(self.sent[-1][1])

    # ---- S5 + L2: endpoint nội bộ cần token; cờ manual/tool từ HTTP bị bỏ
    def http_server(self):
        import threading
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), R.make_handler(POLICY))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, base, path, body, token=None):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=h)
        try:
            with self._real_urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_propose_and_close_need_token_when_token_is_set(self):
        import urllib.request
        self._real_urlopen = urllib.request.urlopen
        R.TOOL_TOKEN = "T" * 32
        base = self.http_server()
        body = {"alert": "A", "fingerprint": "h1", "action_id": "restart_container", "container": "ecommerce-backend"}
        self.assertEqual(self.post(base, "/propose", body), 401)
        self.assertEqual(self.post(base, "/propose", body, "sai"), 401)
        self.assertEqual(self.post(base, "/close", {"fingerprint": "h1"}), 401)
        self.assertEqual(len(R.pending), 0)
        self.assertEqual(self.post(base, "/propose", body, "T" * 32), 200)
        self.assertEqual(len(R.pending), 1)

    def test_manual_flag_from_http_is_ignored(self):
        import urllib.request
        self._real_urlopen = urllib.request.urlopen
        R.TOOL_TOKEN = "T" * 32
        base = self.http_server()
        self.post(base, "/propose", {"alert": "A", "fingerprint": "h2", "action_id": "restart_container",
                                     "container": "ecommerce-backend", "manual": True, "tool": True}, "T" * 32)
        self.assertNotIn("LỆNH BẠN VỪA GÕ", self.sent[-1][0])        # không giả được "lệnh bạn vừa gõ"
        self.assertIn("AI ĐỀ XUẤT", self.sent[-1][0])
        self.assertFalse(next(iter(R.pending.values())).get("manual"))

    def test_legacy_mode_without_token_still_works_for_propose(self):
        import urllib.request
        self._real_urlopen = urllib.request.urlopen
        R.TOOL_TOKEN = ""
        base = self.http_server()
        self.assertEqual(self.post(base, "/propose", {"alert": "A", "fingerprint": "h3", "action_id": "restart_container",
                                                      "container": "ecommerce-backend"}), 200)

    def test_continue_call_to_bridge_carries_token(self):
        R.TOOL_TOKEN, R.BRIDGE_URL, R.MODE = "T" * 32, "http://bridge:8080", "execute"
        seen = []

        class Resp:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
        orig = R.urllib.request.urlopen
        R.urllib.request.urlopen = lambda req, timeout=0: seen.append(req.headers.get("Authorization")) or Resp()
        R.prom_down_jobs = lambda: ["backend"]
        try:
            R.handle_propose(POLICY, {"alert": "A", "fingerprint": "c1", "action_id": "restart_container",
                                      "container": "ecommerce-backend", "round": 1})
            self.click("a", next(iter(R.pending))); time.sleep(0.4)
        finally:
            R.urllib.request.urlopen = orig
        self.assertEqual(seen, ["Bearer " + "T" * 32])

    # ---- L3 + S10
    def test_backlog_is_skipped_on_start(self):
        R.tg_call = lambda method, body, timeout=40: [{"update_id": 7}, {"update_id": 9}] if body.get("offset") == -1 else []
        self.assertEqual(R.skip_backlog(), 10)
        R.tg_call = lambda method, body, timeout=40: []
        self.assertEqual(R.skip_backlog(), 0)

    def test_http_handler_has_timeout(self):
        self.assertTrue(0 < R.make_handler(POLICY).timeout <= 60)


class DockerTests(Base):
    def test_demux_logs(self):
        frame = b"\x01\x00\x00\x00\x00\x00\x00\x05hello" + b"\x02\x00\x00\x00\x00\x00\x00\x03err"
        self.assertEqual(R.demux_logs(frame), "helloerr")

    def test_request_built_from_template_only(self):
        m, url = R.build_docker_request(POLICY, "restart_container", "ecommerce-backend")
        self.assertEqual(m, "POST")
        self.assertTrue(url.endswith("/containers/ecommerce-backend/restart?t=10"))


if __name__ == "__main__":
    unittest.main()
