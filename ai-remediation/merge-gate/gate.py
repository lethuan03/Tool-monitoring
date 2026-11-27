"""merge-gate: mọi code muốn vào main phải qua CI + AI chạy thử chức năng + người duyệt trên Telegram.

Chạy trên máy local (1 container, không mở cổng, 1 luồng). Mỗi vòng:
  nhánh có commit mới -> CI xanh -> kiểm tra cứng -> AI soạn kịch bản -> workflow ai-test chạy trên GitHub
  -> AI đánh giá -> Telegram [duyệt/từ chối] -> tạo PR -> CI của PR xanh -> merge đúng commit đã kiểm.

Mặc định an toàn: GATE_AI=off (không gọi Bedrock) và GATE_MODE=dry-run (không gắn status, không tạo PR, không merge;
vẫn kích hoạt workflow chạy thử). Biến môi trường: xem .env.mergegate.example và README2.md mục 11.
"""
import base64
import fnmatch
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.append(os.path.join(os.path.dirname(HERE), "ci-logs"))  # trong image, fetch_logs.py nằm cạnh file này
import ai  # noqa: E402
import fetch_logs  # noqa: E402

log = logging.getLogger("merge-gate")

CI_WORKFLOW = ".github/workflows/ci.yml"
TEST_WORKFLOW = "ai-test.yml"
ARTIFACT = "ai-test-result"
STATUS_CONTEXT = "ai-gate"
DEFAULT_REQUIRED_JOBS = "Backend (Node.js);Frontend (Node.js);AI remediation (Python);Build thử image"
CI_MISSING_AFTER = 900
TEST_TIMEOUT = 1800
PR_TIMEOUT = 1200
APPROVAL_TTL = 86400
MAX_ERRORS = 10
MAX_SCENARIO_INPUT = 60000  # giới hạn input của workflow_dispatch khoảng 65 535 ký tự

# Sửa các đường dẫn này = có thể đổi chính cách kiểm tra -> không bao giờ tự merge
PROTECTED = (".github/*", "ai-remediation/merge-gate/*", "ai-remediation/ci-logs/*",
             "ai-remediation/remediator/policy.json", "*docker-compose*.yml")
# Phần chạy thử được qua HTTP API
TESTABLE = ("webapp/backend/*", "webapp/mysql/*", "webapp/nginx/*")


class ConfigError(Exception):
    pass


def load_config(env):
    def clean(name, default=""):
        return re.sub(r"[\s\"']", "", env.get(name, "")) or default

    def number(name, default):
        try:
            return int(env.get(name) or default)
        except ValueError:
            raise ConfigError(f"{name} phải là số nguyên") from None

    try:
        github = fetch_logs.load_config({"GITHUB_REPO": env.get("GITHUB_REPO", ""),
                                         "GITHUB_TOKEN": clean("GATE_GITHUB_TOKEN"),
                                         "GITHUB_API_URL": env.get("GITHUB_API_URL", "")})
    except fetch_logs.ConfigError as e:
        raise ConfigError(str(e).replace("GITHUB_TOKEN", "GATE_GITHUB_TOKEN")) from None
    cfg = {
        "github": github,
        "repo": github["repo"],
        "base": env.get("GATE_BASE_BRANCH") or "main",
        "mode": "execute" if clean("GATE_MODE").lower() == "execute" else "dry-run",
        "ai": "bedrock" if clean("GATE_AI").lower() == "bedrock" else "off",
        "model_id": clean("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0").removeprefix("bedrock/"),
        "region": clean("BEDROCK_REGION"),
        "poll": max(15, number("GATE_POLL_SECONDS", 60)),
        "quiet": max(0, number("GATE_QUIET_SECONDS", 120)),
        "max_reviews": max(0, number("GATE_MAX_REVIEWS_PER_DAY", 10)),
        "ignore": [p.strip() for p in (env.get("GATE_IGNORE_BRANCHES") or "backup/*,test/*").split(",") if p.strip()],
        "diff_max": max(1000, number("GATE_DIFF_MAX_CHARS", 60000)),
        "context_max": max(0, number("GATE_CONTEXT_MAX_CHARS", 60000)),
        "required_jobs": [j.strip() for j in (env.get("GATE_REQUIRED_JOBS") or DEFAULT_REQUIRED_JOBS).split(";") if j.strip()],
        "tg_token": clean("GATE_TELEGRAM_BOT_TOKEN"),
        "tg_chat": clean("GATE_TELEGRAM_CHAT_ID"),
        "tg_api": (env.get("TELEGRAM_API_URL") or "https://api.telegram.org").rstrip("/"),
        "approvers": {s.strip() for s in env.get("GATE_APPROVER_IDS", "").split(",") if s.strip()},
        "secret": clean("GATE_SECRET"),
        "state_file": env.get("STATE_FILE") or os.path.join(HERE, "state.json"),
    }
    if not cfg["tg_token"] or not cfg["tg_chat"]:
        raise ConfigError("Thiếu GATE_TELEGRAM_BOT_TOKEN hoặc GATE_TELEGRAM_CHAT_ID")
    if cfg["ai"] == "bedrock" and not cfg["region"]:
        raise ConfigError("GATE_AI=bedrock cần BEDROCK_REGION")
    return cfg


