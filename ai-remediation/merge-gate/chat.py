"""Lệnh gõ trên Telegram và phiên chat với AI.

Lệnh chỉ xem (/status, /saved, /help) chạy ngay, không gọi AI. /test và /merge (kể cả do AI đề xuất) chỉ tạo
thẻ có nút bấm: không có đường nào từ chữ gõ (hay chữ AI viết) tới việc chạy thật mà không qua nút.
"""
import json

import ai
import views

SESSION_IDLE = 900
HISTORY_TURNS = 6
RUNNING = ("new", "ask", "plan", "dispatch", "testing", "review", "report", "pr_wait")

HELP = """<b>merge-gate</b>
Mở PR vào main là bot tự xin phép kiểm. Push thôi thì bot không làm gì.
/status — các nhánh đang xử lý (không gọi AI)
/test &lt;nhánh&gt; [yêu cầu thêm] — xin phép kiểm một nhánh
/merge &lt;nhánh&gt; — xin gộp vào main (chưa đạt thì đề nghị kiểm trước)
/saved — kịch bản đã lưu
/ai — mở phiên chat với AI (mỗi tin gọi AI, tính phí) · /end — đóng phiên
Mọi việc chạy thật đều cần bấm nút."""


class ChatMixin:
    """Dùng chung self.cfg, self.state, self.tg, self.model, self.clock... của Gate."""

    def handle_message(self, msg):
        chat_id = (msg.get("chat") or {}).get("id")
        user = (msg.get("from") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not text or not self.is_authorized(chat_id, user):
            return
        if not text.startswith("/"):
            return self.chat_turn(text)
        parts = text.split(maxsplit=2)
        command = parts[0].split("@")[0].lower()  # trong nhóm: /status@ten_bot
        arg = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if command in ("/help", "/start"):
            self.tg.send(HELP)
        elif command == "/status":
            self.cmd_status()
        elif command == "/saved":
            self.cmd_saved()
        elif command == "/ai":
            self.open_session()
        elif command == "/end":
            self.end_session("Đã đóng phiên chat.")
        elif command in ("/test", "/merge"):
            if not arg:
                self.tg.send(f"Thiếu tên nhánh, ví dụ: {command} feature/abc")
            elif command == "/test":
                self.cmd_test(arg, rest)
            else:
                self.cmd_merge(arg)
        else:
            self.tg.send("Không có lệnh này. /help")
        self.save()

    # ------------------------------------------------ lệnh
    def status_text(self):
        now = self.clock()
        lines = [f"📋 <b>merge-gate</b>: {self.cfg['mode']}, AI {self.cfg['ai']}"]
        for name, rec in sorted(self.state["branches"].items()):
            if rec["stage"] == "baseline":
                continue
            outcome = (rec.get("decision") or {}).get("outcome")
            label = f" · {views.HEADLINES[outcome][1]}" if outcome else ""
            number = f" PR #{rec['pr']}" if rec.get("pr") else ""
            lines.append(f"• <b>{views.esc(name)}</b>{number} <code>{rec['sha'][:7]}</code>: "
                         f"{views.STAGES.get(rec['stage'], rec['stage'])}{label}")
        if len(lines) == 1:
            lines.append("Chưa có yêu cầu merge nào.")
        used = len([t for t in self.state["reviews"] if now - t < 86400])
        chats = len([t for t in self.state["chat_calls"] if now - t < 86400])
        lines.append(f"AI trong 24 giờ: {used}/{self.cfg['max_reviews']} lần kiểm, {chats}/{self.cfg['max_chats']} tin chat. "
                     f"Đang chờ bạn bấm: {len(self.state['pending'])}")
        return views.fit(lines)

    def cmd_status(self):
        self.tg.send(self.status_text())

    def cmd_saved(self):
        saved = self.state["saved"]
        lines = [f"💾 <b>{len(saved)} kịch bản đã lưu</b>"]
        lines += [f"• {views.esc(s['scenario']['name'][:90])} <i>({views.esc(', '.join(s['features'][:3]))})</i>" for s in saved[-25:]]
        self.tg.send(views.fit(lines))

    def find_branch(self, name):
        rec = self.state["branches"].get(name)
        if not rec:
            self.tg.send(f"Không có nhánh <b>{views.esc(name)}</b> (hoặc nhánh nằm trong danh sách bỏ qua). /status")
        return rec

    def cmd_test(self, name, notes=""):
        rec = self.find_branch(name)
        if not rec:
            return
        if rec["stage"] in RUNNING:
            return self.tg.send(f"<b>{views.esc(name)}</b> đang xử lý: {views.STAGES[rec['stage']]}.")
        self.drop_pending(name)
        self.state["branches"][name] = {"sha": rec["sha"], "stage": "new", "seen_at": self.clock() - self.cfg["quiet"],
                                        "notes": notes[:600], "requested": True,
                                        **{k: rec[k] for k in ("pr", "pr_url") if k in rec}}
        self.advance(name)
        current = self.state["branches"][name]["stage"]
        if current == "new":
            self.tg.send(f"<b>{views.esc(name)}</b>: đang chờ CI, xong sẽ gửi thẻ xin phép.")
        elif current == "done" and not self.state["branches"][name].get("decision"):
            self.tg.send(f"<b>{views.esc(name)}</b> không có thay đổi nào so với {views.esc(self.cfg['base'])}.")

    def cmd_merge(self, name):
        rec = self.find_branch(name)
        if not rec:
            return
        if rec["stage"] in RUNNING:
            return self.tg.send(f"<b>{views.esc(name)}</b> đang xử lý: {views.STAGES[rec['stage']]}.")
        outcome = (rec.get("decision") or {}).get("outcome")
        if outcome in ("pass", "manual", "suspect"):  # đã có kết quả cho đúng commit này -> gửi lại thẻ duyệt
            self.drop_pending(name)
            rec["stage"] = "report"
            return self.advance(name)
        if rec["stage"] == "awaiting_test":
            return self.tg.send(f"<b>{views.esc(name)}</b> chưa được kiểm: hãy bấm nút ở thẻ xin phép kiểm.")
        self.tg.send(f"<b>{views.esc(name)}</b> chưa có kết quả đạt cho commit mới nhất, cần kiểm trước.")
        self.cmd_test(name)

    # ------------------------------------------------ phiên chat
    def open_session(self):
        if self.cfg["ai"] == "off":
            return self.tg.send("AI đang tắt (GATE_AI=off). Dùng được các lệnh: /help")
        self.state["session"] = {"last": self.clock(), "history": []}
        self.tg.send(f"💬 Đã mở phiên chat. Mỗi tin gọi AI (tính phí, tối đa {self.cfg['max_chats']} tin/24 giờ). "
                     "AI chỉ đề xuất, chạy gì cũng cần bạn bấm nút. /end để đóng.\n\n" + self.status_text())

    def end_session(self, message):
        self.state["session"] = None
        self.tg.send(message)

    def chat_context(self, text, history):
        branches = {n: r for n, r in self.state["branches"].items() if r["stage"] != "baseline"}
        summary = [{"nhanh": n, "commit": r["sha"][:7], "buoc": views.STAGES.get(r["stage"], r["stage"]),
                    "ket_qua": (r.get("decision") or {}).get("outcome"), "tieu_de": r.get("title", "")}
                   for n, r in branches.items()]
        data = {"nhanh_dang_xu_ly": summary, "moi_nhanh_trong_repo": sorted(self.state["branches"])[:60]}
        # nhánh đang bàn: nhắc tên trong tin này, rồi tin trước; không thì nhánh mới nhất
        mentioned = [n for t in [text] + [h["u"] + " " + h["a"] for h in reversed(history)]
                     for n in self.state["branches"] if n in t]
        focus = mentioned[0] if mentioned else max(branches, key=lambda n: branches[n]["seen_at"], default=None)
        if focus:
            rec = self.state["branches"][focus]
            detail = {"nhanh": focus, "pham_vi": rec.get("scope"), "file_doi": rec.get("changed"),
                      "ai_hieu": (rec.get("plan") or {}).get("understanding"), "quyet_dinh": rec.get("decision"),
                      "ket_qua_khong_dat": [r for r in rec.get("results") or [] if r["status"] != "passed"][:6],
                      "so_kich_ban_dat": sum(r["status"] == "passed" for r in rec.get("results") or []),
                      "danh_gia": rec.get("review")}
            try:
                detail["diff"] = self.diff_of(rec["sha"])[:8000]
            except (OSError, ValueError, KeyError):
                detail["diff"] = "(không đọc được)"
            data["nhanh_dang_ban"] = detail
        return json.dumps(data, ensure_ascii=False)

    def chat_turn(self, text):
        session = self.state.get("session")
        if not session:
            return self.tg.send("Gõ /ai để mở phiên chat, hoặc /help để xem lệnh.")
        now = self.clock()
        if now - session["last"] > SESSION_IDLE:
            return self.end_session("Phiên chat đã đóng vì im lặng quá 15 phút. Gõ /ai để mở lại.")
        if self.cfg["ai"] == "off":
            return self.end_session("AI đang tắt, đã đóng phiên.")
        self.state["chat_calls"] = [t for t in self.state["chat_calls"] if now - t < 86400]
        if len(self.state["chat_calls"]) >= self.cfg["max_chats"]:
            return self.tg.send(f"Đã hết {self.cfg['max_chats']} tin chat trong 24 giờ. Các lệnh /status, /test vẫn dùng được.")
        context = self.chat_context(text, session["history"])
        self.state["chat_calls"].append(now)  # tính lượt trước khi gọi
        session["last"] = now
        self.save()
        try:
            answer = ai.chat(self.model, session["history"], context, text)
        except ai.AIError as e:
            return self.tg.send(f"⚠️ AI không trả lời được: {views.esc(str(e)[:300])}")
        session["history"] = (session["history"] + [{"u": text[:400], "a": answer["reply"][:600]}])[-HISTORY_TURNS:]
        self.tg.send("🤖 " + views.esc(answer["reply"]))
        action = answer["action"]
        if action:
            if action["type"] == "test":
                self.cmd_test(action["branch"], action["notes"])
            else:
                self.cmd_merge(action["branch"])
        self.save()
