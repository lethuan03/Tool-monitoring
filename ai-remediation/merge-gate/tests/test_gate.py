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
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ai  # noqa: E402
import chat  # noqa: E402
import gate  # noqa: E402

logging.disable(logging.CRITICAL)
SHA, SHA2, MAIN = "a" * 40, "b" * 40, "m" * 40
SCENARIO = {"name": "mã giảm giá", "steps": [
    {"setup": True, "request": {"method": "GET", "path": "/api/products"}, "expect": {"status": 200}},
    {"request": {"method": "GET", "path": "/api/sale"}, "expect": {"status": 200}}]}
JOBS = ["Backend (Node.js)", "Frontend (Node.js)", "AI remediation (Python) (x)", "Build thử image (không push) (x)"]
ROUTE = "webapp/backend/src/routes/products.js"
REPO_FILES = {
    "webapp/backend/src/server.js": "const p = require('./routes/products');\napp.use('/api/products', p);\n",
    ROUTE: "const router = r();\nrouter.get('/sale', h);\nmodule.exports = router;\n",
    "webapp/features/products.md": f"# Sản phẩm\n\nFiles: {ROUTE}\n\nQUY-TAC-SAN-PHAM\n",
}


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "x", {}, None)


class FakeGitHub:
    def __init__(self):
        self.heads = {"main": MAIN, "old": "c" * 40}
        self.files = [{"filename": ROUTE, "status": "modified", "patch": "@@ -2 +2 @@\n+x", "changes": 1}]
        self.message = "Thêm API /api/products/sale trả 200"
        self.ci = {"push": "success", "pull_request": "success"}
        self.jobs = JOBS
        self.pulls, self.closed, self.writes, self.dispatches, self.reads = [], [], [], [], []
        self.results = [["passed"]]  # trạng thái kịch bản MỚI ở mỗi lượt chạy thử
        self.saved_status, self.baseline_status, self.no_result = "passed", "passed", False
        self.pr = {"number": 5, "state": "open", "merged": False, "mergeable": True, "head": {"sha": SHA}}
        self.merge_error = None

    def open_pr(self, branch, sha, number=5, draft=False, repo="me/shop"):
        self.pulls = [p for p in self.pulls if p["head"]["ref"] != branch] + [
            {"number": number, "draft": draft, "html_url": f"http://pr/{number}",
             "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo}}}]
        self.pr = {"number": number, "state": "open", "merged": False, "mergeable": True, "head": {"sha": sha}}

    def close_pr(self, branch, merged):
        for pr in [p for p in self.pulls if p["head"]["ref"] == branch]:
            self.pulls.remove(pr)
            self.closed.append({**pr, "merged_at": "2026-01-01" if merged else None})
        self.pr = {**self.pr, "state": "closed", "merged": merged}

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
            return {"tree": [{"path": p, "type": "blob"} for p in REPO_FILES]}
        if path.startswith("/contents/"):
            name = urllib.parse.unquote(path[len("/contents/"):])
            self.reads.append((name, params["ref"]))
            return {"content": base64.b64encode(REPO_FILES[name].encode()).decode()}
        if path.endswith("ai-test.yml/runs"):
            return {"workflow_runs": [
                {"id": 100 + i, "display_title": f"ai-test {d['inputs']['request_id']}", "status": "completed",
                 "html_url": "http://run"} for i, d in enumerate(self.dispatches)]}
        if path.endswith("/artifacts"):
            return {"artifacts": [] if self.no_result else [{"id": int(path.split("/")[-2]), "name": "ai-test-result"}]}
        if path.startswith("/git/ref/heads/"):
            return {"object": {"sha": self.heads[path.split("heads/")[1]]}}
        if path == "/pulls":
            found = self.closed if params["state"] == "closed" else self.pulls
            return [p for p in found if f"me:{p['head']['ref']}" == params["head"]]
        if path.startswith("/pulls/"):
            return self.pr
        raise AssertionError(path)

    def sent(self, index=-1):
        text = self.dispatches[index]["inputs"]["scenarios"]
        return json.loads(text)["scenarios"] if text else []

    def raw(self, path):
        index = int(path.split("/")[-2]) - 100
        statuses = self.results[min(index, len(self.results) - 1)]
        rows = [{"name": "cơ bản", "source": "baseline", "status": self.baseline_status}]
        fresh = 0
        for sc in self.sent(index):
            if sc.get("source") == "saved":
                rows.append({"name": sc["name"], "source": "saved", "status": self.saved_status})
            else:
                rows.append({"name": sc["name"], "source": "ai", "status": statuses[fresh % len(statuses)],
                             "detail": "CHI-TIET-LOI", "request": "GET /api/sale", "step": 2, "actual_status": 500})
                fresh += 1
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
        if path.endswith("/merge"):
            for pr in [p for p in self.pulls if f"/pulls/{p['number']}/merge" == path]:
                self.close_pr(pr["head"]["ref"], merged=True)
        if path == "/pulls":
            self.open_pr(body["head"], self.heads[body["head"]])
        return {"number": 5, "html_url": "http://pr/5"}


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers, self.cleared, self.down = [], [], [], [], False

    def send(self, text, buttons=None):
        if self.down:
            return None
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit(self, message_id, text):
        self.edits.append((message_id, text))

    def clear_buttons(self, message_id):
        self.cleared.append(message_id)

    def answer(self, callback_id, text):
        self.answers.append(text)


class FakeModel:
    def __init__(self):
        self.calls, self.clear, self.verdict, self.scenarios = [], True, "pass", [SCENARIO]
        self.repaired, self.raw, self.cause, self.issues = [SCENARIO], None, "code", []
        self.action = None

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.raw is not None:
            return self.raw
        if '"understanding"' in system:
            return json.dumps({"understanding": "API sale", "requirement_clear": self.clear,
                               "scenarios": self.scenarios, "untestable": []})
        if '"verdict"' in system:
            suspects = [{"scenario": sc["name"], "cause": self.cause, "why": "vì"} for sc in self.scenarios]
            return json.dumps({"verdict": self.verdict, "summary": "ổn", "issues": self.issues, "suspects": suspects})
        if '"reply"' in system:
            return json.dumps({"reply": "Trả lời <b>", "action": self.action})
        return json.dumps({"scenarios": self.repaired})


class Base(unittest.TestCase):
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

    def run_ticks(self, g, count=4):
        for _ in range(count):
            g.tick()
            self.now += 130

    def offer(self, g=None, branch="feature", sha=SHA, number=5):
        """Push commit mới và mở PR, chạy tới thẻ xin phép kiểm."""
        if g is None:
            g = self.make()
            g.tick()
        self.gh.heads[branch] = sha
        self.gh.open_pr(branch, sha, number)
        self.run_ticks(g, 3)
        return g

    def push(self, **kw):
        """Push rồi cho phép kiểm, chạy tới khi có kết quả."""
        g = self.offer(**kw)
        if self.has("t"):
            self.press(g, "t")
            self.run_ticks(g)
        return g

    def cards(self):
        return [b for _, b in self.tg.sent if b]

    def kinds(self):
        return [b["callback_data"].split(":")[0] for b in self.cards()[-1]] if self.cards() else []

    def has(self, kind):
        return kind in self.kinds()

    def data(self, kind):
        return next(b["callback_data"] for b in self.cards()[-1] if b["callback_data"].startswith(kind + ":"))

    def press(self, g, kind="a", user=42, data=None, chat_id=42):
        g.handle_callback({"id": "cb", "from": {"id": user}, "message": {"chat": {"id": chat_id}},
                           "data": data or self.data(kind)})

    def say(self, g, text, user=42, chat_id=42):
        g.handle_message({"text": text, "from": {"id": user}, "chat": {"id": chat_id}})

    def last(self):
        return self.tg.sent[-1][0]

    def result_text(self):
        return [t for t, b in self.tg.sent if "Chạy thử:" in t or "<b>KHÔNG" in t or "<b>BỊ CHẶN" in t][-1]

    def paths(self):
        return [p for _, p, _ in self.gh.writes]

    def statuses(self):
        return [b["state"] for _, p, b in self.gh.writes if p.startswith("/statuses/")]


class FlowTest(Base):
    def test_nothing_runs_before_permission(self):
        self.offer()
        self.assertEqual((self.model.calls, self.gh.dispatches, self.gh.writes), ([], [], []))
        self.assertEqual(self.kinds(), ["t", "x"])
        card = self.last()
        for text in ("Xin phép kiểm", "GET /api/products/sale", "Sản phẩm", "tối đa 3 lượt"):
            self.assertIn(text, card)

    def test_skip(self):
        g = self.offer()
        self.press(g, "x")
        self.run_ticks(g)
        self.assertEqual((self.model.calls, self.gh.dispatches), ([], []))

    def test_pass_approve_merges_reviewed_commit_of_the_pr(self):
        g = self.push()
        self.assertIn("ĐẠT", self.result_text())
        self.assertIn("PR #5", self.result_text())
        self.assertEqual(self.statuses(), ["success"])
        self.assertNotIn("/pulls/5/merge", self.paths())  # chưa duyệt thì chưa merge
        self.press(g)
        g.tick()
        self.assertIn(("PUT", "/pulls/5/merge", {"sha": SHA, "merge_method": "merge"}), self.gh.writes)
        self.assertIn("đã merge", self.last())
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.gh.dispatches[0]["ref"], "main")
        self.assertNotIn("/pulls", self.paths())  # dùng PR có sẵn, không tạo thêm
        count = len(self.tg.sent)
        self.run_ticks(g)
        self.assertEqual([t for t, b in self.tg.sent[count:] if b], [])  # merge xong không hỏi lại

    def test_progress_message_is_edited(self):
        self.push()
        progress = [t for t, _ in self.tg.sent if t.startswith("🔄")]
        self.assertEqual(len(progress), 1)
        texts = [t for _, t in self.tg.edits]
        self.assertTrue(any("⏳ Chạy thử" in t for t in texts))
        self.assertIn("Kết quả: <b>ĐẠT</b>", texts[-1])

    def test_only_related_code_and_main_feature_docs_sent_to_ai(self):
        self.push()
        prompt = self.model.calls[0][1]
        self.assertIn("QUY-TAC-SAN-PHAM", prompt)
        self.assertIn("GET /api/products/sale", prompt)
        self.assertIn(("webapp/features/products.md", MAIN), self.gh.reads)
        self.assertNotIn(("webapp/features/products.md", SHA), self.gh.reads)

    def test_buttons_work_once(self):
        g = self.offer()
        allow = self.data("t")
        self.press(g, data=allow)
        self.press(g, data=allow)
        self.run_ticks(g)
        self.assertEqual(len(self.gh.dispatches), 1)
        merge = self.data("a")
        self.press(g, data=merge)
        g.tick()
        self.press(g, data=merge)
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

    def test_quiet_period_and_no_changed_file(self):
        g = self.make()
        g.tick()
        self.gh.heads["feature"] = SHA
        self.gh.open_pr("feature", SHA)
        g.tick()
        self.assertEqual(self.cards(), [])
        self.reset()
        self.gh.files = []
        self.offer()
        self.assertEqual(self.cards(), [])

    def test_ci_failed_or_job_missing_never_offers_test(self):
        for change in ({"ci": {"push": "success", "pull_request": "failure"}}, {"jobs": JOBS[:2]}):
            with self.subTest(change=change):
                self.reset()
                vars(self.gh).update(change)
                self.offer()
                self.assertEqual((self.cards(), self.model.calls), ([], []))
                self.assertIn("KHÔNG ĐẠT", self.last())
                self.assertEqual(self.statuses(), ["failure"])

    def test_secret_file_or_token_blocks_before_ai(self):
        for f in ({"filename": "webapp/.env.app", "status": "added", "patch": "+A=1"},
                  {"filename": "webapp/backend/src/a.js", "status": "added", "patch": "+k='AKIA" + "A" * 16 + "'"}):
            with self.subTest(file=f["filename"]):
                self.reset()
                self.gh.files = [f]
                self.offer()
                self.assertEqual((self.cards(), self.model.calls), ([], []))
                self.assertIn("BỊ CHẶN", self.last())

    def test_ai_off_runs_without_ai(self):
        self.env["GATE_AI"] = "off"
        self.push()
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.gh.dispatches[0]["inputs"]["scenarios"], "")
        self.assertIn("CHƯA KIỂM ĐỦ", self.result_text())
        self.assertNotIn("a", self.kinds())  # không có nút merge
        self.assertEqual(self.statuses(), ["pending"])

    def test_daily_limit(self):
        self.env["GATE_MAX_REVIEWS_PER_DAY"] = "0"
        self.push()
        self.assertEqual((self.model.calls, self.gh.dispatches), ([], []))
        self.assertIn("hết 0 lần", self.last())


