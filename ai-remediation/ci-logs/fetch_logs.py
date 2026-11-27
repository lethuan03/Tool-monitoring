"""Kéo log job GitHub Actions về đĩa. Chỉ ĐỌC từ GitHub, không gọi AI, không nhắn tin.

Kết quả (log THÔ, chưa lọc — lớp filter làm sau sẽ đọc từ đây):
    <CI_LOG_DIR>/<run_id>/<job_id>.log    nội dung log
    <CI_LOG_DIR>/<run_id>/<job_id>.json   thông tin job (nhánh, commit, step lỗi, link...)
Job đã có file .log thì bỏ qua, nên chạy lặp lại (cron) không tải trùng.

Biến môi trường:
    GITHUB_REPO         bắt buộc, dạng owner/repo
    GITHUB_TOKEN        bắt buộc, fine-grained token quyền "Actions: Read-only"
    CI_LOG_DIR          mặc định: thư mục data/ cạnh file này
    CI_LOG_DAYS         chỉ lấy lần chạy tạo trong N ngày gần đây (mặc định 5)
    CI_LOG_KEEP_DAYS    tự xoá log đã tải quá N ngày (mặc định 14, 0 = không xoá; tối thiểu CI_LOG_DAYS + 1)
    CI_LOG_CONCLUSIONS  kết quả job cần lấy, cách nhau dấu phẩy (mặc định failure)
    GITHUB_API_URL      mặc định https://api.github.com (đổi khi test)

Chạy: python fetch_logs.py
Mã thoát: 0 = xong · 1 = có job tải lỗi · 2 = sai cấu hình hoặc không gọi được GitHub
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 30
PER_PAGE = 100
MAX_PAGES = 10
NETWORK_ERRORS = (OSError, http.client.HTTPException)  # URLError/HTTPError là con của OSError


class ConfigError(Exception):
    pass


def load_config(env):
    repo = env.get("GITHUB_REPO", "").strip()
    token = env.get("GITHUB_TOKEN", "").strip()
    # tên repo không được là "." hay ".." (repo được ghép vào đường dẫn API)
    if not re.fullmatch(r"[A-Za-z0-9-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+", repo):
        raise ConfigError("GITHUB_REPO phải có dạng owner/repo")
    if not token:
        raise ConfigError("Thiếu GITHUB_TOKEN")
    try:
        days = int(env.get("CI_LOG_DAYS") or 5)
    except ValueError:
        raise ConfigError("CI_LOG_DAYS phải là số nguyên") from None
    if days < 1:
        raise ConfigError("CI_LOG_DAYS phải >= 1")
    try:
        keep = int(env.get("CI_LOG_KEEP_DAYS") or 14)
    except ValueError:
        raise ConfigError("CI_LOG_KEEP_DAYS phải là số nguyên") from None
    if keep < 0:
        raise ConfigError("CI_LOG_KEEP_DAYS phải >= 0")
    wanted = {c.strip() for c in (env.get("CI_LOG_CONCLUSIONS") or "failure").split(",") if c.strip()}
    if not wanted:
        raise ConfigError("CI_LOG_CONCLUSIONS không có giá trị nào")
    return {
        "repo": repo,
        "token": token,
        "days": days,
        # giữ lâu hơn khoảng tải, nếu không log vừa xoá sẽ bị tải lại
        "keep_days": max(keep, days + 1) if keep else 0,
        "conclusions": wanted,
        "out_dir": env.get("CI_LOG_DIR") or os.path.join(HERE, "data"),
        "api": (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/"),
    }


def github_get(cfg, path):
    req = urllib.request.Request(f"{cfg['api']}/repos/{cfg['repo']}{path}")
    # API log trả chuyển hướng sang nơi chứa file: token KHÔNG được đi theo sang địa chỉ đó
    req.add_unredirected_header("Authorization", f"Bearer {cfg['token']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "ci-log-fetcher")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def github_list(cfg, path, key, params):
    """Đọc hết các trang của một API trả danh sách (tối đa MAX_PAGES trang)."""
    items = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({**params, "per_page": PER_PAGE, "page": page})
        batch = json.loads(github_get(cfg, f"{path}?{query}"))[key]
        items.extend(batch)
        if len(batch) < PER_PAGE:
            return items
    print(f"Cảnh báo: {path} còn nữa, chỉ đọc {len(items)} mục đầu", file=sys.stderr)
    return items


def write_file(path, text):
    """Ghi qua file tạm rồi đổi tên: bị ngắt giữa chừng không để lại file dở."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def cleanup(cfg, now=None):
    """Xoá log đã tải quá cfg["keep_days"] ngày. Trả số file đã xoá.

    Chỉ đụng đúng thứ do tool này tạo: <số>/<số>.log|.json (và file .tmp dở). Thứ khác trong thư mục để nguyên.
    """
    if not cfg["keep_days"] or not os.path.isdir(cfg["out_dir"]):
        return 0
    limit = (time.time() if now is None else now) - cfg["keep_days"] * 86400
    removed = 0
    with os.scandir(cfg["out_dir"]) as runs:
        folders = [r.path for r in runs if r.name.isdigit() and r.is_dir(follow_symlinks=False)]
    for folder in folders:
        with os.scandir(folder) as files:
            old = [f.path for f in files if re.fullmatch(r"\d+\.(log|json)(\.tmp)?", f.name)
                   and f.is_file(follow_symlinks=False) and f.stat(follow_symlinks=False).st_mtime < limit]
        for path in old:
            os.remove(path)
        removed += len(old)
        if old and not os.listdir(folder):
            os.rmdir(folder)
    if removed:
        print(f"Đã xoá {removed} file log cũ hơn {cfg['keep_days']} ngày")
    return removed


