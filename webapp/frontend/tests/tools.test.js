// Chạy (từ thư mục gốc dự án): node --test frontend/tests/tools.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { TOOL_CONFIG, buildToolUrl } = require('../tools.js');
const byName = (name) => TOOL_CONFIG.tools.find((t) => t.name === name);
test('URL là đường dẫn tương đối qua nginx (không còn host:port)', () => {
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Grafana')), '/admin/grafana/');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus')), '/admin/prometheus/query');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus Targets')), '/admin/prometheus/targets');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus Alerts')), '/admin/prometheus/alerts');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Alertmanager')), '/admin/alertmanager/');
});
test('Chỉ còn 5 ô monitoring được proxy (bỏ Alloy, Mailpit, Loki, Health check)', () => {
  assert.equal(byName('Alloy'), undefined);
  assert.equal(byName('Mailpit'), undefined);
  assert.equal(byName('Loki'), undefined);
  assert.equal(byName('Health check'), undefined);
  assert.equal(TOOL_CONFIG.tools.length, 5);
});
test('buildToolUrl trả về đúng path tương đối đã khai báo', () => {
  assert.equal(buildToolUrl(TOOL_CONFIG, { name: 'x', path: '/admin/grafana/' }), '/admin/grafana/');
  assert.equal(buildToolUrl(TOOL_CONFIG, { name: 'y', path: '/admin/prometheus/targets' }), '/admin/prometheus/targets');
});
test('Mọi tool đều có tên, path và file logo có thật', () => {
  for (const tool of TOOL_CONFIG.tools) {
    assert.ok(tool.name, 'thiếu tên');
    assert.ok(tool.path, `thiếu path cho ${tool.name}`);
    assert.ok(tool.icon, `thiếu icon cho ${tool.name}`);
    assert.ok(fs.existsSync(path.join(__dirname, '..', tool.icon)), `không thấy file ${tool.icon}`);
  }
});