class ResultTest(Base):
    def test_failed_scenario_blamed_on_code_has_no_merge_button(self):
        self.gh.results = [["failed"]]
        g = self.push()
        self.assertIn("KHÔNG ĐẠT", self.result_text())
        self.assertEqual(self.kinds(), ["n", "d"])
        self.assertEqual(len(self.gh.dispatches), 1)  # fail thì không tự soạn lại
        self.press(g, "d")
        self.assertIn("CHI-TIET-LOI", self.last())
        self.assertIn("lỗi ở <b>code</b>", self.last())
        self.assertEqual(len(g.state["pending"]), 1)  # xem chi tiết không mất nút

    def test_suspected_test_error_allows_manual_pr_only(self):
        self.gh.results, self.model.cause, self.model.verdict = [["failed"]], "test", "fail"
        g = self.push()
        self.assertIn("nghi do kịch bản", self.result_text())
        self.assertEqual(self.kinds(), ["n", "d", "r"])  # PR đã có: chỉ merge tay trên GitHub
        self.assertIn("merge tay trên GitHub", self.result_text())
        self.assertEqual(self.statuses(), ["pending"])
        self.gh.close_pr("feature", merged=True)
        self.run_ticks(g, 2)
        self.assertEqual(g.state["saved"], [])  # kịch bản không đạt không được lưu

    def test_not_suspect_when_other_evidence_points_to_code(self):
        cases = {"cơ bản fail": {"baseline_status": "failed"}}
        for label, change in cases.items():
            with self.subTest(label):
                self.reset()
                self.gh.results, self.model.cause = [["failed"]], "test"
                vars(self.gh).update(change)
                self.push()
                self.assertNotIn("a", self.kinds())
        self.reset()
        self.gh.results, self.model.cause = [["failed"]], "test"
        self.model.issues = [{"severity": "high", "detail": "sql"}]
        self.push()
        self.assertNotIn("a", self.kinds())

    def test_retest_sends_previous_errors_and_is_limited(self):
        self.env["GATE_MAX_RETESTS"] = "1"
        self.gh.results = [["failed"], ["failed"]]
        g = self.push()
        self.press(g, "n")
        self.run_ticks(g)
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertIn("CHI-TIET-LOI", self.model.calls[2][1])
        self.assertEqual(self.kinds(), ["d"])

    def test_ai_fail_verdict_and_bad_json_never_pass(self):
        for attr, value in (("verdict", "fail"), ("raw", "không phải json"), ("raw", '{"scenarios": "x"}')):
            with self.subTest(attr=attr, value=value):
                self.reset()
                setattr(self.model, attr, value)
                self.push()
                self.assertNotIn("a", self.kinds())
                self.assertEqual(self.statuses(), ["failure"])

    def test_missing_result_is_fail(self):
        self.gh.no_result = True
        self.push()
        self.assertIn("không có kết quả", self.last())
        self.assertNotIn("a", self.kinds())

    def test_error_scenario_repaired_once(self):
        self.gh.results = [["error"], ["passed"]]
        g = self.push()
        self.assertEqual(len(self.gh.dispatches), 2)
        self.assertEqual(len(self.model.calls), 3)
        self.assertIn("đã soạn lại", self.result_text())
        self.assertEqual(self.statuses(), ["success"])
        self.press(g)
        self.run_ticks(g, 2)
        self.assertEqual(len(g.state["saved"]), 1)

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
                self.assertIn(reason, self.result_text())
                self.assertEqual(self.statuses(), ["pending"])
                self.assertNotIn("a", self.kinds())
                self.assertIn("merge tay trên GitHub", self.result_text())
                g.tick()
                self.assertNotIn("/pulls/5/merge", self.paths())

    def test_changing_feature_docs_is_warned(self):
        self.gh.files.append({"filename": "webapp/features/products.md", "status": "modified", "patch": "+a"})
        self.offer()
        self.assertIn("sửa mô tả chức năng", self.last())


