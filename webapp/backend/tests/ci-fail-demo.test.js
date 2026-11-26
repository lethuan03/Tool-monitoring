// CỐ Ý FAIL để thử bộ kéo log CI. Chỉ tồn tại trên nhánh test/ci-fail, không merge.
const test = require('node:test');
const assert = require('node:assert');

test('demo: cố ý fail để thử kéo log', () => {
  assert.strictEqual(1 + 1, 3, 'lỗi cố ý từ nhánh test/ci-fail');
});
