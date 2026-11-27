"""Test khoanh vùng thay đổi."""
import fnmatch
import glob
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import scope  # noqa: E402

S = scope.SRC
PRODUCTS = """const express = require('express');
const { pool } = require('../db');
const { check } = require('../validate');
const router = express.Router();

// danh sách
router.get(
  '/',
  (req, res) => res.json([])
);

// số lượng
router.get(
  '/count',
  (req, res) => res.json({ count: 1 })
);

router.post('/', check);

module.exports = router;
"""
SOURCES = {
    S + "server.js": """const productsRouter = require('./routes/products');
const ordersRouter = require('./routes/orders');
const { log } = require('./logger');
app.use('/api/products', stampMount('/api/products'), productsRouter);
app.use('/api/orders', ordersRouter);
""",
    S + "routes/products.js": PRODUCTS,
    S + "routes/orders.js": "const { pool } = require('../db');\nrouter.post('/', h);\nmodule.exports = router;\n",
    S + "db.js": "const mysql = require('mysql2');\n",
    S + "validate.js": "exports.check = 1;\n",
    S + "logger.js": "exports.log = 1;\n",
}
DOCS = {
    "webapp/features/system.md": "# Chung\n\nFiles: *\n",
    "webapp/features/products.md": f"# Sản phẩm\n\nFiles: {S}routes/products.js, {S}validate.js\n",
    "webapp/features/orders.md": f"# Đơn hàng\n\nFiles: {S}routes/orders.js\n",
}


def changed(path, patch="@@ -1 +1 @@\n+x"):
    return {"filename": path, "status": "modified", "patch": patch}


def line_of(text, needle):
    return text[:text.index(needle)].count("\n") + 1


class ScopeTest(unittest.TestCase):
    def analyze(self, *files):
        return scope.analyze(list(files), SOURCES, DOCS)

    def test_one_route_changed(self):
        number = line_of(PRODUCTS, "(req, res) => res.json({ count")
        out = self.analyze(changed(S + "routes/products.js", f"@@ -{number} +{number} @@\n+  new"))
        self.assertEqual(out["endpoints"], ["GET /api/products/count"])
        self.assertFalse(out["wide"])
        self.assertEqual(out["features"], ["Sản phẩm", "Chung"] if out["features"][0] == "Sản phẩm" else ["Chung", "Sản phẩm"])
        self.assertIn(S + "db.js", out["files"])
        self.assertNotIn(S + "routes/orders.js", out["files"])
        self.assertNotIn(S + "logger.js", out["files"])
        self.assertEqual(out["files"][0], S + "routes/products.js")

    def test_change_outside_routes_affects_whole_file(self):
        out = self.analyze(changed(S + "routes/products.js", "@@ -2 +2 @@\n+const z = 1;"))
        self.assertEqual(out["endpoints"], ["GET /api/products", "GET /api/products/count", "POST /api/products"])

    def test_shared_file_spreads_to_users(self):
        out = self.analyze(changed(S + "validate.js"))
        self.assertTrue(all("/api/products" in e for e in out["endpoints"]) and out["endpoints"])
        self.assertNotIn("Đơn hàng", out["features"])
        out = self.analyze(changed(S + "db.js"))
        self.assertIn("POST /api/orders", out["endpoints"])
        self.assertIn("GET /api/products", out["endpoints"])
        self.assertEqual(set(out["features"]), {"Chung", "Sản phẩm", "Đơn hàng"})

    def test_unsure_means_wide(self):
        for path in (S + "server.js", S + "logger.js", S + "moi.js", "webapp/mysql/init/x.sh",
                     "webapp/nginx/default.conf", "webapp/backend/package.json"):
            with self.subTest(path=path):
                out = self.analyze(changed(path))
                self.assertTrue(out["wide"])
                self.assertEqual(len(out["files"]), len(SOURCES) + 1)
                self.assertEqual(len(out["features"]), 3)

    def test_docs_tests_and_frontend_need_no_backend_code(self):
        for path in ("README.md", "webapp/backend/tests/a.test.js", "webapp/frontend/app.js"):
            with self.subTest(path=path):
                out = self.analyze(changed(path))
                self.assertEqual((out["files"], out["wide"], out["endpoints"]), ([], False, []))

    def test_changed_lines(self):
        self.assertEqual(scope.changed_lines("@@ -3,3 +3,3 @@\n a\n-b\n+c\n d"), {4})

    def test_real_repo_feature_files_exist_and_routes_found(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

        def read(p):
            with open(p, encoding="utf-8") as f:
                return f.read()

        paths = [os.path.relpath(p, repo).replace(os.sep, "/")
                 for p in glob.glob(os.path.join(repo, "webapp", "backend", "src", "**", "*.js"), recursive=True)]
        sources = {p: read(os.path.join(repo, p)) for p in paths}
        docs = {p: read(p) for p in glob.glob(os.path.join(repo, "webapp", "features", "*.md"))}
        self.assertGreaterEqual(len(scope.mounts(sources)), 3)
        for feature in scope.parse_features(docs):
            for pattern in feature["patterns"]:
                if pattern != "*":
                    self.assertTrue(fnmatch.filter(paths, pattern), f"{feature['name']}: {pattern} không khớp file nào")


if __name__ == "__main__":
    unittest.main()