class SavedTest(Base):
    def approve_first(self):
        g = self.push()
        self.press(g)
        self.run_ticks(g, 2)  # merge rồi tick sau mới lưu
        return g

    def test_saved_only_after_merge(self):
        self.env["GATE_MODE"] = "dry-run"
        g = self.push()
        self.press(g)
        self.run_ticks(g)
        self.assertEqual((g.state["saved"], len(g.state["waiting"])), ([], 1))  # duyệt chưa phải merge
        self.gh.close_pr("feature", merged=True)  # người merge tay trên GitHub
        self.run_ticks(g, 2)
        self.assertEqual((len(g.state["saved"]), g.state["waiting"]), (1, []))
        self.assertTrue(any("lưu 1 kịch bản" in t for t, _ in self.tg.sent))

    def test_not_saved_when_closed_other_commit_rejected_or_expired(self):
        for case in ("closed", "other", "reject", "expired"):
            with self.subTest(case):
                self.reset()
                g = self.push()
                if case == "closed":
                    self.gh.close_pr("feature", merged=False)
                elif case == "other":  # PR được merge nhưng ở commit khác commit đã kiểm
                    self.gh.open_pr("feature", SHA2)
                    self.gh.close_pr("feature", merged=True)
                elif case == "reject":
                    self.press(g, "r")
                    self.gh.close_pr("feature", merged=True)
                else:
                    self.now += gate.WAITING_TTL + 1
                self.run_ticks(g, 2)
                self.assertEqual(g.state["saved"], [])

    def test_merged_scenarios_rerun_next_time(self):
        g = self.approve_first()
        self.assertEqual([s["features"] for s in g.state["saved"]], [["Sản phẩm"]])
        self.model.scenarios = []
        self.offer(g, branch="next", sha=SHA2, number=6)
        self.assertIn("1 kịch bản đã lưu", self.last())
        self.press(g, "t")
        self.run_ticks(g)
        sent = self.gh.sent()
        self.assertEqual([(s["name"], s["source"]) for s in sent], [("[cũ] mã giảm giá", "saved")])

    def test_broken_saved_scenario_blocks_until_user_drops_it(self):
        g = self.approve_first()
        self.model.scenarios = [{**SCENARIO, "name": "mới", "steps": SCENARIO["steps"][1:]}]
        self.model.cause, self.gh.saved_status = "test", "failed"
        self.offer(g, branch="next", sha=SHA2, number=6)
        self.press(g, "t")
        self.run_ticks(g)
        self.assertNotIn("a", self.kinds())
        self.gh.saved_status = "passed"
        self.press(g, "z")
        self.run_ticks(g)
        self.assertEqual(g.state["saved"], [])
        self.assertNotIn("saved", [s.get("source") for s in self.gh.sent()])

    def test_ai_cannot_mark_its_scenario_as_saved(self):
        self.model.scenarios = [{**SCENARIO, "source": "saved", "name": "[cũ] giả"}]
        self.push()
        self.assertEqual([(s.get("source"), s["name"]) for s in self.gh.sent()], [(None, "giả")])


