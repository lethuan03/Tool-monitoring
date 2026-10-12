// ============================================================================
// Kịch bản tạo tải (load test) bằng k6 — giả lập khách vào ồ ạt.
//
// Mục đích KHÔNG phải để "làm sập" web, mà để kiểm chứng:
//   1. Prometheus có đếm đúng số request không (so với số k6 tự đếm)
//   2. p95 thời gian phản hồi có tăng đúng khi quá tải không
//   3. Loki có ghi đủ log không (so với 2 nguồn trên)
//   4. Nút thắt có đúng như dự đoán không (pool 10 kết nối MySQL)
//
// Chạy:
//   docker run --rm --network tool-monitoring_appnet \
//     -v "${PWD}/loadtest:/scripts" grafana/k6:0.54.0 run /scripts/load.js
// ============================================================================

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

// Đếm riêng theo loại request để đối chiếu với Prometheus
const reqProducts = new Counter('my_products_requests');
const reqDetail = new Counter('my_detail_requests');
const reqOrders = new Counter('my_orders_requests');
const reqHome = new Counter('my_home_requests');

// Gọi nginx qua mạng nội bộ Docker (không qua port 8080 của host)
const BASE = __ENV.BASE_URL || 'http://nginx:80';

export const options = {
  // Tăng tải theo bậc để thấy rõ hệ thống "bắt đầu đau" ở mức nào
  stages: [
    { duration: '30s', target: 10 },   // bình thường
    { duration: '45s', target: 50 },   // đông
    { duration: '60s', target: 120 },  // quá tải (vượt xa pool 10 kết nối DB)
    { duration: '30s', target: 120 },  // giữ tải cao
    { duration: '20s', target: 0 },    // hạ về 0 để xem hệ thống hồi phục
  ],
  thresholds: {
    // Không đặt abortOnFail: muốn test chạy hết để thu đủ số liệu,
    // kể cả khi hệ thống đã quá tải.
    http_req_failed: [{ threshold: 'rate<0.5', abortOnFail: false }],
  },
  // Tắt báo cáo rút gọn mặc định, in bản tóm tắt đầy đủ ở cuối
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  // Phân bổ giống hành vi khách thật: xem danh sách nhiều nhất,
  // xem chi tiết ít hơn, đặt hàng ít nhất.
  const r = Math.random();

  if (r < 0.45) {
    const res = http.get(`${BASE}/api/products`, { tags: { name: 'GET /api/products' } });
    reqProducts.add(1);
    check(res, { 'products 200': (x) => x.status === 200 });
  } else if (r < 0.70) {
    const id = 1 + Math.floor(Math.random() * 6);
    const res = http.get(`${BASE}/api/products/${id}`, { tags: { name: 'GET /api/products/:id' } });
    reqDetail.add(1);
    check(res, { 'detail 200': (x) => x.status === 200 });
  } else if (r < 0.90) {
    const res = http.get(`${BASE}/`, { tags: { name: 'GET /' } });
    reqHome.add(1);
    check(res, { 'home 200': (x) => x.status === 200 });
  } else {
    // Đặt hàng: đây là request NẶNG NHẤT — có transaction + SELECT FOR UPDATE
    const id = 1 + Math.floor(Math.random() * 6);
    const payload = JSON.stringify({
      customer: `k6-user-${__VU}`,
      email: `k6-${__VU}@example.com`,
      items: [{ productId: id, quantity: 1 }],
    });
    const res = http.post(`${BASE}/api/orders`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /api/orders' },
    });
    reqOrders.add(1);
    // 201 = đặt được, 409 = hết hàng (vẫn là hành vi ĐÚNG, không phải lỗi)
    check(res, { 'order 201 or 409': (x) => x.status === 201 || x.status === 409 });
  }

  sleep(0.3);
}
