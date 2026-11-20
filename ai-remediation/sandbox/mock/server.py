#!/usr/bin/env python3
"""
Mock Telegram Bot API + Prometheus cho sandbox (README2.md, mục Sandbox).
KHÔNG gọi ra internet. Giả lập vừa đủ để bridge + remediator chạy thật:

  Telegram: sendMessage, getUpdates (long-poll), answerCallbackQuery, editMessageReplyMarkup
  Prometheus: /api/v1/query luôn trả vector rỗng (= không có target nào down / alert nào firing)
  Điều khiển (cho script test):
    GET  /_state   -> {"messages": [...], "answers": [...], "edits": [...]}
    POST /_click   -> {"message_id": n, "button": 0|1, "chat_id": "111", "user_id": "999", "data": "<tuỳ chọn ghi đè>"}
                      giả lập người bấm nút inline (0 = Duyệt, 1 = Từ chối)
    POST /_say     -> {"text": "/stop", "chat_id": "111", "user_id": "999"} giả lập người gõ chữ trong chat
    POST /_down    -> {"jobs": ["backend"]} làm Prometheus giả báo các job này `up == 0` ([] = mọi thứ ổn)
    POST /_reset   -> xoá sạch trạng thái
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

cond = threading.Condition()
S = {"messages": [], "answers": [], "edits": [], "updates": [], "next_msg": 1, "next_update": 1, "down": []}


def reset():
    # update_id PHẢI luôn tăng (như Telegram thật): client nhớ offset, đặt lại về 1 sẽ làm nó bỏ qua nút bấm mới
    with cond:
        S.update(messages=[], answers=[], edits=[], updates=[], next_msg=1, down=[])


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path.startswith("/api/v1/query"):
            with cond:
                jobs = list(S["down"]) if "up+%3D%3D+0" in self.path or "up%20%3D%3D%200" in self.path else []
            res = [{"metric": {"job": j}, "value": [0, "0"]} for j in jobs]
            return self._json({"status": "success", "data": {"resultType": "vector", "result": res}})
        if self.path == "/_state":
            with cond:
                return self._json({k: S[k] for k in ("messages", "answers", "edits")})
        self._json({"ok": False}, 404)

    def do_POST(self):
        body = self._body()
        m = re.fullmatch(r"/bot[^/]+/(\w+)", self.path)
        if m:
            return self._telegram(m.group(1), body)
        if self.path == "/_reset":
            reset()
            return self._json({"ok": True})
        if self.path == "/_click":
            return self._click(body)
        if self.path == "/_say":
            with cond:
                uid = S["next_update"]; S["next_update"] += 1
                S["updates"].append({"update_id": uid, "message": {
                    "message_id": 9000 + uid, "text": body.get("text", ""), "from": {"id": int(body.get("user_id", 999))},
                    "chat": {"id": int(body.get("chat_id", 111))}}})
                cond.notify_all()
            return self._json({"ok": True})
        if self.path == "/_down":
            with cond:
                S["down"] = list(body.get("jobs", []))
            return self._json({"ok": True})
        self._json({"ok": False}, 404)

    # ------------------------------------------------------------ Telegram
    def _telegram(self, method, body):
        if method == "sendMessage":
            with cond:
                mid = S["next_msg"]; S["next_msg"] += 1
                S["messages"].append({"message_id": mid, "chat_id": str(body.get("chat_id")), "text": body.get("text", ""),
                                      "reply_to": body.get("reply_to_message_id"),
                                      "buttons": [b for row in (body.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]})
            return self._json({"ok": True, "result": {"message_id": mid}})
        if method == "getUpdates":
            offset, timeout = int(body.get("offset") or 0), min(int(body.get("timeout") or 0), 5)
            with cond:
                cond.wait_for(lambda: any(u["update_id"] >= offset for u in S["updates"]), timeout=timeout)
                res = [u for u in S["updates"] if u["update_id"] >= offset]
            return self._json({"ok": True, "result": res})
        if method == "answerCallbackQuery":
            with cond:
                S["answers"].append(body.get("text", ""))
            return self._json({"ok": True, "result": True})
        if method == "editMessageReplyMarkup":
            with cond:
                S["edits"].append(body.get("message_id"))
            return self._json({"ok": True, "result": True})
        self._json({"ok": False, "description": "method not mocked"}, 404)

    def _click(self, b):
        with cond:
            msg = next((x for x in S["messages"] if x["message_id"] == b.get("message_id")), None)
            if not msg and not b.get("data"):
                return self._json({"ok": False, "error": "không có tin nhắn đó"}, 404)
            data = b.get("data") or msg["buttons"][int(b.get("button", 0))]["callback_data"]
            uid = S["next_update"]; S["next_update"] += 1
            S["updates"].append({"update_id": uid, "callback_query": {
                "id": f"cb{uid}", "data": data, "from": {"id": int(b.get("user_id", 999))},
                "message": {"message_id": b.get("message_id"), "chat": {"id": int(b.get("chat_id", 111))}}}})
            cond.notify_all()
        self._json({"ok": True, "update_id": uid})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), H).serve_forever()
