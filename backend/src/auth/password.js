// ============================================================================
// Băm và kiểm tra mật khẩu bằng scrypt (có sẵn trong Node, không cần thư viện).
//
// Không bao giờ lưu mật khẩu gốc. DB chỉ giữ chuỗi dạng:
//   scrypt$N$r$p$<salt base64>$<hash base64>
// Mỗi lần băm dùng salt ngẫu nhiên mới, nên 2 người cùng mật khẩu vẫn ra 2
// chuỗi khác nhau. Lưu kèm N/r/p để sau này tăng độ khó vẫn kiểm tra được
// mật khẩu cũ.
// ============================================================================

const crypto = require('crypto');
const { promisify } = require('util');

const scrypt = promisify(crypto.scrypt);

const KEY_LEN = 64;
const SALT_LEN = 16;
// N = 2^15: mỗi lần kiểm tra mất vài chục ms. Đủ chậm để dò mật khẩu tốn kém,
// đủ nhanh để đăng nhập không thấy trễ. maxmem phải >= 128 * N * r.
const DEFAULT_PARAMS = { N: 32768, r: 8, p: 1 };
const MAX_MEM = 64 * 1024 * 1024;

const PASSWORD_MIN = 12;
const PASSWORD_MAX = 200; // chặn chuỗi cực dài làm scrypt chạy lâu

async function hashPassword(password, params = DEFAULT_PARAMS) {
  const salt = crypto.randomBytes(SALT_LEN);
  const key = await scrypt(String(password), salt, KEY_LEN, { ...params, maxmem: MAX_MEM });
  return ['scrypt', params.N, params.r, params.p, salt.toString('base64'), key.toString('base64')].join('$');
}

// Trả true/false, không bao giờ ném lỗi vì dữ liệu sai định dạng
async function verifyPassword(password, stored) {
  if (typeof password !== 'string' || typeof stored !== 'string') return false;
  const parts = stored.split('$');
  if (parts.length !== 6 || parts[0] !== 'scrypt') return false;

  const [N, r, p] = parts.slice(1, 4).map(Number);
  if (![N, r, p].every((n) => Number.isInteger(n) && n > 0)) return false;
  const salt = Buffer.from(parts[4], 'base64');
  const expected = Buffer.from(parts[5], 'base64');
  if (salt.length === 0 || expected.length === 0) return false;

  try {
    const key = await scrypt(password, salt, expected.length, { N, r, p, maxmem: MAX_MEM });
    // So sánh thời gian không đổi: không lộ "đúng được bao nhiêu byte" qua thời gian phản hồi
    return crypto.timingSafeEqual(key, expected);
  } catch {
    return false;
  }
}

// Kiểm tra mật khẩu MỚI (lúc tạo admin / đổi mật khẩu). Trả chuỗi lỗi hoặc ''.
function checkNewPassword(password) {
  if (typeof password !== 'string') return 'Mật khẩu phải là chữ';
  if (password.length < PASSWORD_MIN) return `Mật khẩu tối thiểu ${PASSWORD_MIN} ký tự`;
  if (password.length > PASSWORD_MAX) return `Mật khẩu tối đa ${PASSWORD_MAX} ký tự`;
  return '';
}

module.exports = { hashPassword, verifyPassword, checkNewPassword, PASSWORD_MIN, PASSWORD_MAX };
