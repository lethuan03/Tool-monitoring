"""Nội dung tin nhắn Telegram (HTML). Chỉ dựng chữ, không gọi mạng."""
import html

import ai

HEADLINES = {  # kết quả -> (biểu tượng, nhãn, commit status)
    "pass": ("✅", "ĐẠT", "success"),
    "manual": ("🟡", "ĐẠT nhưng bạn phải merge tay", "pending"),
    "suspect": ("🟡", "CHƯA ĐẠT, nghi do kịch bản test", "pending"),
    "unverified": ("🟡", "CHƯA KIỂM ĐỦ", "pending"),
    "fail": ("❌", "KHÔNG ĐẠT", "failure"),
    "blocked": ("⛔", "BỊ CHẶN", "failure"),
    "error": ("⚠️", "KHÔNG KIỂM ĐƯỢC", "failure"),
}
MARKS = {"passed": "✅", "failed": "❌", "error": "⚠️"}
SOURCES = {"baseline": "cơ bản", "saved": "đã lưu", "ai": "mới"}
STAGES = {"new": "chờ CI", "ask": "chuẩn bị", "awaiting_test": "chờ bạn cho phép test", "plan": "AI soạn kịch bản",
          "dispatch": "gửi chạy thử", "testing": "đang chạy thử", "review": "AI đánh giá", "report": "đang báo kết quả",
          "awaiting": "chờ bạn duyệt", "pr_wait": "chờ CI của PR", "done": "xong",
          "baseline": "chưa có yêu cầu merge"}


def esc(s):
    return html.escape(str(s or ""), quote=False)


def fit(lines, tail=""):
    """Ghép các dòng, cắt theo DÒNG để không vỡ thẻ HTML (giới hạn tin Telegram 4096)."""
    body = ""
    for line in lines:
        if len(body) + len(line) + len(tail) > 3900:
            body += "\n…"
            break
        body += ("\n" if body else "") + line
    return body + ("\n" + tail if tail else "")


def head(name, rec):
    return f"Nhánh <b>{esc(name)}</b> @ <code>{rec['sha'][:7]}</code>"


def pr_lines(rec):
    if not rec.get("pr"):
        return []
    return [f'📥 Yêu cầu merge: <a href="{html.escape(rec.get("pr_url", ""))}">PR #{rec["pr"]}</a>']


def scope_lines(scope):
    if not scope:
        return []
    lines = []
    if scope["endpoints"]:
        more = f" (+{len(scope['endpoints']) - 8})" if len(scope["endpoints"]) > 8 else ""
        lines.append("API bị ảnh hưởng: " + esc(", ".join(scope["endpoints"][:8])) + more)
    if scope["features"]:
        lines.append("Chức năng: " + esc(", ".join(scope["features"])))
    if scope["wide"]:
        lines.append("Không khoanh vùng được, AI sẽ đọc toàn bộ backend: " + esc("; ".join(scope["why"][:3])))
    else:
        lines.append(f"AI sẽ đọc {len(scope['files'])} file: " + esc(", ".join(p.split('/src/')[-1] for p in scope["files"][:8])))
    return lines


def test_card(name, rec, ai_on, saved_count, used, limit):
    lines = ["🔎 <b>Xin phép kiểm code</b>", head(name, rec), *pr_lines(rec), esc(rec.get("title", "")), ""]
    lines.append(f"File đổi ({len(rec.get('changed', []))}): " + esc(", ".join(rec.get("changed", [])[:8])))
    lines += scope_lines(rec.get("scope"))
    lines += [f"⚠️ {esc(w[:200])}" for w in rec.get("warnings", [])[:4]]
    if rec.get("manual"):
        lines += ["Kể cả đạt, bạn phải merge tay vì:"] + [f"• {esc(x[:220])}" for x in rec["manual"][:4]]
    if rec.get("notes"):
        lines.append("Yêu cầu thêm của bạn: " + esc(rec["notes"][:300]))
    lines += ["", "<b>Nếu cho phép:</b>"]
    if ai_on:
        lines.append(f"• Gọi AI tối đa 3 lượt (tính phí). Đã dùng {used}/{limit} lần kiểm trong 24 giờ.")
    else:
        lines.append("• AI đang tắt: chỉ chạy kịch bản có sẵn, không tốn phí AI.")
    lines.append(f"• Chạy thử trên GitHub: kịch bản cơ bản, {saved_count} kịch bản đã lưu liên quan" + (" và kịch bản mới." if ai_on else "."))
    lines.append("Merge sẽ xin phép riêng.")
    return fit(lines)


