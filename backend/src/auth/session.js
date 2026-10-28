// ============================================================================
// Phiên đăng nhập (session) lưu trong MySQL, cookie chỉ chứa token ngẫu nhiên.
//
// - Token: 32 byte ngẫu nhiên. DB chỉ lưu SHA-256 của token, nên ai đọc được
//   bảng sessions (backup, SQL injection...) cũng không dùng lại được phiên.
// - Cookie `sid`: HttpOnly (JavaScript trên trang không đọc được -> XSS không
//   lấy cắp được), SameSite=Strict (trang khác không gửi kèm được), Secure khi
//   chạy HTTPS (nginx báo qua X-Forwarded-Proto, Express tin nhờ trust proxy).
// - Hết hạn sau SESSION_TTL_HOURS giờ (mặc định 8), tính trong MySQL bằng giờ UTC.
// ============================================================================

const crypto = require('crypto');
const { pool } = require('../db');
const { log } = require('../logger');
const { parseCookies } = require('./http');

const COOKIE_NAME = 'sid';
const TTL_SECONDS = Math.max(1, Number(process.env.SESSION_TTL_HOURS) || 8) * 3600;

const hashToken = (token) => crypto.createHash('sha256').update(token).digest('hex');

async function createSession(userId) {
  const token = crypto.randomBytes(32).toString('base64url');
  // Dọn phiên đã hết hạn mỗi lần có người đăng nhập (bảng luôn nhỏ)
  await pool.query('DELETE FROM sessions WHERE expires_at < UTC_TIMESTAMP()');
  await pool.query(
    'INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND))',
    [hashToken(token), userId, TTL_SECONDS]
  );
  return token;
}

// Trả { id, username, role } nếu cookie hợp lệ và chưa hết hạn, ngược lại null
async function findSessionUser(token) {
  if (typeof token !== 'string' || token.length < 20 || token.length > 100) return null;
  try {
    const [rows] = await pool.query(
      `SELECT u.id, u.username, u.role
         FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ? AND s.expires_at > UTC_TIMESTAMP()`,
      [hashToken(token)]
    );
    return rows[0] || null;
  } catch (err) {
    // MySQL unreachable (or any query error): treat as 'no valid session' and
    // return null instead of throwing, so /api/auth/check responds 401 (not logged in)
    // instead of 500 when the DB is down.
    log.warn('session_lookup_failed', { err_message: err.message, err_code: err.code || '' });
    return null;
  }
}

async function deleteSession(token) {
  if (typeof token !== 'string' || !token) return;
  await pool.query('DELETE FROM sessions WHERE token_hash = ?', [hashToken(token)]);
}

async function deleteUserSessions(userId) {
  await pool.query('DELETE FROM sessions WHERE user_id = ?', [userId]);
}

function readToken(req) {
  return parseCookies(req.headers.cookie)[COOKIE_NAME] || null;
}

function cookieOptions(req) {
  return { httpOnly: true, sameSite: 'strict', secure: req.secure, path: '/' };
}

function setSessionCookie(req, res, token) {
  res.cookie(COOKIE_NAME, token, { ...cookieOptions(req), maxAge: TTL_SECONDS * 1000 });
}

function clearSessionCookie(req, res) {
  res.clearCookie(COOKIE_NAME, cookieOptions(req));
}

module.exports = {
  createSession,
  findSessionUser,
  deleteSession,
  deleteUserSessions,
  readToken,
  setSessionCookie,
  clearSessionCookie,
};
