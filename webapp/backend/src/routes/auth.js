// ============================================================================
// /api/auth — đăng nhập, đăng xuất, hỏi "tôi là ai", đổi mật khẩu.
//
// Log: chỉ ghi sự kiện (login_success / login_failed / password_changed) kèm
// user_id hoặc lý do. KHÔNG ghi mật khẩu, KHÔNG ghi username người ta gõ thử
// (có khi là mật khẩu gõ nhầm ô). IP đã có sẵn trong dòng log http_request.
// ============================================================================

const express = require('express');
const { pool } = require('../db');
const { log } = require('../logger');
const { hashPassword, verifyPassword, checkNewPassword, PASSWORD_MAX } = require('../auth/password');
const { createLimiter } = require('../auth/rate-limit');
const {
  createSession, deleteSession, deleteUserSessions, readToken, setSessionCookie, clearSessionCookie,
} = require('../auth/session');
const { requireAdmin, loadUser, rejectCrossSite } = require('../auth/middleware');

const router = express.Router();
const asyncHandler = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// Sai 5 lần trong 15 phút -> khoá 15 phút (theo IP và theo tên đăng nhập)
const limiter = createLimiter({ max: 5, windowMs: 15 * 60 * 1000, lockMs: 15 * 60 * 1000 });

// Hash giả để khi username không tồn tại vẫn tốn đúng thời gian như khi sai mật khẩu
// -> không đoán được "tài khoản này có tồn tại không" qua thời gian phản hồi
const dummyHash = hashPassword('khong-phai-mat-khau-that');

function tooMany(res, ms) {
  res.set('Retry-After', String(Math.ceil(ms / 1000)));
  return res.status(429).json({ error: `Sai quá nhiều lần. Thử lại sau ${Math.ceil(ms / 60000)} phút.` });
}

// POST /api/auth/login  body: { username, password }
router.post(
  '/login',
  asyncHandler(async (req, res) => {
    if (rejectCrossSite(req, res)) return;

    const { username, password } = req.body || {};
    const name = typeof username === 'string' ? username.trim() : '';
    if (!name || name.length > 50 || typeof password !== 'string' || !password || password.length > PASSWORD_MAX) {
      return res.status(400).json({ error: 'Nhập tên đăng nhập và mật khẩu' });
    }

    const ipKey = `ip:${req.ip}`;
    const userKey = `user:${name.toLowerCase()}`;
    const wait = Math.max(limiter.retryAfterMs(ipKey), limiter.retryAfterMs(userKey));
    if (wait > 0) {
      log.warn('login_locked', { reason: 'rate_limited' });
      return tooMany(res, wait);
    }

    const [[user]] = await pool.query('SELECT id, username, role, password_hash FROM users WHERE username = ?', [name]);
    const ok = user
      ? await verifyPassword(password, user.password_hash)
      : (await verifyPassword(password, await dummyHash), false);

    if (!ok) {
      limiter.fail(ipKey);
      limiter.fail(userKey);
      log.warn('login_failed', { reason: user ? 'wrong_password' : 'unknown_user' });
      // Cùng một thông báo cho cả 2 trường hợp
      return res.status(401).json({ error: 'Sai tên đăng nhập hoặc mật khẩu' });
    }

    limiter.reset(ipKey);
    limiter.reset(userKey);
    setSessionCookie(req, res, await createSession(user.id));
    log.info('login_success', { user_id: user.id, role: user.role });
    res.json({ username: user.username, role: user.role });
  })
);

// POST /api/auth/logout
router.post(
  '/logout',
  asyncHandler(async (req, res) => {
    if (rejectCrossSite(req, res)) return;
    await deleteSession(readToken(req));
    clearSessionCookie(req, res);
    res.status(204).end();
  })
);

// GET /api/auth/me — trang shop và trang admin hỏi "đang đăng nhập chưa".
// Luôn trả 200 (kể cả khách chưa đăng nhập) để không làm tăng số 4xx trên dashboard.
router.get(
  '/me',
  asyncHandler(async (req, res) => {
    res.set('Cache-Control', 'no-store');
    const user = await loadUser(req);
    if (!user) return res.json({ authenticated: false });
    res.json({ authenticated: true, username: user.username, role: user.role });
  })
);

// GET /api/auth/check — nginx gọi (auth_request) trước khi trả file admin.html.
// 204 = admin đã đăng nhập, 401 = chưa.
router.get(
  '/check',
  asyncHandler(async (req, res) => {
    const user = await loadUser(req);
    res.status(user && user.role === 'admin' ? 204 : 401).end();
  })
);

// POST /api/auth/password  body: { currentPassword, newPassword }
// Đổi xong: đăng xuất MỌI phiên cũ (máy khác đang đăng nhập cũng bị đá ra),
// rồi cấp phiên mới cho chính trình duyệt đang đổi.
router.post(
  '/password',
  requireAdmin,
  asyncHandler(async (req, res) => {
    const { currentPassword, newPassword } = req.body || {};
    const userKey = `user:${req.user.username.toLowerCase()}`;
    const wait = limiter.retryAfterMs(userKey);
    if (wait > 0) return tooMany(res, wait);

    const problem = checkNewPassword(newPassword);
    if (problem) return res.status(400).json({ error: problem });
    if (typeof currentPassword !== 'string' || currentPassword.length > PASSWORD_MAX) {
      return res.status(400).json({ error: 'Nhập mật khẩu hiện tại' });
    }
    if (newPassword === currentPassword) return res.status(400).json({ error: 'Mật khẩu mới phải khác mật khẩu cũ' });

    const [[row]] = await pool.query('SELECT password_hash FROM users WHERE id = ?', [req.user.id]);
    if (!row || !(await verifyPassword(currentPassword, row.password_hash))) {
      limiter.fail(userKey);
      log.warn('password_change_failed', { user_id: req.user.id, reason: 'wrong_current_password' });
      return res.status(400).json({ error: 'Mật khẩu hiện tại không đúng' });
    }

    await pool.query('UPDATE users SET password_hash = ? WHERE id = ?', [await hashPassword(newPassword), req.user.id]);
    await deleteUserSessions(req.user.id);
    setSessionCookie(req, res, await createSession(req.user.id));
    log.info('password_changed', { user_id: req.user.id });
    res.status(204).end();
  })
);

module.exports = router;
