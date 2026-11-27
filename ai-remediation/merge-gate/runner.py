"""Chạy kịch bản kiểm thử HTTP (dạng DỮ LIỆU, không phải code) vào app đang chạy trên localhost.

Dùng ở 2 nơi:
  - workflow .github/workflows/ai-test.yml (bản trên main) chạy file này trên máy ảo GitHub;
  - gate.py gọi validate() để kiểm kịch bản do AI viết trước khi gửi đi.

Định dạng:
{"scenarios": [{"name": "...", "steps": [
    {"name": "...", "setup": false,
     "request": {"method": "GET", "path": "/api/products", "json": {...}, "headers": {...}},
     "expect": {"status": 200, "body_contains": "chữ", "json": [{"path": "0.id", "exists": true}]},
     "save": {"productId": "0.id"}}
]}]}

  - "${ten}" trong path / json / giá trị mong đợi được thay bằng biến đã "save" (hoặc admin_username,
    admin_password, unique). {"$var": "ten", "add": -2} = biến số cộng thêm.
  - Mỗi kịch bản có phiên (cookie) riêng.
  - Kết quả mỗi kịch bản: passed · failed (sai mong đợi, hoặc app trả 5xx) · error (không chạy được:
    bước "setup" sai, thiếu biến, mất kết nối).

Chạy: python runner.py --baseline baseline.json [--scenarios s.json] --base-url http://127.0.0.1:18080 --out result.json
Mã thoát: 0 = mọi kịch bản đạt · 1 = có kịch bản không đạt · 2 = không chạy được
"""
import argparse
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_SCENARIOS = 20
MAX_STEPS_PER_SCENARIO = 30
MAX_STEPS_TOTAL = 150
MAX_BODY_CHARS = 20000
MAX_RESPONSE_BYTES = 1_000_000
TIMEOUT = 15
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
HEADERS = {"accept", "content-type", "origin", "sec-fetch-site"}
OPERATORS = {"equals", "not_equals", "exists", "contains", "gt", "gte", "lt", "lte", "length", "length_gte"}
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
# ${ten} hoặc ${ten.0.id} (lấy phần tử bên trong biến đã lưu)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,40}(?:\.[A-Za-z0-9_-]{1,40}){0,6})\}")
MISSING = object()


class ScenarioError(ValueError):
    """Kịch bản sai định dạng."""


class StepError(Exception):
    """Bước không chạy được (thiếu biến, đường dẫn sai sau khi thay biến, mất kết nối)."""


class Mismatch(Exception):
    """App trả kết quả khác mong đợi."""


# ---------------------------------------------------------------- kiểm định dạng
def _need(cond, msg):
    if not cond:
        raise ScenarioError(msg)


def check_path(path):
    """Chỉ đường dẫn trong app: bắt đầu bằng 1 dấu "/", không khoảng trắng, không thể trỏ sang máy khác."""
    return (isinstance(path, str) and 0 < len(path) <= 500 and path.startswith("/") and not path.startswith("//")
            and "\\" not in path and not any(c.isspace() or ord(c) < 32 for c in path))


def validate(data):
    """Trả danh sách kịch bản nếu hợp lệ, ngược lại ném ScenarioError."""
    _need(isinstance(data, dict) and isinstance(data.get("scenarios"), list), 'thiếu danh sách "scenarios"')
    scenarios = data["scenarios"]
    _need(len(scenarios) <= MAX_SCENARIOS, f"quá {MAX_SCENARIOS} kịch bản")
    total = 0
    for sc in scenarios:
        _need(isinstance(sc, dict) and isinstance(sc.get("name"), str) and sc["name"].strip(), "kịch bản thiếu tên")
        steps = sc.get("steps")
        _need(isinstance(steps, list) and 0 < len(steps) <= MAX_STEPS_PER_SCENARIO,
              f"kịch bản '{sc['name'][:60]}' phải có 1..{MAX_STEPS_PER_SCENARIO} bước")
        total += len(steps)
        for step in steps:
            _validate_step(step, sc["name"][:60])
        # bước cuối là điều kịch bản muốn kiểm: không được đánh dấu "setup" để né kết quả fail
        _need(not steps[-1].get("setup"), f"kịch bản '{sc['name'][:60]}': bước cuối không được là setup")
    _need(total <= MAX_STEPS_TOTAL, f"quá {MAX_STEPS_TOTAL} bước")
    return scenarios


