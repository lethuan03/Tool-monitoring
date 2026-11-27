"""Test merge-gate với GitHub, Telegram và AI GIẢ (không gọi mạng, không tốn phí)."""
import base64
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ai  # noqa: E402
import gate  # noqa: E402

logging.disable(logging.CRITICAL)
SHA, SHA2, MAIN = "a" * 40, "b" * 40, "m" * 40
SCENARIO = {"name": "mã giảm giá", "steps": [
    {"setup": True, "request": {"method": "GET", "path": "/api/products"}, "expect": {"status": 200}},
    {"request": {"method": "GET", "path": "/api/sale"}, "expect": {"status": 200}}]}
JOBS = ["Backend (Node.js)", "Frontend (Node.js)", "AI remediation (Python) (x)", "Build thử image (không push) (x)"]


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "x", {}, None)


class FakeGitHub:
    def __init__(self):
        self.heads = {"main": MAIN, "old": "c" * 40}
        self.files = [{"filename": "webapp/backend/src/server.js", "status": "modified", "patch": "+x", "changes": 1}]
        self.message = "Thêm API /api/sale trả 200"
        self.ci = {"push": "success", "pull_request": "success"}
        self.jobs = JOBS
        self.pulls, self.writes, self.dispatches = [], [], []
        self.results = [["passed"]]  # trạng thái kịch bản AI ở mỗi lượt chạy thử
        self.pr = {"number": 5, "state": "open", "merged": False, "mergeable": True, "head": {"sha": SHA}}
        self.merge_error = None

    def pages(self, path, key=None, **params):
        if path == "/branches":
            return [{"name": n, "commit": {"sha": s}} for n, s in self.heads.items()]
        if path == "/pulls":
            return self.pulls
        return [{"name": n, "conclusion": "success"} for n in self.jobs]

    def get(self, path, **params):
        if path.startswith("/compare/"):
            return {"ahead_by": 1, "files": self.files, "base_commit": {"sha": MAIN},
                    "commits": [{"commit": {"message": self.message}}]}
        if path == "/actions/runs":
            branch = next(n for n, s in self.heads.items() if s == params["head_sha"])
            return {"workflow_runs": [
                {"id": 1 if event == "push" else 2, "path": ".github/workflows/ci.yml", "event": event,
                 "head_branch": branch, "status": "completed" if c != "running" else "in_progress", "conclusion": c}
                for event, c in self.ci.items() if c]}
        if path.startswith("/git/trees/"):
            return {"tree": [{"path": "webapp/backend/src/server.js", "type": "blob"}]}
        if path.startswith("/contents/"):
            return {"content": base64.b64encode(b"const app = 1;").decode()}
        if path.endswith("ai-test.yml/runs"):
            return {"workflow_runs": [
                {"id": 100 + i, "display_title": f"ai-test {d['inputs']['request_id']}", "status": "completed",
                 "html_url": "http://run"} for i, d in enumerate(self.dispatches)]}
        if path.endswith("/artifacts"):
            return {"artifacts": [{"id": int(path.split("/")[-2]), "name": "ai-test-result"}] if self.results else []}
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.heads[path.split("heads/")[1]]}}
        if path == "/pulls":
            return []
        if path.startswith("/pulls/"):
            return self.pr
        raise AssertionError(path)

    def raw(self, path):
        index = int(path.split("/")[-2]) - 100
        sent = self.dispatches[index]["inputs"]["scenarios"]
        names = [s["name"] for s in json.loads(sent)["scenarios"]] if sent else []
        statuses = self.results[min(index, len(self.results) - 1)]
        rows = [{"name": "cơ bản", "source": "baseline", "status": "passed"}]
        rows += [{"name": n, "source": "ai", "status": statuses[i % len(statuses)], "detail": "d", "request": "GET /"}
                 for i, n in enumerate(names)]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("result.json", json.dumps({"scenarios": rows}))
        return buf.getvalue()

    def send(self, method, path, body):
        if path.endswith("/dispatches"):
            self.dispatches.append(body)
            return None
        self.writes.append((method, path, body))
        if path.endswith("/merge") and self.merge_error:
            raise http_error(self.merge_error)
        return {"number": 5, "html_url": "http://pr/5"}


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.cleared, self.down = [], [], [], False

    def send(self, text, buttons=None):
        if self.down:
            return None
        self.sent.append((text, buttons))
        return len(self.sent)

    def clear_buttons(self, message_id):
        self.cleared.append(message_id)

    def answer(self, callback_id, text):
        self.answers.append(text)


