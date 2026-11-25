#!/usr/bin/env python3
"""
docker-proxy (allowlist): cổng DUY NHẤT tới Docker API cho remediator.

Thay cho tecnativa/docker-socket-proxy (bật CONTAINERS=1 + POST=1 thì cho TẠO / XOÁ container, ghi file vào container...
= quyền root trên máy nếu remediator bị chiếm). Proxy này chỉ chuyển ĐÚNG các lời gọi remediator cần, mọi thứ khác trả 403:

  GET  /_ping, /version                                  (kiểm tra sống)
  GET  /containers/<tên>/json                            inspect (remediator lọc trường trước khi đưa ra ngoài)
  GET  /containers/<tên>/logs?stdout&stderr&tail<=1000   không cho follow (giữ kết nối mãi)
  GET  /containers/<tên>/stats?stream=false              luôn ép stream=false
  POST /containers/<tên>/restart?t<=60                   trừ container trong RESTART_DENY (vd ecommerce-db)
  POST /containers/<tên>/start

Tên container phải bắt đầu bằng ALLOW_PREFIX và không nằm trong DENY_CONTAINERS (chính bridge/remediator/proxy).
Không nhận thân yêu cầu. Luật chính (container nào, action nào) vẫn ở remediator/policy.json; đây là lớp chặn THỨ HAI
để dù remediator bị chiếm cũng không làm được gì ngoài các việc trên. Chỉ dùng thư viện chuẩn Python.
"""
import http.client
import json
import logging
import os
import re
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _list(name, default):
    return {x.strip() for x in os.environ.get(name, default).split(",") if x.strip()}


SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
PORT = int(os.environ.get("PROXY_PORT", "2375"))
ALLOW_PREFIX = os.environ.get("ALLOW_PREFIX", "ecommerce-")
DENY = _list("DENY_CONTAINERS", "ecommerce-holmes-bridge,ecommerce-remediator,ecommerce-docker-proxy,ecommerce-alloy-docker-proxy")
RESTART_DENY = _list("RESTART_DENY", "ecommerce-db")
MAX_RESPONSE = 2_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("docker-proxy")

VER = r"(?:/v\d{1,2}\.\d{1,3})?"
NAME = r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,100})"
BOOL = {"0", "1", "true", "false"}


def _int_in(v, lo, hi):
    return v.isdigit() and lo <= int(v) <= hi


# (method, regex đường dẫn, {tham số được phép: hàm kiểm tra}, tham số ép buộc, loại hành động)
RULES = [
    ("GET", re.compile(rf"^{VER}/_ping$"), {}, {}, None),
    ("HEAD", re.compile(rf"^{VER}/_ping$"), {}, {}, None),
    ("GET", re.compile(rf"^{VER}/version$"), {}, {}, None),
    ("GET", re.compile(rf"^{VER}/containers/{NAME}/json$"), {}, {}, "inspect"),
    ("GET", re.compile(rf"^{VER}/containers/{NAME}/logs$"),
     {"stdout": lambda v: v in BOOL, "stderr": lambda v: v in BOOL, "timestamps": lambda v: v in BOOL,
      "tail": lambda v: _int_in(v, 1, 1000)}, {}, "logs"),
    ("GET", re.compile(rf"^{VER}/containers/{NAME}/stats$"),
     {"stream": lambda v: v in ("0", "false"), "one-shot": lambda v: v in BOOL}, {"stream": "false"}, "stats"),
    ("POST", re.compile(rf"^{VER}/containers/{NAME}/restart$"), {"t": lambda v: _int_in(v, 0, 60)}, {}, "restart"),
    ("POST", re.compile(rf"^{VER}/containers/{NAME}/start$"), {}, {}, "start"),
]


def decide(method, raw_path, content_length=0):
    """(cho_phép, đường_dẫn_chuyển_tiếp | lý_do_chặn). Hàm thuần, dễ test."""
    if content_length:
        return False, "không nhận thân yêu cầu"
    parts = urllib.parse.urlsplit(raw_path)
    path = parts.path
    if "%" in path or "//" in path or "/." in path or "\\" in path:
        return False, "đường dẫn có ký tự lạ"
    try:
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        return False, "query sai dạng"
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        return False, "tham số bị lặp"
    for m, rx, allowed, forced, kind in RULES:
        if method != m:
            continue
        hit = rx.match(path)
        if not hit:
            continue
        name = hit.groupdict().get("name")
        if name is not None:
            if not name.startswith(ALLOW_PREFIX) or name in DENY:
                return False, f"container '{name}' không được phép"
            if kind == "restart" and name in RESTART_DENY:
                return False, f"không được restart '{name}'"
        q = {}
        for k, v in pairs:
            check = allowed.get(k)
            if check is None or not check(v):
                return False, f"tham số '{k}' không được phép"
            q[k] = v
        q.update(forced)
        return True, path + (("?" + urllib.parse.urlencode(q)) if q else "")
    return False, f"{method} {path} không nằm trong danh sách cho phép"


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout=60):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class Handler(BaseHTTPRequestHandler):
    timeout = 20
    protocol_version = "HTTP/1.0"

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length > 0:
            self.rfile.read(min(length, 100_000))     # đọc bỏ rồi từ chối
        ok, res = decide(self.command, self.path, length)
        if not ok:
            log.warning("CHẶN %s %s: %s", self.command, self.path[:200], res)
            return self._send(403, json.dumps({"message": f"blocked by allowlist proxy: {res}"}, ensure_ascii=False).encode())
        conn = UnixHTTPConnection(SOCK)
        try:
            conn.request(self.command, res, body=b"" if self.command == "POST" else None,
                         headers={"Host": "docker", "Content-Length": "0"} if self.command == "POST" else {"Host": "docker"})
            r = conn.getresponse()
            body = r.read(MAX_RESPONSE)
            log.info("CHO PHÉP %s %s -> %s", self.command, res[:200], r.status)
            return self._send(r.status, body, r.getheader("Content-Type") or "application/octet-stream")
        except OSError as e:
            log.error("Không gọi được Docker: %s", type(e).__name__)
            return self._send(502, b'{"message":"docker socket unavailable"}')
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("docker-proxy (allowlist) :%d · tiền tố %s · cấm %s · cấm restart %s", PORT, ALLOW_PREFIX,
             ",".join(sorted(DENY)), ",".join(sorted(RESTART_DENY)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
