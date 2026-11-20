// Chạy: npm test (trong thư mục backend)
const test = require('node:test');
const assert = require('node:assert/strict');
const { validateProduct, parseId } = require('../src/validate-product');

const ok = { name: 'Chuột', description: 'Không dây', price: 390000, stock: 10, image: 'https://picsum.photos/seed/a/400/300' };

test('Dữ liệu hợp lệ thì trả value đã chuẩn hoá', () => {
  const { value, error } = validateProduct({ ...ok, name: '  Chuột  ' });
  assert.equal(error, undefined);
  assert.equal(value.name, 'Chuột');
  assert.equal(value.price, 390000);
  assert.equal(value.stock, 10);
});

test('Mô tả, tồn kho, ảnh được để trống thì lấy mặc định', () => {
  const { value } = validateProduct({ name: 'A', price: 1 });
  assert.deepEqual(value, { name: 'A', description: '', price: 1, stock: 0, image: '' });
});

test('Thiếu tên hoặc tên toàn khoảng trắng thì lỗi', () => {
  assert.ok(validateProduct({ price: 1 }).error);
  assert.ok(validateProduct({ name: '   ', price: 1 }).error);
  assert.ok(validateProduct({ name: 'x'.repeat(256), price: 1 }).error);
});

test('Giá phải là số >= 0', () => {
  for (const price of ['100', -1, NaN, Infinity, null, undefined, 1e11]) {
    assert.ok(validateProduct({ ...ok, price }).error, `price=${price} phải lỗi`);
  }
  assert.equal(validateProduct({ ...ok, price: 0 }).value.price, 0);
  assert.equal(validateProduct({ ...ok, price: 10.005 }).value.price, 10.01);
});

test('Tồn kho phải là số nguyên >= 0', () => {
  for (const stock of [1.5, -1, '5', null, 4294967296]) {
    assert.ok(validateProduct({ ...ok, stock }).error, `stock=${stock} phải lỗi`);
  }
  assert.equal(validateProduct({ ...ok, stock: 0 }).value.stock, 0);
});

test('Ảnh chỉ nhận http(s), ký tự nguy hiểm bị mã hoá', () => {
  assert.ok(validateProduct({ ...ok, image: 'javascript:alert(1)' }).error);
  assert.ok(validateProduct({ ...ok, image: 'not a url' }).error);
  const { value } = validateProduct({ ...ok, image: 'https://x.test/a"onerror="alert(1)' });
  assert.ok(!value.image.includes('"'), value.image);
});

test('parseId chỉ nhận số nguyên dương', () => {
  assert.equal(parseId('7'), 7);
  for (const raw of ['0', '-1', 'abc', '1.5', '1;DROP', '']) assert.equal(parseId(raw), null);
});