def _validate_step(step, where):
    _need(isinstance(step, dict), f"{where}: bước phải là object")
    _need(set(step) <= {"name", "setup", "request", "expect", "save"}, f"{where}: bước có trường lạ")
    req = step.get("request")
    _need(isinstance(req, dict) and set(req) <= {"method", "path", "json", "headers"}, f"{where}: request sai")
    _need(req.get("method") in METHODS, f"{where}: method không được phép")
    _need(check_path(req.get("path")), f"{where}: path không hợp lệ")
    headers = req.get("headers", {})
    _need(isinstance(headers, dict) and all(
        k.lower() in HEADERS and isinstance(v, str) and "\n" not in v and "\r" not in v for k, v in headers.items()),
        f"{where}: header không được phép")
    _need(len(json.dumps(req.get("json"))) <= MAX_BODY_CHARS, f"{where}: body quá dài")

    expect = step.get("expect")
    _need(isinstance(expect, dict) and set(expect) <= {"status", "json", "body_contains"}, f"{where}: expect sai")
    _need(type(expect.get("status")) is int and 100 <= expect["status"] <= 599, f"{where}: thiếu expect.status")
    _need(isinstance(expect.get("body_contains", ""), str), f"{where}: body_contains phải là chữ")
    checks = expect.get("json", [])
    _need(isinstance(checks, list), f"{where}: expect.json phải là danh sách")
    for c in checks:
        _need(isinstance(c, dict) and isinstance(c.get("path"), str), f"{where}: kiểm tra json thiếu path")
        ops = set(c) - {"path"}
        _need(ops and ops <= OPERATORS, f"{where}: toán tử kiểm tra không hợp lệ")
    save = step.get("save", {})
    _need(isinstance(save, dict) and all(NAME_RE.fullmatch(k) and isinstance(v, str) for k, v in save.items()),
          f"{where}: save sai")


# ---------------------------------------------------------------- chạy
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # để kịch bản kiểm được 301/302, và không bao giờ đi theo sang máy khác


