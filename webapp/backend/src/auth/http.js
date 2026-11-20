// Hàm nhỏ về HTTP cho phần đăng nhập. Hàm thuần, test được không cần server.

// Đọc header Cookie thành object. Tự viết để khỏi thêm thư viện cookie-parser.
function parseCookies(header) {
  const out = {};
  if (typeof header !== 'string' || !header) return out;
  for (const raw of header.split(';')) {
    const part = raw.trim();
    const i = part.indexOf('=');
    if (i <= 0) continue; // không có tên cookie -> bỏ
    const name = part.slice(0, i).trim();
    let value = part.slice(i + 1).trim();
    if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
    try {
      out[name] = decodeURIComponent(value);
    } catch {
      out[name] = value;
    }
  }
  return out;
}

// Chặn giả mạo request từ trang web khác (CSRF) cho các request thay đổi dữ liệu.
// Cookie đã đặt SameSite=Strict; đây là lớp thứ hai:
//   - Trình duyệt hiện đại gửi Sec-Fetch-Site: "cross-site" nếu request đến từ trang khác.
//   - Origin (trình duyệt gửi kèm POST/PUT/DELETE) phải cùng tên miền với trang.
// So tên miền, không so cổng: nginx chuyển Host không kèm cổng (proxy_set_header Host $host).
// Không có Origin (curl, script) thì cho qua: những client đó không mang được cookie
// của trình duyệt nạn nhân, nên không phải CSRF.
function isSameSiteRequest({ origin, secFetchSite, hostname }) {
  if (secFetchSite === 'cross-site') return false;
  if (!origin) return true;
  try {
    return new URL(origin).hostname === String(hostname || '').toLowerCase();
  } catch {
    return false;
  }
}

module.exports = { parseCookies, isSameSiteRequest };