class RequestTest(Base):
    def started(self):
        g = self.make()
        g.tick()
        return g

    def test_push_without_pr_does_nothing(self):
        g = self.started()
        self.gh.heads["feature"] = SHA
        self.run_ticks(g)
        self.assertEqual((self.cards(), self.model.calls, self.gh.dispatches), ([], [], []))
        self.gh.open_pr("feature", SHA)
        self.run_ticks(g)
        self.assertEqual(self.kinds(), ["t", "x"])
        self.assertIn("PR #5", self.last())

    def test_fork_draft_and_ignored_branch(self):
        g = self.started()
        self.gh.heads.update({"feature": SHA, "test/x": SHA2})
        self.gh.open_pr("feature", SHA, repo="other/fork")
        self.gh.open_pr("test/x", SHA2, number=6)
        self.run_ticks(g)
        self.gh.open_pr("feature", SHA, draft=True)
        self.run_ticks(g)
        self.assertEqual(self.cards(), [])
        self.gh.open_pr("feature", SHA)  # chuyển sang sẵn sàng
        self.run_ticks(g)
        self.assertEqual(self.kinds(), ["t", "x"])

    def test_existing_pr_on_first_run_is_offered(self):
        self.gh.open_pr("old", "c" * 40)
        g = self.started()
        self.run_ticks(g)
        self.assertEqual(self.kinds(), ["t", "x"])

    def test_same_pr_asked_once_new_commit_asked_again(self):
        g = self.offer()
        self.press(g, "x")
        self.run_ticks(g)
        self.assertEqual(len(self.cards()), 1)
        self.offer(g, sha=SHA2)
        self.assertEqual(len(self.cards()), 2)

    def test_closed_pr_cancels_and_reopen_asks_again(self):
        g = self.offer()
        allow = self.data("t")
        self.gh.close_pr("feature", merged=False)
        self.run_ticks(g)
        self.assertIn("PR #5 đã đóng", self.last())
        self.press(g, data=allow)
        self.assertEqual((g.state["pending"], self.gh.dispatches), ({}, []))
        self.gh.open_pr("feature", SHA)
        self.run_ticks(g)
        self.assertEqual(len(self.cards()), 2)

    def test_pr_closed_right_before_approval_is_not_recreated(self):
        g = self.push()
        self.gh.close_pr("feature", merged=False)
        self.press(g)
        self.assertIn("không còn mở", self.last())
        self.assertEqual([p for p in self.paths() if p.startswith("/pulls")], [])

    def test_command_flow_without_pr_creates_it(self):
        g = self.started()
        self.say(g, "/test old")
        self.press(g, "t")
        self.run_ticks(g)
        self.press(g)
        self.run_ticks(g, 3)
        self.assertIn("/pulls", self.paths())
        self.assertIn("/pulls/5/merge", self.paths())
        self.assertEqual(len(g.state["saved"]), 1)

    def test_command_flow_manual_case_creates_pr_only(self):
        self.model.clear = False
        g = self.started()
        self.say(g, "/test old")
        self.press(g, "t")
        self.run_ticks(g)
        self.press(g)
        self.run_ticks(g, 3)
        self.assertIn("/pulls", self.paths())
        self.assertNotIn("/pulls/5/merge", self.paths())
        self.assertEqual(len(self.cards()), 2)  # PR vừa tạo không bị coi là yêu cầu mới

    def test_pr_opened_after_test_resends_card_without_ai(self):
        g = self.started()
        self.say(g, "/test old")
        self.press(g, "t")
        self.run_ticks(g)
        self.press(g, "r")
        calls = len(self.model.calls)
        self.gh.open_pr("old", "c" * 40)
        self.run_ticks(g)
        self.assertEqual((self.kinds(), len(self.model.calls)), (["a", "r"], calls))
        self.assertIn("PR #5", self.last())

    def test_test_command_keeps_open_pr(self):
        g = self.push()
        self.say(g, "/test feature")
        self.assertEqual(g.state["branches"]["feature"]["pr"], 5)
        self.assertIn("PR #5", self.last())


