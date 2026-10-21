// Chạy: npm test (trong thư mục backend)
const test = require('node:test');
const assert = require('node:assert/strict');
const { hashPassword, verifyPassword, checkNewPassword } = require('../src/auth/password');
const { createLimiter } = require('../src/auth/rate-limit');
const { parseCookies, isSameSiteRequest } = require('../src/auth/http');

// Tham số nhẹ để test chạy nhanh; định dạng giống hệt bản thật
const FAST = { N: 1024, r: 8, p: 1 };

test('Băm rồi kiểm tra: đúng mật khẩu thì true, sai thì false', async () => {
  const h = await hashPassword('mat-khau-dai-du-12', FAST);
  assert.match(h, /^scrypt\$1024\$8\$1\$/);
  assert.ok(!h.includes('mat-khau-dai-du-12'), 'không được chứa mật khẩu gốc');
  assert.equal(await verifyPassword('mat-khau-dai-du-12', h), true);
  assert.equal(await verifyPassword('mat-khau-dai-du-13', h), false);
});

test('Cùng mật khẩu băm 2 lần ra 2 chuỗi khác nhau (salt ngẫu nhiên)', async () => {
  const a = await hashPassword('giong-nhau-123', FAST);
  const b = await hashPassword('giong-nhau-123', FAST);
  assert.notEqual(a, b);
});

test('Hash sai định dạng thì false, không ném lỗi', async () => {
  for (const bad of ['', 'abc', 'scrypt$x$8$1$AA$BB', 'bcrypt$1$2$3$4$5', null, 42]) {
    assert.equal(await verifyPassword('abc', bad), false);
  }
  assert.equal(await verifyPassword(undefined, await hashPassword('x'.repeat(12), FAST)), false);
});

test('Mật khẩu mới: tối thiểu 12, tối đa 200 ký tự', () => {
  assert.ok(checkNewPassword('ngan'));
  assert.ok(checkNewPassword('x'.repeat(201)));
  assert.ok(checkNewPassword(123456789012));
  assert.equal(checkNewPassword('x'.repeat(12)), '');
});

test('Giới hạn đăng nhập: sai 5 lần thì khoá, hết thời gian thì mở', () => {
  const l = createLimiter({ max: 5, windowMs: 1000, lockMs: 5000 });
  for (let i = 0; i < 4; i++) l.fail('ip:1', 0);
  assert.equal(l.retryAfterMs('ip:1', 0), 0, '4 lần chưa khoá');
  l.fail('ip:1', 0);
  assert.equal(l.retryAfterMs('ip:1', 100), 4900, 'lần 5 bị khoá');
  assert.equal(l.retryAfterMs('ip:2', 100), 0, 'khoá khác không bị ảnh hưởng');
  assert.equal(l.retryAfterMs('ip:1', 5001), 0, 'hết khoá');
  l.reset('ip:1');
  assert.equal(l.size(), 0);
});

test('Giới hạn đăng nhập: sai rải rác ngoài cửa sổ thì đếm lại từ đầu', () => {
  const l = createLimiter({ max: 3, windowMs: 1000, lockMs: 5000 });
  l.fail('k', 0); l.fail('k', 500);
  l.fail('k', 2000); // cửa sổ cũ đã qua -> đếm lại = 1
  assert.equal(l.retryAfterMs('k', 2000), 0);
});

test('Giới hạn đăng nhập không phình bộ nhớ vô hạn', () => {
  const l = createLimiter({ max: 5, windowMs: 1000, lockMs: 1000, maxKeys: 100 });
  for (let i = 0; i < 1000; i++) l.fail(`ip:${i}`, 0);
  assert.ok(l.size() <= 100);
});

test('parseCookies đọc đúng cookie, bỏ qua phần rác', () => {
  assert.deepEqual(parseCookies('sid=abc; theme=dark'), { sid: 'abc', theme: 'dark' });
  assert.deepEqual(parseCookies('a=%20x%20; =bad; noeq; b="q"'), { a: ' x ', b: 'q' });
  assert.deepEqual(parseCookies(undefined), {});
});

test('Chống CSRF: chỉ cho request cùng tên miền', () => {
  assert.equal(isSameSiteRequest({ origin: 'http://localhost:8080', hostname: 'localhost' }), true);
  assert.equal(isSameSiteRequest({ origin: 'https://shop.example.com', hostname: 'shop.example.com' }), true);
  assert.equal(isSameSiteRequest({ origin: 'https://evil.example', hostname: 'shop.example.com' }), false);
  assert.equal(isSameSiteRequest({ origin: 'null', hostname: 'localhost' }), false);
  assert.equal(isSameSiteRequest({ secFetchSite: 'cross-site', hostname: 'localhost' }), false);
  assert.equal(isSameSiteRequest({ hostname: 'localhost' }), true, 'không có Origin (curl) thì cho qua');
});