# ---------------------------------------------------------------- GitHub
class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg

    def raw(self, path):
        return fetch_logs.github_get(self.cfg, path)

    def get(self, path, **params):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return json.loads(self.raw(path + query))

    def pages(self, path, key=None, **params):
        items = []
        for page in range(1, 11):
            data = self.get(path, per_page=100, page=page, **params)
            batch = data[key] if key else data
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def send(self, method, path, body):
        req = urllib.request.Request(f"{self.cfg['api']}/repos/{self.cfg['repo']}{path}",
                                     data=json.dumps(body).encode(), method=method)
        req.add_unredirected_header("Authorization", f"Bearer {self.cfg['token']}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "merge-gate")
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read()
        return json.loads(text) if text else None


# ---------------------------------------------------------------- Telegram
class Telegram:
    def __init__(self, token, chat, api):
        self.token, self.chat, self.api = token, chat, api

    def call(self, method, body, timeout=40):
        req = urllib.request.Request(f"{self.api}/bot{self.token}/{method}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("result")
        except urllib.error.HTTPError as e:
            log.error("Telegram %s từ chối (HTTP %s)", method, e.code)
        except Exception as e:  # không in URL: URL chứa token
            log.error("Telegram %s lỗi mạng: %s", method, type(e).__name__)
        return None

    def send(self, text, buttons=None):
        body = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [buttons]}
        return (self.call("sendMessage", body, timeout=20) or {}).get("message_id")

    def clear_buttons(self, message_id):
        self.call("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": message_id,
                                             "reply_markup": {"inline_keyboard": []}}, timeout=15)

    def answer(self, callback_id, text):
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]}, timeout=15)

    def updates(self, offset, timeout):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]},
                         timeout=timeout + 15)


def esc(s):
    return html.escape(str(s or ""), quote=False)


# ---------------------------------------------------------------- kiểm tra cứng (không dùng AI)
def _matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def _is_secret_file(path):
    name = path.rsplit("/", 1)[-1]
    if (name == ".env" or name.startswith(".env.")) and not name.endswith(".example"):
        return True
    return name.endswith((".pem", ".key")) or path.startswith("ec2/")


def inspect(files, diff_max):
    """Trả {"blocked": [...], "manual": [...], "warnings": [...], "testable": bool}."""
    blocked, protected, untested, unseen, warnings = [], [], [], [], []
    size, testable = 0, False
    for f in files:
        path, status, patch = f["filename"], f.get("status"), f.get("patch")
        old = f.get("previous_filename") or path
        if status != "removed":
            if _is_secret_file(path):
                blocked.append(f"file bí mật: {path}")
            added = "\n".join(line for line in (patch or "").splitlines() if line.startswith("+"))
            if ai.contains_secret(added):
                blocked.append(f"có khoá/token trong: {path}")
        if _matches(path, PROTECTED) or _matches(old, PROTECTED):
            protected.append(path)
        if _matches(path, TESTABLE) or _matches(old, TESTABLE):
            testable = True
        elif not path.endswith(".md"):
            untested.append(path)
        # GitHub không trả patch cho file quá lớn / nhị phân -> AI không xem được
        if patch is None and status != "removed" and not (status == "renamed" and not f.get("changes")):
            unseen.append(path)
        size += len(patch or "")
        if status == "removed" and re.search(r"(^|/)tests?/|\.test\.|(^|/)test_", path):
            warnings.append(f"xoá file test: {path}")

    def short(paths):
        return ", ".join(paths[:4]) + (f" và {len(paths) - 4} file khác" if len(paths) > 4 else "")

    manual = []
    if protected:
        manual.append(f"sửa phần được bảo vệ: {short(protected)}")
    if untested:
        manual.append(f"có phần chưa chạy thử được: {short(untested)}")
    if unseen:
        manual.append(f"AI không xem được nội dung: {short(unseen)}")
    if size > diff_max or len(files) >= 300:
        manual.append("diff quá dài, AI không xem hết")
    return {"blocked": blocked, "manual": manual, "warnings": warnings, "testable": testable}