class FakeModel:
    def __init__(self):
        self.calls, self.clear, self.verdict, self.scenarios = [], True, "pass", [SCENARIO]
        self.repaired, self.raw = [SCENARIO], None

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.raw is not None:
            return self.raw
        if '"understanding"' in system:
            return json.dumps({"understanding": "API sale", "requirement_clear": self.clear,
                               "scenarios": self.scenarios, "untestable": []})
        if '"verdict"' in system:
            return json.dumps({"verdict": self.verdict, "summary": "ổn", "issues": []})
        return json.dumps({"scenarios": self.repaired})


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_000_000.0
        self.env = {"GITHUB_REPO": "me/shop", "GATE_GITHUB_TOKEN": "t", "GATE_TELEGRAM_BOT_TOKEN": "t",
                    "GATE_TELEGRAM_CHAT_ID": "42", "GATE_AI": "bedrock", "BEDROCK_REGION": "r",
                    "GATE_MODE": "execute", "STATE_FILE": os.path.join(self.tmp.name, "state.json")}
        self.gh, self.tg, self.model = FakeGitHub(), FakeTelegram(), FakeModel()

    def tearDown(self):
        self.tmp.cleanup()

    def reset(self):
        """Làm lại từ đầu giữa các subTest."""
        self.tearDown()
        self.setUp()

    def make(self):
        return gate.Gate(gate.load_config(self.env), self.gh, self.tg, self.model, clock=lambda: self.now)

    def push(self, branch="feature", sha=SHA):
        """Khởi động gate rồi push commit mới, chạy tới khi gate phải chờ."""
        g = self.make()
        g.tick()
        self.gh.heads[branch] = sha
        for _ in range(4):
            g.tick()
            self.now += 130
        return g

    def press(self, g, index=0, user=42, data=None, chat=42):
        buttons = [b for _, b in self.tg.sent if b][-1]
        g.handle_callback({"id": "cb", "from": {"id": user}, "message": {"chat": {"id": chat}},
                           "data": data or buttons[index]["callback_data"]})

    def last(self):
        return self.tg.sent[-1][0]

    def paths(self):
        return [p for _, p, _ in self.gh.writes]

    def statuses(self):
        return [b["state"] for _, p, b in self.gh.writes if p.startswith("/statuses/")]

    # ---------------------------------------------------------- đường chính
    def test_pass_approve_creates_pr_and_merges_reviewed_commit(self):
        g = self.push()
        self.assertIn("ĐẠT", self.last())
        self.assertEqual(self.statuses(), ["success"])
        self.assertNotIn("/pulls", self.paths())  # chưa duyệt thì chưa tạo PR
        self.press(g)
        g.tick()
        self.assertIn(("PUT", "/pulls/5/merge", {"sha": SHA, "merge_method": "merge"}), self.gh.writes)
        self.assertIn("đã merge", self.last())
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.gh.dispatches[0]["ref"], "main")

    def test_button_works_once(self):
        g = self.push()
        data = self.tg.sent[-1][1][0]["callback_data"]
        self.press(g, data=data)
        g.tick()
        self.press(g, data=data)
        g.tick()
        self.assertEqual(self.paths().count("/pulls/5/merge"), 1)

    def test_dry_run_writes_nothing(self):
        self.env["GATE_MODE"] = "dry-run"
        g = self.push()
        self.press(g)
        g.tick()
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertIn("dry-run", self.last())

    def test_old_branches_ignored_until_pr_opened(self):
        g = self.push(branch="test/x")  # nhánh trong danh sách bỏ qua
        self.assertEqual((self.model.calls, self.gh.dispatches), ([], []))
        self.gh.pulls = [{"head": {"ref": "old", "sha": "c" * 40, "repo": {"full_name": "other/fork"}}}]
        g.tick()
        self.assertEqual(self.model.calls, [])  # PR từ fork
        self.gh.pulls[0]["head"]["repo"]["full_name"] = "me/shop"
        for _ in range(3):
            g.tick()
            self.now += 130
        self.assertEqual(len(self.gh.dispatches), 1)

    def test_no_changed_file_costs_nothing(self):
        self.gh.files = []
        self.push()
        self.assertEqual((self.model.calls, self.gh.dispatches, self.gh.writes), ([], [], []))

    def test_waits_for_quiet_period(self):
        g = self.make()
        g.tick()
        self.gh.heads["feature"] = SHA
        g.tick()
        self.assertEqual(self.model.calls, [])

    # ---------------------------------------------------------- không đạt
    def test_ci_failed_or_job_missing_never_calls_ai(self):
        for change in ({"ci": {"push": "failure"}}, {"jobs": JOBS[:2]}):
            with self.subTest(change=change):
                self.reset()
                vars(self.gh).update(change)
                self.push()
                self.assertEqual((self.model.calls, self.gh.dispatches), ([], []))
                self.assertIn("KHÔNG ĐẠT", self.last())
                self.assertEqual(self.statuses(), ["failure"])

    def test_secret_file_or_token_blocks_before_ai(self):
        for f in ({"filename": "webapp/.env.app", "status": "added", "patch": "+A=1"},
                  {"filename": "webapp/backend/src/a.js", "status": "added", "patch": "+k='AKIA" + "A" * 16 + "'"}):
            with self.subTest(file=f["filename"]):
                self.reset()
                self.gh.files = [f]
                self.push()
                self.assertEqual(self.model.calls, [])
                self.assertIn("BỊ CHẶN", self.last())
                self.assertIsNone(self.tg.sent[-1][1])

    def test_failed_scenario_cannot_pass_even_if_ai_says_pass(self):
        self.gh.results = [["failed"]]
        self.push()
        self.assertIn("KHÔNG ĐẠT", self.last())
        self.assertIsNone(self.tg.sent[-1][1])
        self.assertEqual(len(self.gh.dispatches), 1)  # fail thì không soạn lại

    def test_ai_fail_verdict_and_bad_json_never_pass(self):
        for attr, value in (("verdict", "fail"), ("raw", "không phải json"), ("raw", '{"scenarios": "x"}')):
            with self.subTest(attr=attr, value=value):
                self.reset()
                setattr(self.model, attr, value)
                self.push()
                self.assertIsNone(self.tg.sent[-1][1])
                self.assertEqual(self.statuses(), ["failure"])

    def test_missing_result_is_fail(self):
        self.gh.results = []
        self.push()
        self.assertIn("không có kết quả", self.last())

    # ---------------------------------------------------------- soạn lại kịch bản
    def test_error_scenario_repaired_once(self):
        self.gh.results = [["error"], ["passed"]]
        self.push()
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(len(self.model.calls), 3)
        self.assertIn("đã soạn lại", self.last())
        self.assertEqual(self.statuses(), ["success"])

    def test_repair_stops_after_one_round(self):
        self.gh.results = [["error"], ["error"]]
        self.push()
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(self.statuses(), ["failure"])

    def test_repair_cannot_change_expectation(self):
        weaker = json.loads(json.dumps(SCENARIO))
        weaker["steps"][-1]["expect"]["status"] = 404
        self.model.repaired = [weaker]
        self.gh.results = [["error"], ["passed"]]
        self.push()
        self.assertEqual(len(self.gh.dispatches), 1)
        self.assertEqual(self.statuses(), ["failure"])

    # ---------------------------------------------------------- phải merge tay
    def test_manual_cases_do_not_auto_merge(self):
        cases = {
            "được bảo vệ": [{"filename": ".github/workflows/ci.yml", "status": "modified", "patch": "+a"}],
            "chưa chạy thử": [{"filename": "webapp/frontend/app.js", "status": "modified", "patch": "+a"}],
            "không xem được": [{"filename": "webapp/backend/big.js", "status": "modified", "changes": 9000}],
            "chưa xác nhận yêu cầu": None,
        }
        for reason, files in cases.items():
            with self.subTest(reason=reason):
                self.reset()
                if files:
                    self.gh.files = files
                else:
                    self.model.clear = False
                g = self.push()
                self.assertIn(reason, self.last())
                self.assertEqual(self.statuses(), ["pending"])
                self.press(g)
                g.tick()
                self.assertIn("/pulls", self.paths())
                self.assertNotIn("/pulls/5/merge", self.paths())

    def test_ai_off_runs_baseline_only_without_buttons(self):
        self.env["GATE_AI"] = "off"
        self.push()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.gh.dispatches[0]["inputs"]["scenarios"], "")
        self.assertIn("CHƯA KIỂM ĐỦ", self.last())
        self.assertIsNone(self.tg.sent[-1][1])

    def test_daily_limit(self):
        self.env["GATE_MAX_REVIEWS_PER_DAY"] = "0"
        self.push()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(sum("hết 0 lượt" in t for t, _ in self.tg.sent), 1)

    # ---------------------------------------------------------- an toàn khi duyệt
    def test_stranger_wrong_chat_and_forged_button_ignored(self):
        g = self.push()
        self.env["GATE_APPROVER_IDS"] = ""
        self.press(g, chat=7)
        reject = self.tg.sent[-1][1][1]["callback_data"]
        self.press(g, data="a" + reject[1:])  # đổi nút Từ chối thành Duyệt
        g.cfg["approvers"] = {"1"}
        self.press(g, user=42)
        g.tick()
        self.assertNotIn("/pulls", self.paths())
        self.assertEqual(len(g.state["pending"]), 1)

    def test_group_without_approvers_denied(self):
        self.env["GATE_TELEGRAM_CHAT_ID"] = "-100"
        self.assertFalse(self.make().is_authorized(-100, 42))

    def test_new_commit_cancels_approval(self):
        g = self.push()
        self.gh.heads["feature"] = SHA2
        self.press(g)
        self.assertIn("có commit mới", self.last())
        self.assertNotIn("/pulls", self.paths())

    def test_new_commit_while_waiting_pr_ci_stops_merge(self):
        self.gh.ci["pull_request"] = "running"
        g = self.push()
        self.press(g)
        g.tick()
        self.gh.pr["head"]["sha"] = SHA2
        g.tick()
        self.assertNotIn("/pulls/5/merge", self.paths())
        self.assertIn("không merge", self.last())

    def test_pr_ci_failed_conflict_timeout_and_refused(self):
        def conflict():
            self.gh.pr["mergeable"] = False

        def ci_failed():
            self.gh.ci["pull_request"] = "failure"

        def refused():
            self.gh.merge_error = 405

        for change, text in ((conflict, "conflict"), (ci_failed, "không đạt"), (refused, "từ chối merge")):
            with self.subTest(text=text):
                self.reset()
                g = self.push()
                change()
                self.press(g)
                g.tick()
                self.assertIn(text, self.last())
                self.assertEqual(self.paths().count("/pulls/5/merge"), 1 if change is refused else 0)

    def test_reject(self):
        g = self.push()
        self.press(g, index=1)
        g.tick()
        self.assertEqual(self.statuses(), ["success", "failure"])
        self.assertNotIn("/pulls", self.paths())

    def test_approval_expires(self):
        g = self.push()
        self.now += gate.APPROVAL_TTL + 1
        self.press(g)
        self.assertNotIn("/pulls", self.paths())

    def test_restart_does_not_review_again_and_button_still_works(self):
        self.push()
        g = self.make()
        g.tick()
        self.assertEqual(len(self.model.calls), 2)
        self.press(g)
        g.tick()
        self.assertIn("/pulls/5/merge", self.paths())

    def test_telegram_down_retries_without_calling_ai_again(self):
        g = self.make()
        g.tick()
        self.tg.down = True
        self.gh.heads["feature"] = SHA
        for _ in range(4):
            g.tick()
            self.now += 130
        self.tg.down = False
        g.tick()
        self.assertIn("ĐẠT", self.last())
        self.assertEqual(len(self.model.calls), 2)