def progress(name, rec, current, ai_on):
    """current: plan | test | review | done."""
    order = ["plan", "test", "review"]
    labels = {"plan": "AI soạn kịch bản" if ai_on else "Chọn kịch bản có sẵn",
              "test": "Chạy thử trên GitHub" + (" (lượt 2: kịch bản đã soạn lại)" if rec.get("round") == 2 else ""),
              "review": "AI đánh giá" if ai_on else "Tổng hợp"}
    if current == "plan":
        labels["test"] = "Chạy thử trên GitHub"
    if current in order:
        position = order.index(current)
        marks = ["✅" if i < position else "⏳" if i == position else "⬜" for i in range(len(order))]
    else:  # xong (có thể dừng giữa chừng): chỉ đánh dấu bước thật sự đã làm
        has_results = rec.get("results") is not None
        done = [rec.get("to_run") is not None, has_results, has_results and (rec.get("review") is not None or not ai_on)]
        marks = ["✅" if ok else "✖️" for ok in done]
    lines = [f"🔄 {head(name, rec)}", "✅ CI"]
    for i, step in enumerate(order):
        extra = ""
        if step == "plan" and marks[i] == "✅" and rec.get("to_run") is not None:
            extra = f": {len(rec.get('final') or rec['to_run'])} kịch bản"
        lines.append(f"{marks[i]} {labels[step]}{extra}")
    if current == "done":
        _, label, _ = HEADLINES[rec["decision"]["outcome"]]
        lines.append(f"Kết quả: <b>{label}</b>")
    return "\n".join(lines)


def result(cfg, name, rec, warning=""):
    d = rec["decision"]
    icon, label, _ = HEADLINES[d["outcome"]]
    lines = [f"{icon} <b>{label}</b>", head(name, rec), *pr_lines(rec)]
    if rec.get("title"):
        lines.append(esc(rec["title"]))
    plan = rec.get("plan") or {}
    if plan.get("understanding"):
        lines += ["", f"<b>AI hiểu chức năng:</b> {esc(plan['understanding'][:400])}"]
    results = rec.get("results") or []
    if results:
        passed = sum(r["status"] == "passed" for r in results)
        lines += ["", f"<b>Chạy thử: {passed}/{len(results)} đạt</b>"]
        ordered = sorted(results, key=lambda r: (r["status"] == "passed", r["source"] != "ai"))
        for r in ordered[:12]:
            redone = " (đã soạn lại)" if r["name"] in rec.get("repaired", []) else ""
            lines.append(f"{MARKS[r['status']]} [{SOURCES.get(r['source'], r['source'])}] {esc(r['name'][:80])}{redone}")
            if r["status"] != "passed":
                lines.append(f"    {esc(str(r.get('request', ''))[:80])}: {esc(ai.redact(r.get('detail', ''))[:160])}")
        if len(ordered) > 12:
            lines.append(f"… và {len(ordered) - 12} kịch bản khác")
    review = rec.get("review")
    if review:
        lines += ["", f"<b>Ý kiến AI</b> (có thể sai): {esc(review['summary'][:500])}"]
        for i in review["issues"][:5]:
            where = f"{i['file']}:{i['line']}" if i["line"] else i["file"]
            lines.append(f"• [{i['severity']}] {esc(where[:80])} {esc(i['detail'][:160])}")
    if d["reasons"]:
        lines += ["", "<b>Lý do:</b>"] + [f"• {esc(x[:220])}" for x in d["reasons"][:8]]
    scope = rec.get("scope")
    if scope and results:
        lines.append(f"AI đã xem: {'toàn bộ backend' if scope['wide'] else str(len(scope['files'])) + ' file liên quan'}")
    lines += [f"⚠️ {esc(x[:200])}" for x in (rec.get("warnings") or [])[:4]]
    if warning:
        lines.append(f"⚠️ {esc(warning)}")
    if rec.get("pr") and d["outcome"] in ("manual", "suspect"):
        lines.append("👉 Bot không merge trường hợp này: xem lại rồi merge tay trên GitHub.")
    footer = [""]
    for text, url in (("CI", rec.get("ci_url")), ("Chạy thử", rec.get("run_url")),
                      ("Thay đổi", f"https://github.com/{cfg['repo']}/compare/{cfg['base']}...{rec['sha']}")):
        if url:
            footer.append(f'<a href="{html.escape(url)}">{text}</a>')
    if cfg["mode"] != "execute":
        footer.append("<i>dry-run: không tạo PR, không merge</i>")
    return fit(lines, "\n".join(footer))


def details(name, rec):
    """Bằng chứng của các kịch bản không đạt, tách khỏi ý kiến AI."""
    lines = [f"📄 <b>Chi tiết lỗi</b> — {head(name, rec)}"]
    suspects = {s["scenario"]: s for s in (rec.get("review") or {}).get("suspects", [])}
    bad = [r for r in rec.get("results") or [] if r["status"] != "passed"]
    for r in bad[:6]:
        def show(key, limit):
            return esc(ai.redact(str(r.get(key) or ""))[:limit])

        lines += ["", f"{MARKS[r['status']]} <b>{esc(r['name'][:120])}</b>",
                  f"Bước {r.get('step')}: {show('step_name', 100)}",
                  f"Gọi: <code>{show('request', 150)}</code>"]
        if r.get("body") and r["body"] != "null":
            lines.append(f"Gửi: <code>{show('body', 200)}</code>")
        lines += [f"Mong đợi: <code>{show('expected', 200)}</code>",
                  f"Nhận: status {r.get('actual_status')} <code>{show('response', 250)}</code>",
                  f"Sai ở: {show('detail', 250)}"]
        suspect = suspects.get(r["name"])
        if suspect:
            where = "code" if suspect["cause"] == "code" else "kịch bản test"
            lines.append(f"AI nghĩ lỗi ở <b>{where}</b>: {esc(suspect['why'][:220])}")
    if not bad:
        lines.append("Không có kịch bản nào lỗi.")
    return fit(lines)