def substitute(value, variables):
    if isinstance(value, str):
        if "${" in VAR_RE.sub("", value):  # không được lặng lẽ gửi nguyên chuỗi "${...}" đi
            raise StepError(f"biến viết sai: {value[:80]!r}")
        whole = VAR_RE.fullmatch(value)
        if whole:  # cả chuỗi là 1 biến -> giữ nguyên kiểu (số vẫn là số)
            return _lookup(variables, whole[1])
        return VAR_RE.sub(lambda m: str(_lookup(variables, m[1])), value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        if "$var" in value:
            base = _lookup(variables, str(value["$var"]))
            add = value.get("add", 0)
            try:
                return float(base) + add if isinstance(base, str) else base + add
            except (TypeError, ValueError):
                raise StepError(f"biến {value['$var']} không phải số") from None
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


def _lookup(variables, name):
    root, _, rest = name.partition(".")
    value = dig(variables[root], rest) if root in variables else MISSING
    if value is MISSING:
        raise StepError(f"chưa có biến ${{{name}}}")
    return value


def dig(data, path):
    """Lấy giá trị theo đường dẫn kiểu "items.0.name" ("" = cả tài liệu)."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and re.fullmatch(r"-?\d+", part) and -len(data) <= int(part) < len(data):
            data = data[int(part)]
        else:
            return MISSING
    return data


def _number(v):
    # chấp nhận số trả về dạng chuỗi ("1290000.00")
    if isinstance(v, bool):
        raise ValueError
    return float(v)


def check_value(actual, op, wanted):
    if op == "exists":
        return (actual is not MISSING) == bool(wanted)
    if actual is MISSING:
        return False
    try:
        if op in ("equals", "not_equals"):
            same = actual == wanted
            if not same and isinstance(wanted, (int, float)) and isinstance(actual, str):
                try:
                    same = _number(actual) == wanted
                except ValueError:
                    same = False
            return same if op == "equals" else not same
        if op == "contains":
            return wanted in actual
        if op == "length":
            return len(actual) == wanted
        if op == "length_gte":
            return len(actual) >= wanted
        a, w = _number(actual), _number(wanted)
        return {"gt": a > w, "gte": a >= w, "lt": a < w, "lte": a <= w}[op]
    except (TypeError, ValueError):
        return False


def send(opener, base_url, request, variables):
    path = substitute(request["path"], variables)
    if not check_path(path):
        raise StepError(f"path không hợp lệ sau khi thay biến: {path[:80]!r}")
    headers = dict(request.get("headers", {}))
    body = None
    if "json" in request:
        body = json.dumps(substitute(request["json"], variables)).encode()
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
    url = base_url + urllib.parse.quote(path, safe="/?&=%:#+,;@!$'()*-._~")
    req = urllib.request.Request(url, data=body, headers=headers, method=request["method"])
    try:
        try:
            res = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:  # 3xx/4xx/5xx vẫn là kết quả hợp lệ để kiểm
            res = e
        with res:
            return res.code, res.read(MAX_RESPONSE_BYTES).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise StepError(f"không gọi được app: {type(e).__name__}") from None


def check_step(step, status, text, variables):
    expect = step["expect"]
    if status != expect["status"]:
        raise Mismatch(f"status {status}, mong đợi {expect['status']}")
    needle = substitute(expect.get("body_contains", ""), variables)
    if needle and str(needle) not in text:
        raise Mismatch(f"nội dung không chứa {str(needle)[:80]!r}")
    checks, save = expect.get("json", []), step.get("save", {})
    if not checks and not save:
        return
    try:
        data = json.loads(text)
    except ValueError:
        raise Mismatch("phản hồi không phải JSON") from None
    for c in checks:
        actual = dig(data, str(substitute(c["path"], variables)))
        for op in sorted(set(c) - {"path"}):
            wanted = substitute(c[op], variables)
            if not check_value(actual, op, wanted):
                got = "không có" if actual is MISSING else json.dumps(actual, ensure_ascii=False)[:120]
                raise Mismatch(f"{c['path'] or '(gốc)'}: cần {op} {json.dumps(wanted, ensure_ascii=False)[:80]}, thực tế {got}")
    for name, path in save.items():
        value = dig(data, path)
        if value is MISSING:
            raise Mismatch(f"không lấy được {path!r} để lưu biến {name}")
        variables[name] = value


def run_scenario(scenario, base_url, variables, source):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect())
    variables = {**variables, "unique": secrets.token_hex(4)}
    if scenario.get("source") == "saved":  # kịch bản đã đạt và được duyệt ở lần trước
        source = "saved"
    result = {"name": scenario["name"][:200], "source": source, "status": "passed"}
    for number, step in enumerate(scenario["steps"], 1):
        status, text = None, ""
        try:
            status, text = send(opener, base_url, step["request"], variables)
            check_step(step, status, text, variables)
        except (StepError, Mismatch) as e:
            # app trả 5xx là lỗi của app, kể cả ở bước chuẩn bị
            setup_only = step.get("setup") and not (status and status >= 500)
            result["status"] = "error" if isinstance(e, StepError) or setup_only else "failed"
            request = step["request"]
            # bằng chứng: đã GỬI gì (sau khi thay biến), MONG gì, NHẬN gì
            path = _best_effort(request["path"], variables)
            result.update(step=number, step_name=str(step.get("name", ""))[:200],
                          request=f"{request['method']} {path}"[:200],
                          body=json.dumps(_best_effort(request.get("json"), variables), ensure_ascii=False)[:500],
                          expected=json.dumps(_best_effort(step["expect"], variables), ensure_ascii=False)[:500],
                          actual_status=status, detail=str(e)[:400], response=text[:500])
            break
    return result


def _best_effort(value, variables):
    try:
        return substitute(value, variables)
    except StepError:
        return value


def check_base_url(url):
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or parts.path not in ("", "/"):
        raise ScenarioError("chỉ được chạy vào http://127.0.0.1:<cổng> hoặc http://localhost:<cổng>")
    return url.rstrip("/")


def wait_for_app(base_url, seconds):
    deadline = time.time() + seconds
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        try:
            with opener.open(base_url + "/health", timeout=5) as res:
                if res.status == 200:
                    return True
        except (OSError, http.client.HTTPException):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(2)


def run(groups, base_url, variables):
    """groups: danh sách (nguồn, danh sách kịch bản đã validate)."""
    results = [run_scenario(sc, base_url, variables, source) for source, scenarios in groups for sc in scenarios]
    return {"passed": bool(results) and all(r["status"] == "passed" for r in results), "scenarios": results}


def load_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    return validate(json.loads(text)) if text else []


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--scenarios")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--out", default="result.json")
    parser.add_argument("--wait", type=int, default=90)
    args = parser.parse_args(argv)
    try:
        base_url = check_base_url(args.base_url)
        groups = [("baseline", load_file(args.baseline))]
        if args.scenarios:
            groups.append(("ai", load_file(args.scenarios)))
    except (OSError, ValueError) as e:
        print(f"Kịch bản/cấu hình sai: {e}", file=sys.stderr)
        return 2
    if not wait_for_app(base_url, args.wait):
        print("App không trả lời /health", file=sys.stderr)
        return 2
    report = run(groups, base_url, {
        "admin_username": os.environ.get("TEST_ADMIN_USERNAME", "admin"),
        "admin_password": os.environ.get("TEST_ADMIN_PASSWORD", "TestOnlyPassword123"),
    })
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    for r in report["scenarios"]:
        extra = "" if r["status"] == "passed" else f" — bước {r['step']} {r['request']}: {r['detail']}"
        print(f"[{r['status']:6}] ({r['source']}) {r['name']}{extra}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
