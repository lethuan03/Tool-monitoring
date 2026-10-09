# Shop Demo — Web thương mại điện tử

Ứng dụng demo tách rõ **3 tầng**: Frontend (tĩnh) · Backend (REST API) · Database (MySQL). Có sẵn `Dockerfile` + `docker-compose` để đóng gói, và các endpoint `/health`, `/metrics` để gắn tool monitoring về sau.

## Cấu trúc

```
.
├── backend/              # REST API (Node.js + Express)
│   ├── src/
│   │   ├── server.js     # Khởi tạo app, /health, /metrics, phục vụ frontend
│   │   ├── db.js         # Kết nối MySQL (mysql2) + tạo bảng + seed dữ liệu mẫu
│   │   └── routes/
│   │       ├── products.js
│   │       └── orders.js
│   └── package.json
├── frontend/             # Giao diện tĩnh (HTML/CSS/JS thuần)
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── Dockerfile
├── docker-compose.yml
└── README.md
```

Database là **MySQL 8**. Khi khởi động, backend tự kết nối (có retry chờ MySQL sẵn sàng), tạo các bảng `products`, `orders`, `order_items` nếu chưa có, và seed 6 sản phẩm mẫu ở lần chạy đầu.

## Biến môi trường (kết nối DB)

| Biến          | Mặc định     | Mô tả                     |
| ------------- | ------------ | ------------------------- |
| `DB_HOST`     | `localhost`  | Host MySQL                |
| `DB_PORT`     | `3306`       | Cổng MySQL                |
| `DB_USER`     | `root`       | User                      |
| `DB_PASSWORD` | `root`       | Mật khẩu                  |
| `DB_NAME`     | `ecommerce`  | Tên database              |
| `PORT`        | `3000`       | Cổng web/API              |

## Chạy bằng Docker (khuyến nghị — có sẵn MySQL)

```bash
docker compose up --build
```

- Web: <http://localhost:3000>
- MySQL chạy ở service `db`, dữ liệu lưu ở volume `ecommerce-db-data` nên không mất khi restart.
- `web` chờ `db` healthy rồi mới khởi động (đã cấu hình `depends_on`).

## Chạy local (không Docker)

Yêu cầu: Node.js 18+ và một MySQL đang chạy.

```bash
cd backend
npm install

# trỏ tới MySQL của bạn (ví dụ)
set DB_HOST=localhost&& set DB_USER=root&& set DB_PASSWORD=yourpass&& set DB_NAME=ecommerce&& npm start   # Windows CMD
# hoặc PowerShell:
# $env:DB_PASSWORD="yourpass"; npm start
```

Backend sẽ tự tạo database `ecommerce` (nếu user có quyền) và các bảng. Mở <http://localhost:3000>.

## API

| Method | Endpoint             | Mô tả                                   |
| ------ | -------------------- | --------------------------------------- |
| GET    | `/api/products`      | Danh sách sản phẩm                      |
| GET    | `/api/products/:id`  | Chi tiết 1 sản phẩm                     |
| POST   | `/api/products`      | Thêm sản phẩm `{name, price, ...}`      |
| GET    | `/api/orders`        | Danh sách đơn hàng                      |
| GET    | `/api/orders/:id`    | Chi tiết 1 đơn hàng                     |
| POST   | `/api/orders`        | Đặt hàng `{customer, email, items[]}`   |
| GET    | `/health`            | Health check (dùng cho orchestration)   |
| GET    | `/metrics`           | Metrics kiểu Prometheus (text)          |

### Ví dụ đặt hàng

```bash
curl -X POST http://localhost:3000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customer":"Nguyen Van A","email":"a@example.com","items":[{"productId":1,"quantity":2}]}'
```

## Dành cho monitoring (chưa cấu hình, chỉ để sẵn)

- `/health` trả JSON trạng thái + uptime — hợp cho liveness/readiness probe.
- `/metrics` trả plaintext kiểu Prometheus (`app_uptime_seconds`, `app_http_requests_total`, `app_http_responses_total{status=...}`).
- Container có sẵn `HEALTHCHECK` trong Dockerfile.
- MySQL muốn có metric nội bộ thì cắm thêm `mysqld_exporter` (chưa làm).

Khi cần, bạn chỉ việc trỏ Prometheus scrape `/metrics` và dựng dashboard (Grafana...) — phần này mình chưa làm theo yêu cầu.
