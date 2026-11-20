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
                self.assertEqual(name, "start_container")  # chỉ action không phá hoại mới được ngoại lệ

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
        self.say("/logs nginx")
        self.assertEqual((self.pending_one()["action_id"], self.pending_one()["container"]), ("logs_tail", "ecommerce-nginx"))

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
        self.say("docker logs --tail 50 nginx")           # cờ bị bỏ qua, lệnh thật lấy từ mẫu cố định
        self.assertEqual([(p["action_id"], p["container"]) for p in R.pending.values()], [("logs_tail", "ecommerce-nginx")])

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

    def test_plain_text_ignored(self):
        self.say("xin chào")
        self.say("restart backend")                        # thiếu / hoặc docker: không phải lệnh
        self.assertEqual(self.sent, [])

    def test_injection_in_name_not_allowed(self):
        for cmd in ("/restart backend;reboot", "/restart $(id)", "/restart ../etc"):
            self.sent.clear()
            self.say(cmd)
            self.assertEqual(len(R.pending), 0, cmd)
        self.assertEqual(self.docker_calls, [])


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