def build_diff(files, limit):
    parts = []
    for f in files:
        parts.append(f"=== {f.get('status')} {f['filename']}\n{f.get('patch') or '(không có nội dung)'}")
    text = "\n".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n(... bị cắt vì quá dài)"


# ---------------------------------------------------------------- cổng
class Gate:
    def __init__(self, cfg, github, telegram, model=None, clock=time.time):
        self.cfg, self.gh, self.tg, self.model, self.clock = cfg, github, telegram, model, clock
        self.state = {"initialized": False, "branches": {}, "pending": {}, "reviews": [], "secret": ""}
        if os.path.exists(cfg["state_file"]):
            with open(cfg["state_file"], encoding="utf-8") as f:
                self.state.update(json.load(f))
        if not self.state["secret"]:
            self.state["secret"] = secrets.token_hex(32)
        self.secret = (cfg["secret"] or self.state["secret"]).encode()

    def save(self):
        folder = os.path.dirname(os.path.abspath(self.cfg["state_file"]))
        os.makedirs(folder, exist_ok=True)
        fetch_logs.write_file(self.cfg["state_file"], json.dumps(self.state, ensure_ascii=False))

    # ------------------------------------------------ theo dõi nhánh
    def ignored(self, name):
        return name == self.cfg["base"] or _matches(name, self.cfg["ignore"])

    def sync_branches(self):
        heads = {b["name"]: b["commit"]["sha"] for b in self.gh.pages("/branches")}
        branches, now = self.state["branches"], self.clock()
        if not self.state["initialized"]:
            # lần đầu: chỉ ghi nhận, không review nhánh cũ (tránh tốn phí hàng loạt)
            for name, sha in heads.items():
                if not self.ignored(name):
                    branches[name] = {"sha": sha, "stage": "baseline", "seen_at": now}
            self.state["initialized"] = True
            self.tg.send(f"🚦 <b>merge-gate</b> đã chạy ({esc(self.cfg['mode'])}, AI: {esc(self.cfg['ai'])}). "
                         f"Ghi nhận {len(branches)} nhánh hiện có, chỉ kiểm commit mới từ bây giờ.")
        else:
            for name in [n for n in branches if n not in heads or self.ignored(n)]:
                self.drop_pending(name)
                del branches[name]
            for name, sha in heads.items():
                if not self.ignored(name) and branches.get(name, {}).get("sha") != sha:
                    self.drop_pending(name)
                    branches[name] = {"sha": sha, "stage": "new", "seen_at": now}
        # PR vào main mở bằng tay từ nhánh cũ -> cũng phải kiểm (PR từ fork: bỏ qua)
        for pr in self.gh.pages("/pulls", state="open", base=self.cfg["base"]):
            head = pr["head"]
            rec = branches.get(head["ref"])
            if (rec and rec["stage"] == "baseline" and rec["sha"] == head["sha"]
                    and (head.get("repo") or {}).get("full_name") == self.cfg["repo"]):
                rec.update(stage="new", seen_at=now)
        self.save()

    def drop_pending(self, branch):
        for pid, p in list(self.state["pending"].items()):
            if p["branch"] == branch:
                del self.state["pending"][pid]
                if p.get("message_id"):
                    self.tg.clear_buttons(p["message_id"])

    def tick(self):
        self.expire_pending()
        self.sync_branches()
        for name in list(self.state["branches"]):
            self.advance(name)

    def advance(self, name):
        """Đẩy 1 nhánh đi qua các bước cho tới khi phải chờ."""
        rec = self.state["branches"][name]
        for _ in range(8):
            stage = rec["stage"]
            handler = getattr(self, "stage_" + stage, None)
            if not handler:
                return
            try:
                handler(name, rec)
                rec["errors"] = 0
            except Exception as e:
                rec["errors"] = rec.get("errors", 0) + 1
                log.warning("Nhánh %s, bước %s lỗi (%s lần): %s", name, stage, rec["errors"], ai.redact(str(e))[:300])
                if rec["errors"] >= MAX_ERRORS:
                    rec["errors"] = 0
                    if stage in ("report", "pr_wait"):
                        rec["stage"] = "done"
                        self.tg.send(f"⚠️ <b>{esc(name)}</b>: lỗi lặp lại ở bước {stage}, đã bỏ. Xem log merge-gate.")
                    else:
                        self.finish(rec, "error", f"lỗi lặp lại ở bước {stage}: {type(e).__name__}")
                self.save()
                return
            self.save()
            if rec["stage"] == stage:
                return

    def finish(self, rec, outcome, *reasons):
        rec["decision"] = {"outcome": outcome, "reasons": list(reasons)}
        rec["stage"] = "report"

    # ------------------------------------------------ các bước
    def ci_state(self, sha, branch, event):
        """(missing | running | failed | success, link)."""
        runs = [r for r in self.gh.get("/actions/runs", head_sha=sha, per_page=100)["workflow_runs"]
                if r.get("path", "").split("@")[0] == CI_WORKFLOW and r.get("event") == event
                and r.get("head_branch") == branch]
        if not runs:
            return "missing", ""
        run = max(runs, key=lambda r: r["id"])
        url = run.get("html_url", "")
        if run.get("status") != "completed":
            return "running", url
        if run.get("conclusion") != "success":
            return "failed", url
        jobs = self.gh.pages(f"/actions/runs/{run['id']}/jobs", key="jobs")
        for prefix in self.cfg["required_jobs"]:  # run "xanh" nhưng bỏ/skip job bắt buộc thì không tính
            matched = [j for j in jobs if j["name"].startswith(prefix)]
            if not matched or any(j.get("conclusion") != "success" for j in matched):
                return "failed", url
        return "success", url

    def compare(self, sha):
        base = urllib.parse.quote(self.cfg["base"], safe="")
        return self.gh.get(f"/compare/{base}...{sha}")

    def stage_new(self, name, rec):
        waited = self.clock() - rec["seen_at"]
        if waited < self.cfg["quiet"]:
            return
        try:
            cmp_ = self.compare(rec["sha"])
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return self.finish(rec, "error", f"không so sánh được với {self.cfg['base']} (không chung lịch sử?)")
        if not cmp_.get("ahead_by") or not cmp_.get("files"):
            rec["stage"] = "done"  # không đổi file nào so với main (vd chỉ gộp main vào nhánh) -> không tốn lượt AI
            return
        rec["title"] = cmp_["commits"][-1]["commit"]["message"].splitlines()[0][:150] if cmp_.get("commits") else ""
        status, url = self.ci_state(rec["sha"], name, "push")
        rec["ci_url"] = url
        if status == "failed":
            return self.finish(rec, "fail", "CI không đạt")
        if status == "missing" and waited > CI_MISSING_AFTER:
            return self.finish(rec, "error", "không thấy CI chạy cho commit này")
        if status == "success":
            rec["stage"] = "plan"

    def file_context(self, sha, changed):
        tree = self.gh.get(f"/git/trees/{sha}", recursive="1")["tree"]
        wanted = [t["path"] for t in tree if t.get("type") == "blob" and (
            t["path"].startswith("webapp/backend/src/") or t["path"] == "webapp/nginx/snippets/app-locations.conf")]
        wanted.sort(key=lambda p: (p not in changed, not p.endswith("server.js"), "/routes/" not in p, p))
        files, used = {}, 0
        for path in wanted:
            data = self.gh.get(f"/contents/{urllib.parse.quote(path)}", ref=sha)
            text = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
            if used + len(text) > self.cfg["context_max"]:
                continue
            files[path] = text
            used += len(text)
        return files

    def context(self, rec, with_files=True):
        cmp_ = self.compare(rec["sha"])
        files = cmp_.get("files", [])
        return {
            "commits": [c["commit"]["message"][:1000] for c in cmp_.get("commits", [])][-20:],
            "diff": build_diff(files, self.cfg["diff_max"]),
            "files": self.file_context(rec["sha"], {f["filename"] for f in files}) if with_files else {},
        }

    def stage_plan(self, name, rec):
        cmp_ = self.compare(rec["sha"])
        findings = inspect(cmp_.get("files", []), self.cfg["diff_max"])
        rec.update(base_sha=cmp_.get("base_commit", {}).get("sha"), manual=findings["manual"],
                   warnings=findings["warnings"], testable=findings["testable"])
        if findings["blocked"]:
            return self.finish(rec, "blocked", *findings["blocked"][:6])
        plan = {"understanding": "", "requirement_clear": False, "scenarios": [], "untestable": []}
        if self.cfg["ai"] != "off":
            now = self.clock()
            self.state["reviews"] = [t for t in self.state["reviews"] if now - t < 86400]
            if len(self.state["reviews"]) >= self.cfg["max_reviews"]:
                if not rec.get("limit_notified"):
                    rec["limit_notified"] = bool(self.tg.send(
                        f"⏸ <b>{esc(name)}</b>: đã hết {self.cfg['max_reviews']} lượt AI trong 24 giờ, sẽ kiểm sau."))
                return
            ctx = self.context(rec)
            self.state["reviews"].append(now)  # tính lượt TRƯỚC khi gọi: lỗi cũng đã tốn phí
            self.save()
            try:
                plan = ai.generate(self.model, ctx)
            except ai.AIError as e:
                return self.finish(rec, "error", str(e))
        if len(json.dumps({"scenarios": plan["scenarios"]}, ensure_ascii=False)) > MAX_SCENARIO_INPUT:
            return self.finish(rec, "error", "kịch bản AI soạn quá dài để gửi cho workflow")
        rec.update(plan=plan, to_run=plan["scenarios"], round=1, stage="dispatch")

    def stage_dispatch(self, name, rec):
        request_id = secrets.token_hex(8)
        scenarios = json.dumps({"scenarios": rec["to_run"]}, ensure_ascii=False) if rec["to_run"] else ""
        try:
            self.gh.send("POST", f"/actions/workflows/{TEST_WORKFLOW}/dispatches", {
                "ref": self.cfg["base"],
                "inputs": {"sha": rec["sha"], "request_id": request_id, "scenarios": scenarios}})
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404, 422):
                raise
            return self.finish(rec, "error", f"không chạy được workflow {TEST_WORKFLOW} (HTTP {e.code}): kiểm tra "
                                             f"file đã có trên {self.cfg['base']} và token có Actions: Read and write")
        rec.update(request_id=request_id, dispatched_at=self.clock(), stage="testing")

    def download_result(self, run_id):
        artifacts = self.gh.get(f"/actions/runs/{run_id}/artifacts")["artifacts"]
        found = [a for a in artifacts if a["name"] == ARTIFACT]
        if not found:
            return None
        blob = self.gh.raw(f"/actions/artifacts/{found[0]['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            if z.getinfo("result.json").file_size > 2_000_000:
                return None
            data = json.loads(z.read("result.json"))
        ok = isinstance(data.get("scenarios"), list) and all(
            isinstance(r, dict) and r.get("status") in ("passed", "failed", "error") and "name" in r and "source" in r
            for r in data["scenarios"])
        return data["scenarios"] if ok else None

    def stage_testing(self, name, rec):
        runs = self.gh.get(f"/actions/workflows/{TEST_WORKFLOW}/runs", event="workflow_dispatch", per_page=50)
        title = f"ai-test {rec['request_id']}"
        run = next((r for r in runs["workflow_runs"] if r.get("display_title") == title), None)
        if not run or run.get("status") != "completed":
            if self.clock() - rec["dispatched_at"] > TEST_TIMEOUT:
                return self.finish(rec, "error", "chạy thử quá lâu không xong")
            return
        rec["run_url"] = run.get("html_url", "")
        results = self.download_result(run["id"])
        if results is None:
            return self.finish(rec, "fail", "không có kết quả chạy thử (app không dựng được hoặc bộ chạy lỗi)")
        expected = {sc["name"][:200] for sc in rec["to_run"]}
        if not expected <= {r["name"] for r in results if r["source"] == "ai"}:
            return self.finish(rec, "fail", "kết quả thiếu kịch bản đã gửi")

        if rec["round"] == 1:
            rec["results"] = results
            errors = [r for r in results if r["source"] == "ai" and r["status"] == "error"]
            # chỉ sửa khi KHÔNG có kịch bản fail nào (đã fail thì sửa cũng vô ích, chỉ tốn phí)
            if errors and self.cfg["ai"] != "off" and not any(r["status"] == "failed" for r in results):
                try:
                    fixed = ai.repair(self.model, self.context(rec), rec["to_run"], errors)
                except ai.AIError as e:
                    log.warning("Không sửa được kịch bản: %s", e)
                    fixed = []
                if fixed:
                    rec.update(to_run=fixed, round=2, repaired=[sc["name"] for sc in fixed], stage="dispatch")
                    return
        else:
            kept = [r for r in rec["results"] if r["source"] == "ai" and r["name"] not in rec["repaired"]]
            rec["results"] = results + kept
        rec["stage"] = "review"

    def stage_review(self, name, rec):
        results, plan = rec["results"], rec["plan"]
        all_passed = bool(results) and all(r["status"] == "passed" for r in results)
        review, review_error = None, ""
        if self.cfg["ai"] != "off":
            ctx = self.context(rec)
            try:
                review = ai.review(self.model, ctx, plan["understanding"], results)
            except ai.AIError as e:
                review_error = str(e)
        rec["review"] = review
        if not all_passed:
            return self.finish(rec, "fail", "có kịch bản không đạt")
        if self.cfg["ai"] == "off":
            return self.finish(rec, "unverified", "AI đang tắt: chỉ chạy kịch bản cơ bản, chưa kiểm chức năng mới")
        if review is None:
            return self.finish(rec, "fail", f"AI không đánh giá được: {review_error}")
        if review["verdict"] != "pass":
            return self.finish(rec, "fail", "AI đánh giá không đạt")
        reasons = list(rec["manual"])
        if not plan["requirement_clear"]:
            reasons.append("chưa xác nhận yêu cầu: commit message không mô tả đủ chức năng")
        if rec["testable"] and not plan["scenarios"]:
            reasons.append("AI không soạn được kịch bản cho thay đổi này")
        reasons += [f"AI không kiểm được: {x}" for x in plan["untestable"][:3] if rec["testable"]]
        self.finish(rec, "manual" if reasons else "pass", *reasons)

    # ------------------------------------------------ báo kết quả
    def set_status(self, sha, state, description, url=""):
        if self.cfg["mode"] != "execute":
            return ""
        body = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
        if url:
            body["target_url"] = url
        try:
            self.gh.send("POST", f"/statuses/{sha}", body)
        except (OSError, ValueError) as e:
            return f"không gắn được status {STATUS_CONTEXT}: {type(e).__name__} {getattr(e, 'code', '')}"
        return ""

    HEADLINES = {
        "pass": ("✅", "ĐẠT", "success"),
        "manual": ("🟡", "ĐẠT nhưng bạn phải merge tay", "pending"),
        "unverified": ("🟡", "CHƯA KIỂM ĐỦ", "pending"),
        "fail": ("❌", "KHÔNG ĐẠT", "failure"),
        "blocked": ("⛔", "BỊ CHẶN", "failure"),
        "error": ("⚠️", "KHÔNG KIỂM ĐƯỢC", "failure"),
    }

    def render(self, name, rec, warning):
        d = rec["decision"]
        icon, label, _ = self.HEADLINES[d["outcome"]]
        lines = [f"{icon} <b>{label}</b>", f"Nhánh <b>{esc(name)}</b> @ <code>{rec['sha'][:7]}</code>"]
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
                mark = {"passed": "✅", "failed": "❌", "error": "⚠️"}[r["status"]]
                redone = " (đã soạn lại)" if r["name"] in rec.get("repaired", []) else ""
                lines.append(f"{mark} {esc(r['name'][:90])}{redone}")
                if r["status"] != "passed":
                    lines.append(f"    {esc(str(r.get('request', ''))[:80])}: {esc(ai.redact(r.get('detail', ''))[:180])}")
        review = rec.get("review")
        if review:
            lines += ["", f"<b>AI nhận xét:</b> {esc(review['summary'][:500])}"]
            for i in review["issues"][:6]:
                where = f"{i['file']}:{i['line']}" if i["line"] else i["file"]
                lines.append(f"• [{i['severity']}] {esc(where[:80])} {esc(i['detail'][:160])}")
        if d["reasons"]:
            lines += ["", "<b>Lý do:</b>"] + [f"• {esc(x[:220])}" for x in d["reasons"][:8]]
        lines += [f"⚠️ {esc(x[:200])}" for x in (rec.get("warnings") or [])[:4]]
        if warning:
            lines.append(f"⚠️ {esc(warning)}")
        footer = [""]
        for text, url in (("CI", rec.get("ci_url")), ("Chạy thử", rec.get("run_url")),
                          ("Thay đổi", f"https://github.com/{self.cfg['repo']}/compare/{self.cfg['base']}...{rec['sha']}")):
            if url:
                footer.append(f'<a href="{html.escape(url)}">{text}</a>')
        if self.cfg["mode"] != "execute":
            footer.append("<i>dry-run: không tạo PR, không merge</i>")
        tail = "\n".join(footer)
        body = ""
        for line in lines:  # cắt theo dòng để không làm vỡ thẻ HTML
            if len(body) + len(line) + len(tail) > 3900:
                body += "\n…"
                break
            body += ("\n" if body else "") + line
        return body + "\n" + tail

    def sign(self, pid, kind):
        return hmac.new(self.secret, f"{kind}:{pid}".encode(), hashlib.sha256).hexdigest()[:16]

    def stage_report(self, name, rec):
        outcome = rec["decision"]["outcome"]
        _, label, status = self.HEADLINES[outcome]
        warning = self.set_status(rec["sha"], status, label, rec.get("run_url", ""))
        buttons, pid = None, None
        if outcome in ("pass", "manual"):
            pid = rec.setdefault("pid", secrets.token_hex(6))
            approve = "✅ Tạo PR & merge" if outcome == "pass" else "📝 Tạo PR (tự merge tay)"
            buttons = [{"text": text, "callback_data": f"{kind}:{pid}:{self.sign(pid, kind)}"}
                       for kind, text in (("a", approve), ("r", "❌ Từ chối"))]
        message_id = self.tg.send(self.render(name, rec, warning), buttons)
        if message_id is None:
            raise RuntimeError("không gửi được tin Telegram")
        if pid:
            self.state["pending"][pid] = {"branch": name, "sha": rec["sha"], "auto": outcome == "pass",
                                          "message_id": message_id, "expires": self.clock() + APPROVAL_TTL}
            rec["stage"] = "awaiting"
        else:
            rec["stage"] = "done"

    def expire_pending(self):
        for pid, p in list(self.state["pending"].items()):
            if p["expires"] < self.clock():
                del self.state["pending"][pid]
                self.tg.clear_buttons(p["message_id"])
                self.tg.send(f"⌛ <b>{esc(p['branch'])}</b>: quá 24 giờ chưa duyệt, đã huỷ.")
                rec = self.state["branches"].get(p["branch"])
                if rec:
                    rec["stage"] = "done"

    # ------------------------------------------------ nút Telegram
    def is_authorized(self, chat_id, user_id):
        """Đúng chat; có GATE_APPROVER_IDS thì chỉ những người đó; chat nhóm (id âm) không khai báo thì chặn."""
        if str(chat_id) != self.cfg["tg_chat"]:
            return False
        if self.cfg["approvers"]:
            return str(user_id) in self.cfg["approvers"]
        return not self.cfg["tg_chat"].startswith("-")

    def handle_callback(self, cb):
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
        user = (cb.get("from") or {}).get("id")
        if not self.is_authorized(chat, user):
            log.warning("Bỏ qua nút bấm của người không có quyền: %s", user)
            return self.tg.answer(cb["id"], "Bạn không có quyền duyệt")
        parts = (cb.get("data") or "").split(":")
        if len(parts) != 3 or parts[0] not in ("a", "r") or not hmac.compare_digest(
                parts[2].encode(), self.sign(parts[1], parts[0]).encode()):
            return self.tg.answer(cb["id"], "Nút không hợp lệ")
        self.expire_pending()
        p = self.state["pending"].pop(parts[1], None)  # lấy ra trước khi làm: mỗi nút chỉ dùng 1 lần
        if not p:
            return self.tg.answer(cb["id"], "Yêu cầu đã xử lý hoặc hết hạn")
        self.save()
        self.tg.answer(cb["id"], "Đã nhận")
        self.tg.clear_buttons(p["message_id"])
        name, rec = p["branch"], self.state["branches"].get(p["branch"])
        title = f"<b>{esc(name)}</b>"
        if not rec or rec["sha"] != p["sha"]:
            return self.tg.send(f"⚠️ {title} đã đổi sau khi kiểm, yêu cầu bị huỷ.")
        rec["stage"] = "done"
        if parts[0] == "r":
            self.set_status(p["sha"], "failure", "Người duyệt từ chối")
            self.tg.send(f"🚫 {title}: đã từ chối.")
            return self.save()
        try:
            self.approve(name, rec, p, title)
        except (OSError, ValueError, KeyError) as e:
            self.tg.send(f"⚠️ {title}: không tạo được PR ({type(e).__name__} {getattr(e, 'code', '')}).")
        self.save()

    def head_of(self, branch):
        return self.gh.get(f"/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]

    def approve(self, name, rec, p, title):
        if self.head_of(name) != p["sha"]:
            return self.tg.send(f"⚠️ {title} có commit mới, không dùng kết quả cũ. Sẽ kiểm lại commit mới.")
        if self.cfg["mode"] != "execute":
            what = "tạo PR và merge" if p["auto"] else "tạo PR"
            return self.tg.send(f"🧪 {title}: dry-run, nếu chạy thật sẽ {what}.")
        owner = self.cfg["repo"].split("/")[0]
        found = self.gh.get("/pulls", state="open", base=self.cfg["base"], head=f"{owner}:{name}")
        pr = found[0] if found else self.gh.send("POST", "/pulls", {
            "title": rec.get("title") or f"Merge {name}", "head": name, "base": self.cfg["base"],
            "body": f"Tạo bởi merge-gate cho commit {p['sha']}.\n\nKết quả chạy thử: {rec.get('run_url', '')}"})
        link = f'<a href="{html.escape(pr["html_url"])}">PR #{pr["number"]}</a>'
        if not p["auto"]:
            return self.tg.send(f"📝 {title}: đã tạo {link}. Hãy xem và merge tay.")
        moved = self.head_of(self.cfg["base"]) != rec.get("base_sha")
        rec.update(stage="pr_wait", pr=pr["number"], pr_url=pr["html_url"], deadline=self.clock() + PR_TIMEOUT)
        note = f"\n{esc(self.cfg['base'])} đã đổi sau lúc kiểm; CI của PR sẽ kiểm lại bản gộp." if moved else ""
        self.tg.send(f"🔀 {title}: {link}, chờ CI của PR xanh rồi merge.{note}")

    def stage_pr_wait(self, name, rec):
        link = f'<a href="{html.escape(rec["pr_url"])}">PR #{rec["pr"]}</a>'

        def stop(text, icon="⚠️"):
            rec["stage"] = "done"
            self.tg.send(f"{icon} <b>{esc(name)}</b> {link}: {text}")

        pr = self.gh.get(f"/pulls/{rec['pr']}")
        if pr.get("merged"):
            return stop("đã được merge.", "🎉")
        if pr.get("state") != "open":
            return stop("PR đã bị đóng, không merge.")
        if pr["head"]["sha"] != rec["sha"]:
            return stop("có commit mới, không merge.")
        if pr.get("mergeable") is False:
            return stop("bị conflict, không merge.")
        status, _ = self.ci_state(rec["sha"], name, "pull_request")
        if status == "failed":
            return stop("CI của PR không đạt, không merge.", "❌")
        if status == "success" and pr.get("mergeable"):
            try:
                self.gh.send("PUT", f"/pulls/{rec['pr']}/merge", {"sha": rec["sha"], "merge_method": "merge"})
            except urllib.error.HTTPError as e:
                if e.code not in (403, 405, 409, 422):
                    raise
                return stop(f"GitHub từ chối merge (HTTP {e.code}): kiểm tra ruleset, quyền token, conflict.")
            return stop(f"đã merge vào {esc(self.cfg['base'])}.", "🎉")
        if self.clock() > rec["deadline"]:
            return stop("chờ CI quá 20 phút, không merge. Hãy xem và merge tay.")


# ---------------------------------------------------------------- vòng chính
def skip_backlog(telegram):
    """Bỏ qua nút bấm gửi lúc gate đang tắt: lệnh cũ không được chạy bất ngờ."""
    res = telegram.call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
    return res[-1]["update_id"] + 1 if res else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(os.environ)
    except ConfigError as e:
        log.error("Sai cấu hình: %s", e)
        return 2
    telegram = Telegram(cfg["tg_token"], cfg["tg_chat"], cfg["tg_api"])
    model = ai.bedrock_model(cfg["model_id"], cfg["region"]) if cfg["ai"] == "bedrock" else None
    gate = Gate(cfg, GitHub(cfg["github"]), telegram, model)
    gate.save()
    log.info("merge-gate chạy: repo=%s mode=%s ai=%s", cfg["repo"], cfg["mode"], cfg["ai"])
    offset, next_poll = skip_backlog(telegram), 0.0
    while True:
        if time.time() >= next_poll:
            try:
                gate.tick()
            except Exception as e:
                log.warning("Vòng kiểm tra lỗi: %s %s", type(e).__name__, getattr(e, "code", ""))
            next_poll = time.time() + cfg["poll"]
        updates = telegram.updates(offset, max(1, min(25, int(next_poll - time.time()))))
        if updates is None:
            time.sleep(5)
            continue
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            if "callback_query" in update:
                try:
                    gate.handle_callback(update["callback_query"])
                except Exception:
                    log.exception("Lỗi xử lý nút bấm")


if __name__ == "__main__":
    sys.exit(main())
