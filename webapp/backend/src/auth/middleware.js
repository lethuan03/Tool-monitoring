// Middleware phân quyền: gắn trước các route chỉ admin được dùng.
const { findSessionUser, readToken } = require('./session');
const { isSameSiteRequest } = require('./http');

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

// Request thay đổi dữ liệu mà đến từ trang khác -> 403 (chống CSRF)
function rejectCrossSite(req, res) {
  if (SAFE_METHODS.has(req.method)) return false;
  const ok = isSameSiteRequest({
    origin: req.get('origin'),
    secFetchSite: req.get('sec-fetch-site'),
    hostname: req.hostname,
  });
  if (ok) return false;
  res.status(403).json({ error: 'Yêu cầu bị từ chối (khác nguồn)' });
  return true;
}

// Đọc phiên đăng nhập (nếu có) vào req.user. Không chặn gì.
async function loadUser(req) {
  if (req.user !== undefined) return req.user;
  req.user = await findSessionUser(readToken(req));
  return req.user;
}

// Chỉ admin đã đăng nhập mới qua được. Chưa đăng nhập -> 401, không phải admin -> 403.
function requireAdmin(req, res, next) {
  if (rejectCrossSite(req, res)) return;
  loadUser(req)
    .then((user) => {
      if (!user) return res.status(401).json({ error: 'Cần đăng nhập' });
      if (user.role !== 'admin') return res.status(403).json({ error: 'Không có quyền' });
      next();
    })
    .catch(next);
}

module.exports = { requireAdmin, loadUser, rejectCrossSite };