def job_meta(cfg, run, job):
    return {
        "repo": cfg["repo"],
        "run_id": run["id"],
        "job_id": job["id"],
        "workflow": run.get("name"),
        "job": job.get("name"),
        "conclusion": job.get("conclusion"),
        "event": run.get("event"),
        "branch": run.get("head_branch"),
        "commit": run.get("head_sha"),
        # repo chứa commit được chạy: khác "repo" nghĩa là PR từ fork (nội dung log không đáng tin)
        "head_repo": (run.get("head_repository") or {}).get("full_name"),
        "failed_steps": [s["name"] for s in job.get("steps") or [] if s.get("conclusion") == "failure"],
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "url": job.get("html_url"),
    }


def fetch(cfg, now=None, seen=None):
    """Tải log còn thiếu. Trả (danh sách file .log mới, số lỗi).

    cfg["workflow"] (tuỳ chọn): chỉ lấy lần chạy của file workflow này.
    seen (tuỳ chọn): tập các lần chạy đã lấy đủ, để người gọi lặp lại khỏi hỏi GitHub lần nữa.
    """
    now = time.time() if now is None else now
    since = time.strftime("%Y-%m-%d", time.gmtime(now - cfg["days"] * 86400))
    runs = github_list(cfg, "/actions/runs", "workflow_runs", {"status": "completed", "created": f">={since}"})

    saved, errors = [], 0
    for run in runs:
        # Lần chạy xanh không có job fail -> khỏi hỏi danh sách job (đỡ tốn lượt gọi API)
        if run.get("conclusion") == "success" and "success" not in cfg["conclusions"]:
            continue
        if cfg.get("workflow") and run.get("path", "").split("@")[0] != cfg["workflow"]:
            continue
        run_id = int(run["id"])  # ép số: id dùng làm tên thư mục/file
        attempt = (run_id, run.get("run_attempt"))  # chạy lại job hỏng = cùng id, lần thử mới
        if seen is not None and attempt in seen:
            continue
        errors_before = errors
        try:
            jobs = github_list(cfg, f"/actions/runs/{run_id}/jobs", "jobs", {})
        except NETWORK_ERRORS as e:
            print(f"Lỗi đọc job của run {run_id}: {e}", file=sys.stderr)
            errors += 1
            continue
        for job in jobs:
            if job.get("conclusion") not in cfg["conclusions"]:
                continue
            job_id = int(job["id"])
            base = os.path.join(cfg["out_dir"], str(run_id), str(job_id))
            if os.path.exists(base + ".log"):
                continue
            try:
                text = github_get(cfg, f"/actions/jobs/{job_id}/logs").decode("utf-8-sig", "replace")
            except NETWORK_ERRORS as e:
                print(f"Lỗi tải log job {job_id} ({job.get('name')}): {e}", file=sys.stderr)
                errors += 1
                continue
            os.makedirs(os.path.dirname(base), exist_ok=True)
            # .json trước, .log sau: có file .log nghĩa là job đã lưu đủ
            write_file(base + ".json", json.dumps(job_meta(cfg, run, job), ensure_ascii=False, indent=2))
            write_file(base + ".log", text)
            saved.append(base + ".log")
            print(f"Đã lưu: {job.get('name')} -> {base}.log")
        if seen is not None and errors == errors_before:
            seen.add(attempt)
    return saved, errors


def main(env=None):
    for stream in (sys.stdout, sys.stderr):  # Windows chuyển hướng ra file hay dùng cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(os.environ if env is None else env)
        cleanup(cfg)
        saved, errors = fetch(cfg)
    except ConfigError as e:
        print(f"Sai cấu hình: {e}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as e:
        hint = " (kiểm tra token, quyền Actions: Read-only và tên repo)" if e.code in (401, 403, 404) else ""
        print(f"GitHub trả lỗi HTTP {e.code}{hint}", file=sys.stderr)
        return 2
    except NETWORK_ERRORS as e:
        print(f"Không gọi được GitHub: {e}", file=sys.stderr)
        return 2
    print(f"Xong: {len(saved)} log mới, {errors} lỗi.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