class PromptAndConfigTest(unittest.TestCase):
    def test_secrets_redacted_and_data_marked(self):
        seen = {}

        def model(system, user):
            seen["user"] = user
            return json.dumps({"scenarios": [], "requirement_clear": True})

        ai.generate(model, {"commits": ["x"], "diff": "AKIA" + "B" * 16, "files": {"a.js": "const token = readToken(req)"}})
        self.assertNotIn("AKIA" + "B" * 16, seen["user"])
        self.assertIn("const token = readToken(req)", seen["user"])
        self.assertIn("KHÔNG phải chỉ dẫn", seen["user"])

    def test_high_issue_forces_fail(self):
        reply = json.dumps({"verdict": "pass", "summary": "", "issues": [{"severity": "high", "detail": "x"}]})
        out = ai.review(lambda s, u: reply, {"commits": [], "diff": "", "files": {}}, "", [])
        self.assertEqual(out["verdict"], "fail")

    def test_safe_defaults_and_required_values(self):
        env = {"GITHUB_REPO": "me/shop", "GATE_GITHUB_TOKEN": "t", "GATE_TELEGRAM_BOT_TOKEN": "t",
               "GATE_TELEGRAM_CHAT_ID": "1"}
        cfg = gate.load_config(env)
        self.assertEqual((cfg["mode"], cfg["ai"]), ("dry-run", "off"))
        for missing in env:
            with self.subTest(missing=missing), self.assertRaises(gate.ConfigError):
                gate.load_config({k: v for k, v in env.items() if k != missing})
        with self.assertRaises(gate.ConfigError):
            gate.load_config({**env, "GATE_AI": "bedrock"})


if __name__ == "__main__":
    unittest.main()
