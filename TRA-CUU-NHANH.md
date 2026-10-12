# Tra cứu nhanh — Tool, port, đường dẫn

> Port mặc định theo `.env.example`. Nếu bạn đã đổi `HTTP_PORT` / `GRAFANA_PORT` trong `.env` thì thay số tương ứng.
> Ví dụ máy đang trùng 8080 với app khác thì đặt `HTTP_PORT=8088` → web ở <http://localhost:8088>.

## 1. Mở trên trình duyệt (dùng hằng ngày)

| Tool | Vai trò (ví von) | Link | Đăng nhập | Dùng khi nào |
| ---- | ---------------- | ---- | --------- | ------------ |
| **Web shop** | Cửa hàng | <http://localhost:8080> | Không cần | Dùng web, tạo traffic |
| **Grafana** | Bảng điều khiển | <http://localhost:3001> | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` trong `.env` | **Demo, theo dõi hằng ngày** (dashboard Shop Overview) |
| **Prometheus — gõ PromQL** | Sổ ghi số | <http://localhost:9090/query> | Không cần (chỉ mở trên máy này) | Tra cứu, thử query |
| **Prometheus — danh sách target** | | <http://localhost:9090/targets> | | Xem 4 nơi Prometheus đi lấy số có **UP** không |
| **Health check web** | | <http://localhost:8080/health> | Không cần | Kiểm tra backend còn sống |

## 2. Toàn bộ container và port

**Mở ra máy?**

- **Có**: mở trên máy bạn, có thể cho người khác truy cập.
- **Chỉ localhost**: chỉ người ngồi trên máy đó mở được.
- **Không**: chỉ các container nói chuyện với nhau trong mạng nội bộ Docker. Từ trình duyệt **không** mở được, đây là cố ý vì lý do bảo mật.

| Service (tên trong compose) | Container | Image | Port nội bộ | Mở ra máy? | Ai gọi tới |
| --------------------------- | --------- | ----- | ----------- | ---------- | ---------- |
| `nginx` | ecommerce-nginx | `nginx:1.27-alpine` | `80` (web), `8081` (stub_status) | **Có**: `8080` → 80 (đổi bằng `HTTP_PORT`) | Trình duyệt của khách; nginx-exporter đọc `:8081` |
| `frontend` | ecommerce-frontend | `nginx:1.27-alpine` (build) | `80` | Không | nginx |
| `backend` | ecommerce-backend | build từ `backend/` | `3000` (`/api`, `/health`, `/metrics`) | Không | nginx; Prometheus đọc `/metrics` |
| `db` | ecommerce-db | `mysql:8.0.40` | `3306` | Không | backend, mysqld-exporter |
| `nginx-exporter` | ecommerce-nginx-exporter | `nginx/nginx-prometheus-exporter:1.3.0` | `9113` | Không | Prometheus |
| `mysqld-exporter` | ecommerce-mysqld-exporter | `prom/mysqld-exporter:v0.15.1` | `9104` | Không | Prometheus |
| `prometheus` | ecommerce-prometheus | `prom/prometheus:v3.1.0` | `9090` | **Chỉ localhost**: `127.0.0.1:9090` | Bạn (trình duyệt); Grafana |
| `grafana` | ecommerce-grafana | `grafana/grafana:11.3.0` | `3000` | **Chỉ localhost**: `127.0.0.1:3001` (đổi bằng `GRAFANA_PORT`) | Bạn (trình duyệt) |

Grafana bên trong container cũng chạy port `3000`, nhưng đó là "cửa riêng" của container Grafana nên **không đụng** port 3000 của backend. Ra ngoài máy thì nó được đổi thành `3001`.

## 3. Đường đi của số liệu

```
Khách ──► nginx :8080 ──► frontend :80 / backend :3000 ──► db :3306

Prometheus :9090 ── mỗi 15s đi lấy số ──► backend :3000/metrics
                                       ├─► nginx-exporter :9113 ──► nginx :8081/stub_status
                                       └─► mysqld-exporter :9104 ──► db :3306

Grafana :3001 ── hỏi PromQL mỗi 15s ──► Prometheus :9090
```

## 4. Lệnh hay dùng

| Việc | Lệnh |
| ---- | ---- |
| Bật toàn bộ | `docker compose up -d --build` |
| Xem trạng thái | `docker compose ps` |
| Xem log một service | `docker compose logs -f backend` (thay `backend` bằng tên service ở bảng trên) |
| Tắt (giữ dữ liệu) | `docker compose down` |
| Tắt + xoá sạch dữ liệu | `docker compose down -v` |
| Giả lập sự cố DB / khôi phục | `docker compose stop db` / `docker compose start db` |

PromQL mẫu để thử: xem **README mục 4.3**.