class SafetyTest(Base):
    def test_stranger_wrong_chat_and_forged_button_ignored(self):
        g = self.offer()
        self.press(g, "t", chat_id=7)
        skip = self.data("x")
        self.press(g, data="t" + skip[1:])  # đổi nút Bỏ qua thành Cho phép
        g.cfg["approvers"] = {"1"}
        self.press(g, "t", user=42)
        self.run_ticks(g)
        self.assertEqual((self.model.calls, self.gh.dispatches), ([], []))
        self.assertEqual(len(g.state["pending"]), 1)

    def test_test_button_cannot_merge(self):
        g = self.offer()
        pid = self.data("t").split(":")[1]
        self.press(g, data=f"a:{pid}:{g.sign(pid, 'a')}")  # chữ ký đúng nhưng thẻ này không có nút merge
        self.assertEqual(self.gh.writes, [])

    def test_group_without_approvers_denied(self):
        self.env["GATE_TELEGRAM_CHAT_ID"] = "-100"
        self.assertFalse(self.make().is_authorized(-100, 42))

    def test_new_commit_cancels_approval(self):
        g = self.push()
        self.gh.heads["feature"] = SHA2
        self.press(g)
        self.assertIn("có commit mới", self.last())
        self.assertNotIn("/pulls/5/merge", self.paths())

    def test_new_commit_while_waiting_pr_ci_stops_merge(self):
        g = self.push()
        self.gh.ci["pull_request"] = "running"
        self.press(g)
        g.tick()
        self.gh.pr["head"]["sha"] = SHA2
        g.tick()
        self.assertNotIn("/pulls/5/merge", self.paths())
        self.assertIn("không merge", self.last())

    def test_pr_ci_failed_conflict_and_refused(self):
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
        self.press(g, "r")
        g.tick()
        self.assertEqual(self.statuses(), ["success", "failure"])
        self.assertNotIn("/pulls/5/merge", self.paths())

    def test_approval_expires(self):
        g = self.push()
        data = self.data("a")
        self.now += gate.APPROVAL_TTL + 1
        self.press(g, data=data)
        self.assertNotIn("/pulls", self.paths())

    def test_restart_keeps_buttons(self):
        self.push()
        g = self.make()
        g.tick()
        self.assertEqual(len(self.model.calls), 2)
        self.press(g)
        g.tick()
        self.assertIn("/pulls/5/merge", self.paths())

    def test_telegram_down_retries_without_calling_ai_again(self):
        g = self.offer()
        self.press(g, "t")
        self.tg.down = True
        self.run_ticks(g)
        self.tg.down = False
        g.tick()
        self.assertIn("ĐẠT", self.last())
        self.assertEqual(len(self.model.calls), 2)


