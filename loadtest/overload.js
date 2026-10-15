// ============================================================================
// Kịch bản QUÁ TẢI bằng k6 — cố tình dồn traffic cho web "đau" để xem
// dashboard và luật cảnh báo phản ứng ra sao. Khác load.js ở 3 điểm:
//   - KHÔNG nghỉ giữa các lần gọi (load.js nghỉ 0,3 giây), nên mỗi khách ảo
//     gọi liên tục, tạo nhiều request hơn hẳn
//   - Nhiều khách hơn (mặc định 300) và giữ tải lâu (mặc định 16 phút): luật CPU
//     tính trung bình 5 phút rồi chờ thêm `for: 10m`, nên cần khoảng 15 phút mới đỏ
//   - Nhiều request đặt hàng hơn (loại nặng nhất: có transaction, khoá dòng)
//
// Chạy (PowerShell, ở thư mục gốc repo):
//   docker run --rm --network tool-monitoring_appnet `
//     -v "${PWD}/loadtest:/scripts:ro" grafana/k6:0.54.0 run /scripts/overload.js
//
// Chỉnh mức tải bằng biến môi trường (thêm vào lệnh trên, trước tên image):
//   -e VUS=500 -e HOLD=15m
// Dừng giữa chừng: Ctrl+C.
// Xem README mục 7.12.
// ============================================================================

import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://nginx:80';
const VUS = parseInt(__ENV.VUS || '300', 10);
const HOLD = __ENV.HOLD || '16m';

export const options = {
  stages: [
    { duration: '1m', target: VUS },  // tăng dần lên mức tải
    { duration: HOLD, target: VUS },  // giữ tải cao đủ lâu để luật cảnh báo kịp đỏ
    { duration: '30s', target: 0 },   // hạ về 0 để xem hệ thống hồi phục
  ],
  // Quá tải thì lỗi là chuyện bình thường; không dừng test vì lỗi
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.9', abortOnFail: false }],
  },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const r = Math.random();
  const id = 1 + Math.floor(Math.random() * 6);

  if (r < 0.5) {
    const res = http.get(`${BASE}/api/products`, { tags: { name: 'GET /api/products' } });
    check(res, { 'products 200': (x) => x.status === 200 });
  } else if (r < 0.7) {
    const res = http.get(`${BASE}/api/products/${id}`, { tags: { name: 'GET /api/products/:id' } });
    check(res, { 'detail 200': (x) => x.status === 200 });
  } else {
    // 30% là đặt hàng (load.js chỉ 10%)
    const res = http.post(`${BASE}/api/orders`, JSON.stringify({
      customer: `overload-${__VU}`,
      email: `overload-${__VU}@example.com`,
      items: [{ productId: id, quantity: 1 }],
    }), { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /api/orders' } });
    check(res, { 'order 201 or 409': (x) => x.status === 201 || x.status === 409 });
  }
}
