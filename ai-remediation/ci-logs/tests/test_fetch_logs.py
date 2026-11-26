"""Test fetch_logs với GitHub GIẢ chạy trên 127.0.0.1 (không gọi mạng ngoài)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fetch_logs  # noqa: E402

LOG_TEXT = "2026-11-26T07:00:00.0000000Z not ok 1 - lỗi thử\n2026-11-26T07:00:01.0000000Z dòng cuối\n"


class FakeGitHub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.server
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        s.requests.append((url.path, query, self.headers.get("Authorization")))
        if url.path.startswith("/blob/"):
            return self.send(200, b"\xef\xbb\xbf" + LOG_TEXT.encode())
        if s.status != 200:
            return self.send(s.status)
        prefix = "/repos/me/shop/actions"
        if url.path == f"{prefix}/runs":
            return self.page(query, "workflow_runs", s.runs)
        if url.path.startswith(f"{prefix}/runs/"):
            return self.page(query, "jobs", s.jobs.get(int(url.path.split("/")[-2]), []))
        if url.path.endswith("/logs"):
            job_id = url.path.split("/")[-2]
            if int(job_id) in s.broken:
                return self.send(410)
            # giống GitHub: chuyển hướng sang địa chỉ chứa file log
            return self.send(302, headers={"Location": f"http://127.0.0.1:{s.server_port}/blob/{job_id}"})
        return self.send(404)

    def page(self, query, key, items):
        size, page = int(query["per_page"][0]), int(query["page"][0])
        self.send(200, json.dumps({key: items[(page - 1) * size:page * size]}).encode())


def run(run_id, conclusion="failure"):
    return {"id": run_id, "name": "ci", "conclusion": conclusion, "event": "push", "head_branch": "main",
            "head_sha": "abc", "head_repository": {"full_name": "me/shop"}}


def job(job_id, conclusion="failure"):
    return {"id": job_id, "name": f"job {job_id}", "conclusion": conclusion, "html_url": "http://x",
            "steps": [{"name": "npm ci", "conclusion": "success"}, {"name": "npm test", "conclusion": conclusion}]}


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
        self.server.requests, self.server.status, self.server.broken = [], 200, set()
        self.server.runs = [run(1), run(2, "success")]
        self.server.jobs = {1: [job(11), job(12, "success")], 2: [job(21, "success")]}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"GITHUB_REPO": "me/shop", "GITHUB_TOKEN": "token-bi-mat", "CI_LOG_DIR": self.tmp.name,
                    "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_port}"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fetch_logs.main(self.env)
        return code, out.getvalue()

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def paths_called(self):
        return [r[0] for r in self.server.requests]

    def test_saves_only_failed_jobs_with_meta(self):
        code, _ = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(self.path("1"))), ["11.json", "11.log"])
        with open(self.path("1", "11.log"), encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), LOG_TEXT)  # giữ nguyên nội dung, đã bỏ BOM
        with open(self.path("1", "11.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["failed_steps"], ["npm test"])
        self.assertEqual((meta["branch"], meta["head_repo"], meta["job_id"]), ("main", "me/shop", 11))
        self.assertNotIn("/repos/me/shop/actions/runs/2/jobs", self.paths_called())

    def test_token_not_sent_to_redirect_target(self):
        self.main()
        auth = {path: a for path, _, a in self.server.requests}
        self.assertEqual(auth["/repos/me/shop/actions/jobs/11/logs"], "Bearer token-bi-mat")
        self.assertIsNone(auth["/blob/11"])

    def test_second_run_does_not_download_again(self):
        self.main()
        self.server.requests.clear()
        code, out = self.main()
        self.assertEqual(code, 0)
        self.assertIn("0 log mới", out)
        self.assertNotIn("/blob/11", self.paths_called())

    def test_conclusions_setting(self):
        self.env["CI_LOG_CONCLUSIONS"] = "failure, success"
        self.main()
        self.assertTrue(os.path.exists(self.path("1", "12.log")))
        self.assertTrue(os.path.exists(self.path("2", "21.log")))

    def test_one_broken_log_does_not_stop_others(self):
        self.server.jobs[1] = [job(11), job(13)]
        self.server.broken = {11}
        code, out = self.main()
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.path("1", "11.log")))
        self.assertTrue(os.path.exists(self.path("1", "13.log")))
        self.assertNotIn("token-bi-mat", out)

    def test_reads_all_pages(self):
        self.server.runs = [run(i, "success") for i in range(100, 200)] + [run(1)]
        self.main()
        self.assertTrue(os.path.exists(self.path("1", "11.log")))

    def test_filters_by_date(self):
        cfg = fetch_logs.load_config({**self.env, "CI_LOG_DAYS": "2"})
        with contextlib.redirect_stdout(io.StringIO()):
            fetch_logs.fetch(cfg, now=3 * 86400)
        self.assertEqual(self.server.requests[0][1]["created"], [">=1970-01-02"])

    def test_auth_error(self):
        self.server.status = 401
        code, out = self.main()
        self.assertEqual(code, 2)
        self.assertIn("401", out)
        self.assertNotIn("token-bi-mat", out)

    def test_bad_config(self):
        for change in ({"GITHUB_REPO": "../x"}, {"GITHUB_REPO": "me/.."}, {"GITHUB_REPO": ""}, {"GITHUB_TOKEN": ""},
                       {"CI_LOG_DAYS": "x"}, {"CI_LOG_DAYS": "0"}, {"CI_LOG_CONCLUSIONS": " , "}):
            with self.subTest(change=change):
                with self.assertRaises(fetch_logs.ConfigError):
                    fetch_logs.load_config({**self.env, **change})


if __name__ == "__main__":
    unittest.main()
