"""Test luật chặn của docker-proxy (allowlist). Không cần Docker.
Chạy:  python -m unittest discover -s ai-remediation/docker-proxy/tests -v
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("proxy", os.path.join(ROOT, "proxy.py"))
P = importlib.util.module_from_spec(spec)
sys.modules["proxy"] = P
spec.loader.exec_module(P)


def allow(method, path, length=0):
    return P.decide(method, path, length)


class AllowedCallsTests(unittest.TestCase):
    """Đúng những lời gọi remediator cần (lấy từ policy.json) phải đi qua."""

    def test_policy_calls_pass(self):
        policy = json.load(open(os.path.join(os.path.dirname(ROOT), "remediator", "policy.json"), encoding="utf-8"))
        for aid, spec_ in policy["actions"].items():
            d = spec_["docker"]
            path = d["path"].replace("{container}", "ecommerce-backend")
            q = "&".join(f"{k}={v}" for k, v in d.get("query", {}).items())
            ok, res = allow(d["method"], path + (f"?{q}" if q else ""))
            self.assertTrue(ok, f"{aid}: {res}")

    def test_ping_and_version_and_api_version_prefix(self):
        for m, path in (("GET", "/_ping"), ("HEAD", "/_ping"), ("GET", "/version"), ("GET", "/v1.45/containers/ecommerce-nginx/json")):
            self.assertTrue(allow(m, path)[0], path)

    def test_stats_is_forced_to_one_shot(self):
        ok, res = allow("GET", "/containers/ecommerce-backend/stats")
        self.assertTrue(ok)
        self.assertIn("stream=false", res)

    def test_db_can_be_started_and_inspected_but_not_restarted(self):
        self.assertTrue(allow("POST", "/containers/ecommerce-db/start")[0])
        self.assertTrue(allow("GET", "/containers/ecommerce-db/json")[0])
        self.assertFalse(allow("POST", "/containers/ecommerce-db/restart?t=10")[0])


class BlockedCallsTests(unittest.TestCase):
    """Mọi thứ còn lại bị chặn, kể cả khi remediator bị chiếm (các lỗ của tecnativa với POST=1)."""

    def test_dangerous_container_apis_blocked(self):
        for m, path in (("POST", "/containers/create"), ("POST", "/containers/create?name=x"),
                        ("DELETE", "/containers/ecommerce-backend"), ("DELETE", "/containers/ecommerce-db?force=1&v=1"),
                        ("PUT", "/containers/ecommerce-backend/archive?path=/app"),
                        ("POST", "/containers/ecommerce-backend/stop"), ("POST", "/containers/ecommerce-backend/kill"),
                        ("POST", "/containers/ecommerce-backend/exec"), ("POST", "/exec/abc/start"),
                        ("POST", "/containers/ecommerce-backend/update"), ("POST", "/containers/ecommerce-backend/rename?name=x"),
                        ("POST", "/containers/ecommerce-backend/pause"), ("POST", "/containers/ecommerce-backend/attach"),
                        ("GET", "/containers/ecommerce-backend/archive?path=/etc/passwd"),
                        ("GET", "/containers/ecommerce-backend/export"), ("POST", "/containers/prune")):
            self.assertFalse(allow(m, path)[0], f"{m} {path}")

    def test_other_apis_blocked(self):
        for m, path in (("GET", "/images/json"), ("POST", "/images/create?fromImage=alpine"), ("GET", "/volumes"),
                        ("GET", "/info"), ("GET", "/networks"), ("POST", "/build"), ("GET", "/secrets"), ("GET", "/events"),
                        ("GET", "/containers/json"), ("POST", "/swarm/init"), ("GET", "/system/df")):
            self.assertFalse(allow(m, path)[0], f"{m} {path}")

    def test_system_and_foreign_containers_blocked(self):
        for name in ("ecommerce-remediator", "ecommerce-holmes-bridge", "ecommerce-docker-proxy", "freshmart-app-1", "postgres"):
            self.assertFalse(allow("POST", f"/containers/{name}/restart")[0], name)
            self.assertFalse(allow("GET", f"/containers/{name}/json")[0], name)

    def test_query_abuse_blocked(self):
        for path in ("/containers/ecommerce-backend/logs?follow=1", "/containers/ecommerce-backend/logs?tail=all",
                     "/containers/ecommerce-backend/logs?tail=999999", "/containers/ecommerce-backend/logs?stdout=1&stdout=1",
                     "/containers/ecommerce-backend/stats?stream=true", "/containers/ecommerce-backend/json?size=1"):
            self.assertFalse(allow("GET", path)[0], path)
        self.assertFalse(allow("POST", "/containers/ecommerce-backend/restart?t=99999")[0])
        self.assertFalse(allow("POST", "/containers/ecommerce-backend/restart?signal=KILL")[0])

    def test_path_tricks_blocked(self):
        for path in ("/containers/ecommerce-backend/../create", "/containers/%2e%2e/create", "//containers/ecommerce-backend/json",
                     "/containers/ecommerce-backend/json/", "/containers/ecommerce-a/b/json", "/v1.45/../containers/create"):
            self.assertFalse(allow("GET", path)[0], path)
            self.assertFalse(allow("POST", path.replace("/json", "/restart"))[0], path)

    def test_request_body_rejected(self):
        self.assertFalse(allow("POST", "/containers/ecommerce-backend/start", 10)[0])

    def test_wrong_method_blocked(self):
        self.assertFalse(allow("POST", "/containers/ecommerce-backend/json")[0])
        self.assertFalse(allow("GET", "/containers/ecommerce-backend/restart")[0])
        self.assertFalse(allow("DELETE", "/_ping")[0])


class ConfigTests(unittest.TestCase):
    def test_prefix_and_lists_from_env(self):
        old = (P.ALLOW_PREFIX, P.DENY, P.RESTART_DENY)
        try:
            P.ALLOW_PREFIX, P.DENY, P.RESTART_DENY = "ecommerce-sbx-", {"sbx-remediator"}, {"ecommerce-sbx-db"}
            self.assertTrue(allow("POST", "/containers/ecommerce-sbx-nginx/restart")[0])
            self.assertFalse(allow("POST", "/containers/ecommerce-backend/restart")[0])     # sandbox không bao giờ chạm stack thật
            self.assertFalse(allow("POST", "/containers/ecommerce-sbx-db/restart")[0])
        finally:
            P.ALLOW_PREFIX, P.DENY, P.RESTART_DENY = old


if __name__ == "__main__":
    unittest.main()
