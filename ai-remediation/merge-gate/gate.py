"""merge-gate: mọi code muốn vào main phải qua CI + chạy thử chức năng + người duyệt trên Telegram.

Chạy trên máy local (1 container, không mở cổng, 1 luồng). Yêu cầu merge = PR mở vào main (hoặc /test, /merge):
  CI xanh -> kiểm tra cứng + khoanh vùng -> XIN PHÉP KIỂM -> AI soạn kịch bản -> workflow ai-test trên GitHub
  -> AI đánh giá -> XIN PHÉP MERGE -> CI của PR xanh -> merge đúng commit đã kiểm (chưa có PR thì tạo).
Push lên nhánh chưa có PR thì không làm gì. Gọi AI, chạy workflow, tạo PR, merge đều chỉ xảy ra sau khi người
được phép bấm nút.

Mặc định an toàn: GATE_AI=off (không gọi Bedrock) và GATE_MODE=dry-run (không gắn status, không tạo PR, không merge;
đã được cho phép thì vẫn kích hoạt workflow chạy thử). Biến môi trường: xem .env.mergegate.example và README2.md mục 11.
"""
import base64
import fnmatch
import hashlib
import hmac
import html
import http.client
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
import chat  # noqa: E402
import fetch_logs  # noqa: E402
import runner  # noqa: E402
import scope as scoping  # noqa: E402
import views  # noqa: E402
from views import esc  # noqa: E402

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
MAX_SAVED = 100
MAX_WAITING = 20  # số kết quả chờ merge để lưu kịch bản
WAITING_TTL = 30 * 86400
POSITIVE = ("pass", "manual", "suspect")  # kết quả còn merge được

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
                                         "GITHUB_API_URL": env.get("GITHUB_API_URL", ""),
                                         "CI_LOG_DIR": env.get("CI_LOG_DIR", ""),
                                         "CI_LOG_DAYS": env.get("CI_LOG_DAYS", ""),
                                         "CI_LOG_KEEP_DAYS": env.get("CI_LOG_KEEP_DAYS", "")})
    except fetch_logs.ConfigError as e:
        raise ConfigError(str(e).replace("GITHUB_TOKEN", "GATE_GITHUB_TOKEN")) from None
    github["workflow"] = CI_WORKFLOW  # chỉ kéo log của CI, không lấy log chạy thử ai-test
    cfg = {
        "github": github,
        "ci_logs": bool(env.get("CI_LOG_DIR")),
        "repo": github["repo"],
        "base": env.get("GATE_BASE_BRANCH") or "main",
        "mode": "execute" if clean("GATE_MODE").lower() == "execute" else "dry-run",
        "ai": "bedrock" if clean("GATE_AI").lower() == "bedrock" else "off",
        "model_id": clean("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0").removeprefix("bedrock/"),
        "region": clean("BEDROCK_REGION"),
        "poll": max(15, number("GATE_POLL_SECONDS", 60)),
        "quiet": max(0, number("GATE_QUIET_SECONDS", 120)),
        "max_reviews": max(0, number("GATE_MAX_REVIEWS_PER_DAY", 10)),
        "max_chats": max(0, number("GATE_MAX_CHATS_PER_DAY", 30)),
        "max_retests": max(0, number("GATE_MAX_RETESTS", 2)),
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
    def __init__(self, token, chat_id, api):
        self.token, self.chat, self.api = token, chat_id, api

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

    def _message(self, text, buttons):
        body = {"chat_id": self.chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}
        return body

    def send(self, text, buttons=None):
        return (self.call("sendMessage", self._message(text, buttons), timeout=20) or {}).get("message_id")

    def edit(self, message_id, text):
        self.call("editMessageText", {**self._message(text, None), "message_id": message_id}, timeout=15)

    def clear_buttons(self, message_id):
        self.call("editMessageReplyMarkup", {"chat_id": self.chat, "message_id": message_id,
                                             "reply_markup": {"inline_keyboard": []}}, timeout=15)

    def answer(self, callback_id, text):
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]}, timeout=15)

    def updates(self, offset, timeout):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["callback_query", "message"]}, timeout=timeout + 15)


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
        if path.startswith(scoping.FEATURE_DIR) or old.startswith(scoping.FEATURE_DIR):
            warnings.append(f"nhánh sửa mô tả chức năng (chuẩn chấm), hãy đọc kỹ: {path}")

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


