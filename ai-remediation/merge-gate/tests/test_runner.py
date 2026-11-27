"""Test bộ chạy kịch bản với app GIẢ trên 127.0.0.1."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import runner  # noqa: E402


class FakeApp(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"ok": True})
        if self.path == "/api/products/7":
            return self.send(200, {"id": 7, "stock": self.server.stock, "price": "10.00"})
        if self.path == "/api/me":
            return self.send(200 if "sid=abc" in (self.headers.get("Cookie") or "") else 401, {})
        if self.path == "/away":
            return self.send(302, headers={"Location": "http://example.invalid/"})
        if self.path == "/boom":
            return self.send(500, {"error": "x"})
        return self.send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/api/login":
            return self.send(200, {}, {"Set-Cookie": "sid=abc; HttpOnly; Path=/; SameSite=Strict"})
        if self.path == "/api/buy":
            self.server.stock -= body["quantity"]
            return self.send(201, {"productId": body["productId"]})
        return self.send(404, {})


def step(method, path, status, **extra):
    request = {"method": method, "path": path}
    if "json" in extra:
        request["json"] = extra.pop("json")
    expect = {"status": status}
    if "checks" in extra:
        expect["json"] = extra.pop("checks")
    return {"request": request, "expect": expect, **extra}


def scenario(*steps, name="sc"):
    return {"name": name, "steps": list(steps)}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApp)
        self.server.stock = 10
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_one(self, *steps):
        return runner.run_scenario(scenario(*steps), self.base, {}, "ai")

    def test_variables_math_and_numeric_strings(self):
        result = self.run_one(
            step("GET", "/api/products/7", 200, save={"id": "id", "stock": "stock"}, setup=True),
            step("POST", "/api/buy", 201, json={"productId": "${id}", "quantity": 2},
                 checks=[{"path": "productId", "equals": "${id}"}]),
            step("GET", "/api/products/${id}", 200,
                 checks=[{"path": "stock", "equals": {"$var": "stock", "add": -2}}, {"path": "price", "equals": 10},
                         {"path": "missing", "exists": False}]))
        self.assertEqual(result["status"], "passed", result)

    def test_dotted_variable(self):
        result = self.run_one(
            step("GET", "/api/products/7", 200, save={"all": ""}, setup=True),
            step("GET", "/api/products/${all.id}", 200, checks=[{"path": "id", "equals": "${all.id}"}]))
        self.assertEqual(result["status"], "passed", result)

    def test_unknown_variable_syntax_is_reported_not_sent(self):
        for path in ("/api/products/${all[0].id}", "/api/products/${missing.0.id}", "/api/products/${"):
            with self.subTest(path=path):
                result = self.run_one(step("GET", path, 200))
                self.assertEqual((result["status"], result["actual_status"]), ("error", None))
                self.assertIn("biến", result["detail"])

    def test_failure_keeps_evidence(self):
        result = self.run_one(step("GET", "/api/products/7", 200, save={"id": "id"}, setup=True),
                              step("POST", "/api/buy", 200, json={"productId": "${id}", "quantity": 1}))
        self.assertEqual((result["request"], result["actual_status"]), ("POST /api/buy", 201))
        self.assertIn('"productId": 7', result["body"])
        self.assertIn('"status": 200', result["expected"])

    def test_saved_source_is_kept(self):
        sc = {**scenario(step("GET", "/health", 200)), "source": "saved"}
        self.assertEqual(runner.run([("ai", [sc])], self.base, {})["scenarios"][0]["source"], "saved")

    def test_cookie_kept_inside_scenario_only(self):
        self.assertEqual(self.run_one(step("POST", "/api/login", 200), step("GET", "/api/me", 200))["status"], "passed")
        self.assertEqual(self.run_one(step("GET", "/api/me", 401))["status"], "passed")

    def test_redirect_not_followed(self):
        self.assertEqual(self.run_one(step("GET", "/away", 302))["status"], "passed")

    def test_wrong_expectation_is_failed(self):
        result = self.run_one(step("GET", "/api/products/7", 200, checks=[{"path": "stock", "equals": 99}]))
        self.assertEqual((result["status"], result["step"]), ("failed", 1))
        self.assertIn("99", result["detail"])

    def test_setup_problem_is_error_but_server_crash_is_failed(self):
        last = step("GET", "/health", 200)
        self.assertEqual(self.run_one(step("GET", "/nope", 200, setup=True), last)["status"], "error")
        self.assertEqual(self.run_one(step("GET", "/boom", 200, setup=True), last)["status"], "failed")
        self.assertEqual(self.run_one(step("GET", "/x/${chua_co}", 200))["status"], "error")

    def test_variable_cannot_redirect_to_other_host(self):
        sc = scenario(step("GET", "${evil}", 200))
        sc["steps"][0]["request"]["path"] = "/ok"
        sc["steps"][0]["request"]["path"] = "/${evil}"
        result = runner.run_scenario(sc, self.base, {"evil": "/example.invalid/x"}, "ai")
        self.assertEqual(result["status"], "error")

    def test_validate_rejects_bad_input(self):
        good = step("GET", "/a", 200)
        bad_steps = [
            step("GET", "http://example.invalid/", 200),
            step("GET", "//example.invalid/", 200),
            step("TRACE", "/a", 200),
            {"request": {"method": "GET", "path": "/a", "headers": {"Host": "x"}}, "expect": {"status": 200}},
            {"request": {"method": "GET", "path": "/a"}, "expect": {}},
            {**good, "shell": "rm"},
            step("GET", "/a", 200, checks=[{"path": "x"}]),
            step("GET", "/a", 200, checks=[{"path": "x", "regex": "."}]),
            {**good, "setup": True},  # bước cuối không được là setup
        ]
        for bad in bad_steps:
            with self.subTest(bad=bad):
                with self.assertRaises(runner.ScenarioError):
                    runner.validate({"scenarios": [scenario(bad)]})
        with self.assertRaises(runner.ScenarioError):
            runner.validate({"scenarios": [scenario(good)] * (runner.MAX_SCENARIOS + 1)})

    def test_baseline_file_is_valid(self):
        scenarios = runner.load_file(os.path.join(os.path.dirname(HERE), "baseline.json"))
        self.assertGreaterEqual(len(scenarios), 5)

    def test_only_localhost(self):
        for url in ("http://example.com", "https://127.0.0.1", "http://127.0.0.1.evil.com", "http://10.0.0.1:80"):
            with self.subTest(url=url), self.assertRaises(runner.ScenarioError):
                runner.check_base_url(url)

    def test_empty_run_never_passes(self):
        self.assertFalse(runner.run([], self.base, {})["passed"])

    def test_main_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {n: os.path.join(tmp, n) for n in ("base.json", "ai.json", "out.json")}
            with open(paths["base.json"], "w", encoding="utf-8") as f:
                json.dump({"scenarios": [scenario(step("GET", "/health", 200), name="b")]}, f)
            open(paths["ai.json"], "w").close()  # rỗng = chỉ chạy kịch bản cơ bản
            with contextlib.redirect_stdout(io.StringIO()):
                code = runner.main(["--baseline", paths["base.json"], "--scenarios", paths["ai.json"],
                                    "--base-url", self.base, "--out", paths["out.json"], "--wait", "1"])
            with open(paths["out.json"], encoding="utf-8") as f:
                report = json.load(f)
        self.assertEqual((code, report["passed"], report["scenarios"][0]["source"]), (0, True, "baseline"))


if __name__ == "__main__":
    unittest.main()
