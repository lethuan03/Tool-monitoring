// Chạy (từ thư mục gốc dự án): node --test frontend/tests/tools.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { TOOL_CONFIG, buildToolUrl } = require('../tools.js');

const byName = (name) => TOOL_CONFIG.tools.find((t) => t.name === name);

test('URL đúng cho các tool chính với cổng mặc định', () => {
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Grafana')), 'http://localhost:3001/');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus')), 'http://localhost:9090/query');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus Targets')), 'http://localhost:9090/targets');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Prometheus Alerts')), 'http://localhost:9090/alerts');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Alertmanager')), 'http://localhost:9093/');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Alloy')), 'http://localhost:12345/');
  assert.equal(buildToolUrl(TOOL_CONFIG, byName('Mailpit')), 'http://localhost:8025/');
});

test('Không còn ô Loki và Health check', () => {
  assert.equal(byName('Loki'), undefined);
  assert.equal(byName('Health check'), undefined);
  assert.equal(TOOL_CONFIG.tools.length, 7);
});

test('Path có hoặc không có "/" đầu đều ra cùng URL', () => {
  const cfg = { host: 'localhost', ports: { x: 1234 } };
  assert.equal(buildToolUrl(cfg, { name: 'a', port: 'x', path: 'abc' }), 'http://localhost:1234/abc');
  assert.equal(buildToolUrl(cfg, { name: 'b', port: 'x', path: '/abc' }), 'http://localhost:1234/abc');
  assert.equal(buildToolUrl(cfg, { name: 'c', port: 'x' }), 'http://localhost:1234/');
});

test('Đổi cổng trong config thì URL đổi theo', () => {
  const cfg = { ...TOOL_CONFIG, ports: { ...TOOL_CONFIG.ports, grafana: 3999 } };
  assert.equal(buildToolUrl(cfg, byName('Grafana')), 'http://localhost:3999/');
});

test('Báo lỗi rõ ràng khi tool trỏ tới cổng chưa khai báo', () => {
  assert.throws(
    () => buildToolUrl(TOOL_CONFIG, { name: 'Lạ', port: 'khong-co' }),
    /Chưa khai báo cổng/
  );
});

test('Mọi tool đều có tên, file logo có thật và cổng', () => {
  for (const tool of TOOL_CONFIG.tools) {
    assert.ok(tool.name, 'thiếu tên');
    assert.ok(tool.icon, `thiếu icon cho ${tool.name}`);
    assert.ok(fs.existsSync(path.join(__dirname, '..', tool.icon)), `không thấy file ${tool.icon}`);
    assert.ok(TOOL_CONFIG.ports[tool.port], `thiếu cổng cho ${tool.name}`);
  }
});