def saved_key(scenario):
    return hashlib.sha256(json.dumps(scenario["steps"], sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- cổng
class Gate(chat.ChatMixin):
    def __init__(self, cfg, github, telegram, model=None, clock=time.time):
        self.cfg, self.gh, self.tg, self.model, self.clock = cfg, github, telegram, model, clock
        self.state = {"initialized": False, "branches": {}, "pending": {}, "reviews": [], "chat_calls": [],
                      "saved": [], "waiting": [], "session": None, "secret": ""}
        if os.path.exists(cfg["state_file"]):
            with open(cfg["state_file"], encoding="utf-8") as f:
                self.state.update(json.load(f))
        if not self.state["secret"]:
            self.state["secret"] = secrets.token_hex(32)
        self.secret = (cfg["secret"] or self.state["secret"]).encode()
        self.logged_runs = set()  # lần chạy CI đã kéo đủ log (chỉ nhớ trong phiên chạy)

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
            # lần đầu: chỉ ghi nhận, không hỏi về nhánh cũ
            for name, sha in heads.items():
                if not self.ignored(name):
                    branches[name] = {"sha": sha, "stage": "baseline", "seen_at": now}
            self.state["initialized"] = True
            self.tg.send(f"🚦 <b>merge-gate</b> đã chạy ({esc(self.cfg['mode'])}, AI: {esc(self.cfg['ai'])}). "
                         f"Chỉ kiểm khi có PR mở vào {esc(self.cfg['base'])} (hoặc gõ /test). /help")
        else:
            for name in [n for n in branches if n not in heads or self.ignored(n)]:
                self.drop_pending(name)
                del branches[name]
            for name, sha in heads.items():
                if not self.ignored(name) and branches.get(name, {}).get("sha") != sha:
                    self.drop_pending(name)
                    branches[name] = {"sha": sha, "stage": "baseline", "seen_at": now}  # push thôi thì chưa làm gì
        self.sync_requests(now)
        self.save()

    def sync_requests(self, now):
        """PR đang mở vào main = yêu cầu merge. PR từ fork: bỏ qua."""
        requests = {pr["head"]["ref"]: pr for pr in self.gh.pages("/pulls", state="open", base=self.cfg["base"])
                    if (pr["head"].get("repo") or {}).get("full_name") == self.cfg["repo"]}
        for name, rec in list(self.state["branches"].items()):
            pr = requests.get(name)
            if pr is None:
                if rec.get("pr") and rec["stage"] != "pr_wait":  # pr_wait tự xử lý PR của nó
                    self.withdraw(name, rec)
                continue
            if pr.get("draft") or pr["head"]["sha"] != rec["sha"] or rec.get("pr") == pr["number"]:
                continue
            link = {"pr": pr["number"], "pr_url": pr.get("html_url", "")}
            positive = (rec.get("decision") or {}).get("outcome") in POSITIVE
            if rec["stage"] == "done" and positive:  # đã kiểm đúng commit này -> gửi lại thẻ, không tốn lượt AI
                rec.update(link, stage="report")
            elif rec["stage"] in ("baseline", "done"):
                self.state["branches"][name] = {"sha": rec["sha"], "stage": "new", "seen_at": now, **link}
            else:  # đang xử lý (do /test): chỉ gắn PR vào
                rec.update(link)

    def withdraw(self, name, rec):
        """PR không còn mở (đóng, hoặc merge ngoài bot): huỷ việc đang chờ."""
        number = rec["pr"]
        if rec["stage"] not in ("done", "baseline"):
            merged = self.gh.get(f"/pulls/{number}").get("merged")
            self.drop_pending(name)
            rec["stage"] = "done"
            what = "đã được merge" if merged else "đã đóng"
            self.tg.send(f"⏹ <b>{esc(name)}</b>: PR #{number} {what}, huỷ yêu cầu đang chờ.")
        del rec["pr"]
        rec.pop("pr_url", None)

    def drop_pending(self, branch):
        for pid, p in list(self.state["pending"].items()):
            if p["branch"] == branch:
                del self.state["pending"][pid]
                if p.get("message_id"):
                    self.tg.clear_buttons(p["message_id"])

    def tick(self):
        self.expire_pending()
        self.sync_branches()
        self.save_merged()
        self.pull_ci_logs()
        for name in list(self.state["branches"]):
            self.advance(name)

    def pull_ci_logs(self):
        """CI hỏng -> tải log THÔ của job hỏng về đĩa, xoá log quá hạn (chỉ đọc GitHub, không AI, không nhắn tin)."""
        if not self.cfg["ci_logs"]:
            return
        try:
            fetch_logs.cleanup(self.cfg["github"], now=self.clock())
            fetch_logs.fetch(self.cfg["github"], now=self.clock(), seen=self.logged_runs)
        except (OSError, ValueError, KeyError, http.client.HTTPException) as e:  # không được làm hỏng việc kiểm
            log.warning("Không kéo được log CI: %s", type(e).__name__)

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

    def progress(self, name, rec, current):
        """Một tin duy nhất, sửa dần theo tiến trình. Lỗi gửi không được làm hỏng luồng kiểm."""
        text = views.progress(name, rec, current, self.cfg["ai"] != "off")
        if rec.get("progress_id"):
            self.tg.edit(rec["progress_id"], text)
        else:
            rec["progress_id"] = self.tg.send(text)

    # ------------------------------------------------ nút bấm
    def sign(self, pid, kind):
        return hmac.new(self.secret, f"{kind}:{pid}".encode(), hashlib.sha256).hexdigest()[:16]

    def send_card(self, name, rec, text, buttons, **extra):
        """Gửi tin kèm nút và ghi lại yêu cầu chờ duyệt. buttons = [(loại, nhãn)]. Trả False nếu không gửi được."""
        pid = secrets.token_hex(6)
        markup = [{"text": label, "callback_data": f"{kind}:{pid}:{self.sign(pid, kind)}"} for kind, label in buttons]
        message_id = self.tg.send(text, markup)
        if message_id is None:
            return False
        self.drop_pending(name)
        self.state["pending"][pid] = {"branch": name, "sha": rec["sha"], "kinds": [k for k, _ in buttons],
                                      "message_id": message_id, "expires": self.clock() + APPROVAL_TTL, **extra}
        return True

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

    def diff_of(self, sha):
        return build_diff(self.compare(sha).get("files", []), self.cfg["diff_max"])

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
            rec["stage"] = "done"  # không đổi file nào so với main (vd chỉ gộp main vào nhánh)
            return
        rec["title"] = cmp_["commits"][-1]["commit"]["message"].splitlines()[0][:150] if cmp_.get("commits") else ""
        status, url = self.ci_state(rec["sha"], name, "pull_request" if rec.get("pr") else "push")
        rec["ci_url"] = url
        if status == "failed":
            return self.finish(rec, "fail", "CI không đạt")
        if status == "missing" and (waited > CI_MISSING_AFTER or rec.get("requested")):
            hint = "PR bị conflict thì GitHub không chạy CI" if rec.get("pr") else "nhánh cũ thì gộp main vào rồi push"
            return self.finish(rec, "error", f"không thấy CI chạy cho commit này ({hint})")
        if status == "success":
            rec["stage"] = "ask"

    def read_file(self, path, ref):
        data = self.gh.get(f"/contents/{urllib.parse.quote(path)}", ref=ref)
        return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")

    def tree(self, ref):
        return [t["path"] for t in self.gh.get(f"/git/trees/{ref}", recursive="1")["tree"] if t.get("type") == "blob"]

    def stage_ask(self, name, rec):
        """Kiểm tra cứng và khoanh vùng (không tốn phí) rồi XIN PHÉP kiểm."""
        cmp_ = self.compare(rec["sha"])
        files = cmp_.get("files", [])
        findings = inspect(files, self.cfg["diff_max"])
        rec.update(base_sha=cmp_.get("base_commit", {}).get("sha"), manual=findings["manual"],
                   warnings=findings["warnings"], testable=findings["testable"],
                   changed=[f["filename"] for f in files][:60])
        if findings["blocked"]:
            return self.finish(rec, "blocked", *findings["blocked"][:6])
        sources = {p: self.read_file(p, rec["sha"]) for p in self.tree(rec["sha"])
                   if p.startswith(scoping.SRC) and p.endswith(".js")}
        # mô tả chức năng lấy ở MAIN: nhánh được kiểm không tự đổi được chuẩn chấm
        docs = {p: self.read_file(p, rec["base_sha"] or self.cfg["base"])
                for p in self.tree(rec["base_sha"] or self.cfg["base"])
                if p.startswith(scoping.FEATURE_DIR) and p.endswith(".md")}
        rec["scope"] = scoping.analyze(files, sources, docs)
        used = len([t for t in self.state["reviews"] if self.clock() - t < 86400])
        text = views.test_card(name, rec, self.cfg["ai"] != "off", len(self.pick_saved(rec, 20)), used, self.cfg["max_reviews"])
        if not self.send_card(name, rec, text, [("t", "✅ Cho phép kiểm"), ("x", "❌ Bỏ qua")]):
            raise RuntimeError("không gửi được tin Telegram")
        rec["stage"] = "awaiting_test"

    def context(self, rec):
        """Chỉ những gì liên quan tới thay đổi (theo scope đã khoanh)."""
        cmp_ = self.compare(rec["sha"])
        base = rec.get("base_sha") or self.cfg["base"]
        wanted = rec["scope"]["files"]
        existing = set(self.tree(rec["sha"]))
        files, used = {}, 0
        for path in wanted:
            if path not in existing:
                continue
            text = self.read_file(path, rec["sha"])
            if used + len(text) > self.cfg["context_max"]:
                continue
            files[path] = text
            used += len(text)
        features = {}
        for path in self.tree(base):
            if path.startswith(scoping.FEATURE_DIR) and path.endswith(".md"):
                text = self.read_file(path, base)
                if scoping.parse_features({path: text})[0]["name"] in rec["scope"]["features"]:
                    features[path] = text[:8000]
        return {
            "commits": [c["commit"]["message"][:1000] for c in cmp_.get("commits", [])][-20:],
            "diff": build_diff(cmp_.get("files", []), self.cfg["diff_max"]),
            "files": files, "features": features, "endpoints": rec["scope"]["endpoints"],
        }

    def pick_saved(self, rec, room):
        """Kịch bản đã lưu thuộc chức năng bị ảnh hưởng, mới nhất trước."""
        scope = rec.get("scope") or {"wide": True, "features": []}
        wanted = set(scope["features"])
        picked = []
        for entry in reversed(self.state["saved"]):
            related = scope["wide"] or not entry["features"] or wanted & set(entry["features"])
            if related and len(picked) < room:
                picked.append({"name": f"[cũ] {entry['scenario']['name']}"[:190], "source": "saved",
                               "steps": entry["scenario"]["steps"]})
        return picked

    def stage_plan(self, name, rec):
        plan = {"understanding": "", "requirement_clear": False, "scenarios": [], "untestable": []}
        if self.cfg["ai"] != "off":
            now = self.clock()
            self.state["reviews"] = [t for t in self.state["reviews"] if now - t < 86400]
            if len(self.state["reviews"]) >= self.cfg["max_reviews"]:
                return self.finish(rec, "error", f"đã hết {self.cfg['max_reviews']} lần kiểm bằng AI trong 24 giờ")
            self.progress(name, rec, "plan")
            ctx = self.context(rec)
            self.state["reviews"].append(now)  # tính lượt TRƯỚC khi gọi: lỗi cũng đã tốn phí
            self.save()
            try:
                plan = ai.generate(self.model, ctx, rec.get("notes", ""), rec.get("previous_errors"))
            except ai.AIError as e:
                return self.finish(rec, "error", str(e))
        to_run = plan["scenarios"] + self.pick_saved(rec, runner.MAX_SCENARIOS - len(plan["scenarios"]))

        def too_big():
            steps = sum(len(sc["steps"]) for sc in to_run)
            size = len(json.dumps({"scenarios": to_run}, ensure_ascii=False))
            return size > MAX_SCENARIO_INPUT or steps > runner.MAX_STEPS_TOTAL

        while too_big() and to_run and to_run[-1].get("source") == "saved":
            to_run.pop()
        if too_big():
            return self.finish(rec, "error", "kịch bản AI soạn quá dài để gửi cho workflow")
        rec.update(plan=plan, to_run=to_run, round=1, repaired=[], stage="dispatch")

    def stage_dispatch(self, name, rec):
        self.progress(name, rec, "test")
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
        if not expected <= {r["name"] for r in results if r["source"] != "baseline"}:
            return self.finish(rec, "fail", "kết quả thiếu kịch bản đã gửi (bộ chạy trên main đã đúng phiên bản chưa?)")

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
                    names = [sc["name"] for sc in fixed]
                    merged = {sc["name"]: sc for sc in fixed}
                    rec.update(final=[merged.get(sc["name"], sc) for sc in rec["to_run"]],
                               to_run=fixed, round=2, repaired=names, stage="dispatch")
                    return
                log.warning("Nhánh %s: không có bản sửa hợp lệ cho %d kịch bản lỗi", name, len(errors))
        else:
            kept = [r for r in rec["results"] if r["source"] != "baseline" and r["name"] not in rec["repaired"]]
            rec["results"] = results + kept
        rec["stage"] = "review"

    def stage_review(self, name, rec):
        self.progress(name, rec, "review")
        results, plan = rec["results"], rec["plan"]
        bad = [r for r in results if r["status"] != "passed"]
        review, review_error = None, ""
        if self.cfg["ai"] != "off":
            ctx = self.context(rec)
            try:
                review = ai.review(self.model, ctx, plan["understanding"], results)
            except ai.AIError as e:
                review_error = str(e)
        rec["review"] = review
        if bad or not results:
            if review and bad and all(r["source"] == "ai" for r in bad) and not any(i["severity"] == "high" for i in review["issues"]):
                causes = {s["scenario"]: s["cause"] for s in review["suspects"]}
                if all(causes.get(r["name"]) == "test" for r in bad):
                    return self.finish(rec, "suspect", f"{len(bad)} kịch bản mới không đạt, AI nghi do kịch bản (có thể sai)",
                                       "kịch bản cơ bản và đã lưu đều đạt")
            return self.finish(rec, "fail", "có kịch bản không đạt")
        if self.cfg["ai"] == "off":
            return self.finish(rec, "unverified", "AI đang tắt: chỉ chạy kịch bản có sẵn, chưa kiểm chức năng mới")
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

    def result_buttons(self, rec):
        outcome = rec["decision"]["outcome"]
        has_pr = bool(rec.get("pr"))
        bad = [r for r in rec.get("results") or [] if r["status"] != "passed"]
        can_retest = rec.get("scope") is not None and rec.get("retests", 0) < self.cfg["max_retests"]
        buttons = []
        if outcome == "pass":
            buttons.append(("a", "✅ Merge vào main" if has_pr else "✅ Tạo PR & merge"))
        if outcome == "manual" and not has_pr:  # đã có PR thì bạn merge tay trên GitHub, không cần nút
            buttons.append(("a", "📝 Tạo PR (tự merge tay)"))
        if outcome == "suspect" and not has_pr:
            buttons.append(("a", "📝 Tạo PR, tôi tự merge"))
        if outcome in ("suspect", "fail", "error") and can_retest:
            buttons.append(("n", "🔁 Test lại"))
            if any(r["source"] == "saved" for r in bad):
                buttons.append(("z", "🗑 Bỏ test cũ bị lỗi & test lại"))
        if bad:
            buttons.append(("d", "📄 Chi tiết"))
        if outcome in POSITIVE:
            buttons.append(("r", "❌ Từ chối"))
        return buttons

    def stage_report(self, name, rec):
        outcome = rec["decision"]["outcome"]
        _, label, status = views.HEADLINES[outcome]
        if rec.get("progress_id"):
            self.progress(name, rec, "done")
        warning = self.set_status(rec["sha"], status, label, rec.get("run_url", ""))
        text = views.result(self.cfg, name, rec, warning)
        buttons = self.result_buttons(rec)
        if buttons:
            if not self.send_card(name, rec, text, buttons, auto=outcome == "pass"):
                raise RuntimeError("không gửi được tin Telegram")
            rec["stage"] = "awaiting"
        else:
            if self.tg.send(text) is None:
                raise RuntimeError("không gửi được tin Telegram")
            rec["stage"] = "done"
        if outcome in POSITIVE:
            self.queue_saved(name, rec)

    def expire_pending(self):
        for pid, p in list(self.state["pending"].items()):
            if p["expires"] < self.clock():
                del self.state["pending"][pid]
                self.tg.clear_buttons(p["message_id"])
                self.tg.send(f"⌛ <b>{esc(p['branch'])}</b>: quá 24 giờ chưa bấm, đã huỷ. Gõ /test hoặc /merge để làm lại.")
                rec = self.state["branches"].get(p["branch"])
                if rec and rec["stage"] in ("awaiting", "awaiting_test"):
                    rec["stage"] = "done"

    # ------------------------------------------------ xử lý nút
    def is_authorized(self, chat_id, user_id):
        """Đúng chat; có GATE_APPROVER_IDS thì chỉ những người đó; chat nhóm (id âm) không khai báo thì chặn."""
        if str(chat_id) != self.cfg["tg_chat"]:
            return False
        if self.cfg["approvers"]:
            return str(user_id) in self.cfg["approvers"]
        return not self.cfg["tg_chat"].startswith("-")

    def handle_callback(self, cb):
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        user = (cb.get("from") or {}).get("id")
        if not self.is_authorized(chat_id, user):
            log.warning("Bỏ qua nút bấm của người không có quyền: %s", user)
            return self.tg.answer(cb["id"], "Bạn không có quyền duyệt")
        parts = (cb.get("data") or "").split(":")
        if len(parts) != 3 or not hmac.compare_digest(parts[2].encode(), self.sign(parts[1], parts[0]).encode()):
            return self.tg.answer(cb["id"], "Nút không hợp lệ")
        kind, pid = parts[0], parts[1]
        self.expire_pending()
        p = self.state["pending"].get(pid)
        if not p or kind not in p.get("kinds", []):
            return self.tg.answer(cb["id"], "Yêu cầu đã xử lý hoặc hết hạn")
        name, rec = p["branch"], self.state["branches"].get(p["branch"])
        current = rec is not None and rec["sha"] == p["sha"]
        if kind == "d":  # chỉ xem: không tiêu nút
            self.tg.answer(cb["id"], "Đang gửi chi tiết")
            if current:
                self.tg.send(views.details(name, rec))
            return
        del self.state["pending"][pid]  # lấy ra trước khi làm: mỗi thẻ chỉ dùng 1 lần
        self.save()
        self.tg.answer(cb["id"], "Đã nhận")
        self.tg.clear_buttons(p["message_id"])
        title = f"<b>{esc(name)}</b>"
        if not current:
            return self.tg.send(f"⚠️ {title} đã đổi sau lúc gửi thẻ, yêu cầu bị huỷ.")
        if kind in ("t", "x") and rec["stage"] != "awaiting_test" or kind not in ("t", "x") and rec["stage"] != "awaiting":
            return self.tg.send(f"⚠️ {title} đang ở bước khác, yêu cầu bị huỷ.")
        if kind == "x":
            rec["stage"] = "done"
            self.tg.send(f"⏭ {title}: bỏ qua, không kiểm. Gõ /test {esc(name)} khi cần.")
        elif kind in ("t", "n", "z"):
            if kind != "t":
                self.unqueue_saved(name, p["sha"])
                self.prepare_retest(rec, drop_saved=kind == "z")
            rec["stage"] = "plan"
            self.save()
            self.advance(name)
        elif kind == "r":
            rec["stage"] = "done"
            self.set_status(p["sha"], "failure", "Người duyệt từ chối")
            self.unqueue_saved(name, p["sha"])
            self.tg.send(f"🚫 {title}: đã từ chối.")
        else:
            rec["stage"] = "done"
            try:
                self.approve(name, rec, p, title)
            except (OSError, ValueError, KeyError) as e:
                self.tg.send(f"⚠️ {title}: không tạo được PR ({type(e).__name__} {getattr(e, 'code', '')}).")
        self.save()

    def prepare_retest(self, rec, drop_saved):
        bad = [r for r in rec.get("results") or [] if r["status"] != "passed"]
        if drop_saved:
            broken = {r["name"] for r in bad if r["source"] == "saved"}
            self.state["saved"] = [s for s in self.state["saved"]
                                   if f"[cũ] {s['scenario']['name']}"[:190] not in broken]
        rec["retests"] = rec.get("retests", 0) + 1
        rec["previous_errors"] = [{k: r.get(k) for k in ("name", "request", "expected", "detail")}
                                  for r in bad if r["source"] == "ai"][:6]
        for key in ("results", "review", "decision", "run_url", "progress_id", "to_run", "final", "plan"):
            rec.pop(key, None)

    def unqueue_saved(self, name, sha):
        self.state["waiting"] = [w for w in self.state["waiting"] if (w["branch"], w["sha"]) != (name, sha)]

    def queue_saved(self, name, rec):
        """Kịch bản mới đã đạt CHỜ tới khi đúng commit này được merge: trước đó main chưa có chức năng."""
        passed = {r["name"] for r in rec.get("results") or [] if r["source"] == "ai" and r["status"] == "passed"}
        scenarios = rec.get("final") or rec.get("to_run") or []
        scope = rec.get("scope") or {}
        # không khoanh vùng được -> để trống = lần nào cũng chạy. "Chung toàn hệ thống" luôn được chọn nên không giúp phân loại
        features = [] if scope.get("wide") else [f for f in scope.get("features", []) if f != "Chung toàn hệ thống"]
        keep = [{"name": sc["name"], "steps": sc["steps"]} for sc in scenarios
                if sc.get("source") != "saved" and sc["name"] in passed]
        self.unqueue_saved(name, rec["sha"])
        if keep:
            self.state["waiting"].append({"branch": name, "sha": rec["sha"], "features": features,
                                          "scenarios": keep, "until": self.clock() + WAITING_TTL})
        self.state["waiting"] = self.state["waiting"][-MAX_WAITING:]

    def save_merged(self):
        """PR của commit đang chờ đã merge (bot hoặc người merge) -> lưu kịch bản để lần sau chạy lại."""
        owner = self.cfg["repo"].split("/")[0]
        for entry in list(self.state["waiting"]):
            try:
                closed = self.gh.get("/pulls", state="closed", base=self.cfg["base"],
                                     head=f"{owner}:{entry['branch']}", per_page=30)
                merged = any(p.get("merged_at") and p["head"]["sha"] == entry["sha"] for p in closed)
            except (OSError, ValueError, KeyError) as e:
                log.warning("Không kiểm tra được PR đã merge của %s: %s", entry["branch"], type(e).__name__)
                continue
            if not merged and entry["until"] > self.clock():
                continue
            self.state["waiting"].remove(entry)
            if not merged:
                continue
            known = {saved_key(s["scenario"]) for s in self.state["saved"]}
            added = 0
            for sc in entry["scenarios"]:
                if saved_key(sc) not in known:
                    known.add(saved_key(sc))
                    self.state["saved"].append({"features": entry["features"], "sha": entry["sha"], "scenario": sc})
                    added += 1
            self.state["saved"] = self.state["saved"][-MAX_SAVED:]
            self.save()
            if added:
                self.tg.send(f"💾 <b>{esc(entry['branch'])}</b> đã merge: lưu {added} kịch bản để chạy lại lần sau.")

    def head_of(self, branch):
        return self.gh.get(f"/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]

    def approve(self, name, rec, p, title):
        if self.head_of(name) != p["sha"]:
            return self.tg.send(f"⚠️ {title} có commit mới, không dùng kết quả cũ. Sẽ kiểm lại commit mới.")
        if self.cfg["mode"] != "execute":
            what = ("merge PR" if rec.get("pr") else "tạo PR và merge") if p["auto"] else "tạo PR"
            return self.tg.send(f"🧪 {title}: dry-run, nếu chạy thật sẽ {what}.")
        owner = self.cfg["repo"].split("/")[0]
        found = self.gh.get("/pulls", state="open", base=self.cfg["base"], head=f"{owner}:{name}")
        if rec.get("pr") and not any(x["number"] == rec["pr"] for x in found):
            return self.tg.send(f"⚠️ {title}: PR #{rec['pr']} không còn mở, không merge.")
        pr = found[0] if found else self.gh.send("POST", "/pulls", {
            "title": rec.get("title") or f"Merge {name}", "head": name, "base": self.cfg["base"],
            "body": f"Tạo bởi merge-gate cho commit {p['sha']}.\n\nKết quả chạy thử: {rec.get('run_url', '')}"})
        link = f'<a href="{html.escape(pr["html_url"])}">PR #{pr["number"]}</a>'
        rec.update(pr=pr["number"], pr_url=pr["html_url"])
        if not p["auto"]:
            return self.tg.send(f"📝 {title}: {link}. Hãy xem và merge tay.")
        moved = self.head_of(self.cfg["base"]) != rec.get("base_sha")
        rec.update(stage="pr_wait", deadline=self.clock() + PR_TIMEOUT)
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
    """Bỏ qua tin và nút bấm gửi lúc gate đang tắt: lệnh cũ không được chạy bất ngờ."""
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
            try:
                if "callback_query" in update:
                    gate.handle_callback(update["callback_query"])
                elif "message" in update:
                    gate.handle_message(update["message"])
            except Exception as e:
                log.warning("Lỗi xử lý tin Telegram: %s %s", type(e).__name__, ai.redact(str(e))[:300])


if __name__ == "__main__":
    sys.exit(main())