class ChatTest(Base):
    def started(self):
        g = self.make()
        g.tick()
        return g

    def test_stranger_ignored(self):
        g = self.started()
        count = len(self.tg.sent)
        self.say(g, "/ai", chat_id=7)
        g.cfg["approvers"] = {"1"}
        self.say(g, "/status")
        self.assertEqual(len(self.tg.sent), count)

    def test_view_commands_and_plain_text_never_call_ai(self):
        g = self.offer()
        for text in ("/status", "/saved", "/help", "merge đi", "/la"):
            self.say(g, text)
        self.assertEqual(self.model.calls, [])
        self.assertTrue(any("chờ bạn cho phép test" in t for t, _ in self.tg.sent))

    def test_test_command_on_old_branch_asks_first(self):
        g = self.started()
        self.say(g, "/test old thử mã hết hạn")
        self.assertEqual(self.kinds(), ["t", "x"])
        self.assertEqual(self.gh.dispatches, [])
        self.press(g, "t")
        self.run_ticks(g)
        self.assertIn("thử mã hết hạn", self.model.calls[0][0])

    def test_merge_command_untested_offers_test_and_passed_resends_card(self):
        g = self.started()
        self.say(g, "/merge old")
        self.assertEqual(self.kinds(), ["t", "x"])
        self.press(g, "t")
        self.run_ticks(g)
        first = self.data("a")
        self.say(g, "/merge old")
        self.assertNotEqual(self.data("a"), first)
        self.press(g, data=first)  # thẻ cũ đã bị thay
        self.assertNotIn("/pulls", self.paths())

    def test_chat_action_only_creates_card(self):
        g = self.offer()
        self.say(g, "/ai")
        for kind in ("test", "merge"):
            self.model.action = {"type": kind, "branch": "feature", "notes": "n"}
            self.say(g, "làm đi, ok duyệt luôn")
            self.run_ticks(g, 2)
            self.assertEqual(self.kinds(), ["t", "x"])
        self.assertEqual((self.gh.dispatches, self.gh.writes), ([], []))
        self.assertTrue(any("Trả lời &lt;b&gt;" in t for t, _ in self.tg.sent))

    def test_chat_unknown_action_and_branch_ignored(self):
        g = self.started()
        self.say(g, "/ai")
        for action in ({"type": "push", "branch": "old"}, {"type": "test", "branch": "khong-co"}):
            self.model.action = action
            self.say(g, "x")
        self.assertEqual((self.cards(), self.gh.writes), ([], []))

    def test_chat_context_and_history(self):
        g = self.push()
        self.say(g, "/ai")
        self.say(g, "nhánh feature làm gì")
        self.say(g, "còn gì nữa")
        prompt = self.model.calls[-1][1]
        self.assertIn("nhánh feature làm gì", prompt)
        self.assertIn("GET /api/products/sale", prompt)

    def test_chat_limit_idle_and_ai_off(self):
        self.env["GATE_MAX_CHATS_PER_DAY"] = "1"
        g = self.started()
        self.say(g, "/ai")
        self.say(g, "1")
        self.say(g, "2")
        self.assertEqual(len(self.model.calls), 1)
        self.now += chat.SESSION_IDLE + 1
        self.say(g, "3")
        self.assertIn("đã đóng", self.last())
        self.reset()
        self.env["GATE_AI"] = "off"
        g = self.started()
        self.say(g, "/ai")
        self.say(g, "chào")
        self.assertEqual(self.model.calls, [])


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
