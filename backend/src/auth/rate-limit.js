// ============================================================================
// Chống dò mật khẩu: sai quá `max` lần trong `windowMs` thì khoá `lockMs`.
//
// Lưu trong bộ nhớ của tiến trình backend: đủ cho 1 container. Khởi động lại
// backend là bộ đếm về 0. Chạy nhiều bản backend thì phải chuyển sang lưu
// chung (Redis/DB).
// Đếm theo 2 khoá: IP (1 máy thử nhiều tài khoản) và username (nhiều máy thử
// 1 tài khoản). Khoá nào chạm ngưỡng cũng chặn.
// ============================================================================

function createLimiter({ max = 5, windowMs = 15 * 60 * 1000, lockMs = 15 * 60 * 1000, maxKeys = 10000 } = {}) {
  const entries = new Map(); // key -> { count, firstAt, lockedUntil }

  function prune(now) {
    for (const [key, e] of entries) {
      if (e.lockedUntil <= now && now - e.firstAt > windowMs) entries.delete(key);
    }
  }

  // Còn bao nhiêu ms nữa mới được thử lại (0 = không bị khoá)
  function retryAfterMs(key, now = Date.now()) {
    const e = entries.get(key);
    return e && e.lockedUntil > now ? e.lockedUntil - now : 0;
  }

  function fail(key, now = Date.now()) {
    // Chặn kẻ tấn công làm phình bộ nhớ bằng hàng triệu khoá khác nhau
    if (entries.size >= maxKeys) prune(now);
    if (entries.size >= maxKeys) entries.clear();

    let e = entries.get(key);
    if (!e || (e.lockedUntil <= now && now - e.firstAt > windowMs)) {
      e = { count: 0, firstAt: now, lockedUntil: 0 };
      entries.set(key, e);
    }
    e.count += 1;
    if (e.count >= max) e.lockedUntil = now + lockMs;
  }

  function reset(key) {
    entries.delete(key);
  }

  return { retryAfterMs, fail, reset, size: () => entries.size };
}

module.exports = { createLimiter };
