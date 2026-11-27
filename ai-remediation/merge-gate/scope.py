"""Khoanh vùng thay đổi để AI chỉ phải đọc phần liên quan (phân tích bằng code, không tốn phí AI).

diff -> dòng nào đổi -> API nào chứa dòng đó -> file đó require gì, ai require nó -> mô tả chức năng liên quan.
Không chắc ảnh hưởng tới đâu thì trả wide=True (đọc toàn bộ mã backend), không bao giờ thu hẹp theo phỏng đoán.
"""
import fnmatch
import posixpath
import re

SRC = "webapp/backend/src/"
SERVER = SRC + "server.js"
NGINX_LOCATIONS = "webapp/nginx/snippets/app-locations.conf"
FEATURE_DIR = "webapp/features/"

REQUIRE_RE = re.compile(r"""require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
CONST_REQUIRE_RE = re.compile(r"""const\s+(\w+)\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)""")
MOUNT_RE = re.compile(r"""app\.use\(\s*['"](/[^'"]*)['"]\s*,[^;]*?\b(\w+)\s*\)\s*;""")
ROUTE_RE = re.compile(r"""\brouter\.(get|post|put|patch|delete)\(\s*['"]([^'"]*)['"]""")


def resolve(from_path, target, paths):
    base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), target))
    for candidate in (base, base + ".js", base + "/index.js"):
        if candidate in paths:
            return candidate
    return None


def dependencies(sources):
    """{file: các file nó require trong dự án}."""
    return {path: {r for r in (resolve(path, t, sources) for t in REQUIRE_RE.findall(text)) if r}
            for path, text in sources.items()}


def mounts(sources):
    """{file route: tiền tố URL}, đọc từ server.js."""
    server = sources.get(SERVER, "")
    names = {name: resolve(SERVER, target, sources) for name, target in CONST_REQUIRE_RE.findall(server)}
    return {names[var]: prefix for prefix, var in MOUNT_RE.findall(server) if names.get(var)}


def changed_lines(patch):
    """Số dòng (bản mới) bị thêm hoặc nằm tại chỗ bị xoá, bỏ qua dòng trống."""
    lines, number = set(), 0
    for line in (patch or "").splitlines():
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
        if hunk:
            number = int(hunk[1])
        elif line.startswith("+"):
            if line[1:].strip():
                lines.add(number)
            number += 1
        elif line.startswith("-"):
            if line[1:].strip():
                lines.add(number)
        elif not line.startswith("\\"):
            number += 1
    return lines


def route_blocks(text):
    """[(dòng đầu, dòng cuối, method, đường dẫn)] — tính cả chú thích ngay phía trên route."""
    rows = text.splitlines()
    found = []
    for m in ROUTE_RE.finditer(text):
        start = text.count("\n", 0, m.start()) + 1
        while start > 1 and (not rows[start - 2].strip() or rows[start - 2].lstrip().startswith("//")):
            start -= 1
        found.append((start, m[1].upper(), m[2]))
    end_of_routes = next((i for i, row in enumerate(rows, 1) if row.startswith("module.exports")), len(rows) + 1)
    return [(start, (found[i + 1][0] if i + 1 < len(found) else end_of_routes) - 1, method, path)
            for i, (start, method, path) in enumerate(found)]


def endpoint(method, prefix, path):
    return f"{method} {prefix}{'' if path == '/' else path}"


def parse_features(docs):
    """docs = {đường dẫn: nội dung .md}. Dòng "Files: mẫu1, mẫu2" nói chức năng nằm ở đâu ("*" = luôn đưa vào)."""
    features = []
    for path, text in sorted(docs.items()):
        title = re.search(r"^#\s+(.+)$", text, re.M)
        files = re.search(r"^Files:\s*(.+)$", text, re.M)
        patterns = [p.strip() for p in files[1].split(",") if p.strip()] if files else []
        features.append({"name": title[1].strip() if title else path, "path": path, "patterns": patterns, "text": text})
    return features


def analyze(files, sources, docs):
    """files = danh sách file đổi (GitHub compare); sources = {đường dẫn: nội dung} mã backend tại commit cần kiểm.
    Trả {"files": [...cần AI đọc], "endpoints": [...], "features": [...tên], "wide": bool, "why": [...]}."""
    deps, route_prefix = dependencies(sources), mounts(sources)
    changed = [f for f in files if not f["filename"].endswith(".md")]
    context, affected, endpoints, why, touched = set(), set(), set(), [], set()

    def all_endpoints(path):
        return {endpoint(method, route_prefix[path], p) for _, _, method, p in route_blocks(sources[path])}

    for f in changed:
        path = f["filename"]
        if path.startswith("webapp/backend/tests/"):
            continue
        if path.startswith(SRC) and path in sources:
            touched.add(path)
            context.add(path)
            context |= deps[path]
            if path == SERVER:
                why.append("đổi server.js")
            elif path in route_prefix:
                affected.add(path)
                blocks = route_blocks(sources[path])
                lines = changed_lines(f.get("patch"))
                hit = [b for b in blocks if any(b[0] <= n <= b[1] for n in lines)]
                outside = not lines or any(not any(b[0] <= n <= b[1] for b in blocks) for n in lines)
                for _, _, method, p in (blocks if outside else hit):
                    endpoints.add(endpoint(method, route_prefix[path], p))
            else:  # file dùng chung: lan ngược tới mọi route đang dùng nó
                seen, queue = {path}, [path]
                while queue:
                    current = queue.pop()
                    for user, used in deps.items():
                        if current in used and user not in seen and user != SERVER:
                            seen.add(user)
                            queue.append(user)
                reached = seen & set(route_prefix)
                if not reached:
                    why.append(f"không xác định được ảnh hưởng của {path}")
                for route in reached:
                    affected.add(route)
                    endpoints |= all_endpoints(route)
        elif path.startswith(("webapp/backend/", "webapp/mysql/", "webapp/nginx/")):
            touched.add(path)
            why.append(f"đổi {path}")

    features = parse_features(docs)
    wide = bool(why)
    if wide:
        context = set(sources) | {NGINX_LOCATIONS}
        selected = features
    else:
        context |= affected
        if affected:
            context.add(SERVER)
        # khớp theo file đổi và route bị ảnh hưởng (không theo dependency: db.js là dependency của mọi thứ)
        relevant = touched | affected | {f["filename"] for f in changed}
        selected = [ft for ft in features
                    if "*" in ft["patterns"] or any(fnmatch.fnmatchcase(p, pat) for p in relevant for pat in ft["patterns"])]
    return {"files": sorted(context, key=lambda p: (p not in touched, p not in affected, p)),
            "endpoints": sorted(endpoints), "features": [ft["name"] for ft in selected], "wide": wide, "why": why[:5]}
