# Tra cứu nhanh — Tool, port, đường dẫn

> Port mặc định theo `.env.example`. Nếu bạn đã đổi `HTTP_PORT` / `GRAFANA_PORT` trong `.env` thì thay số tương ứng.
> Ví dụ máy đang trùng 8080 với app khác thì đặt `HTTP_PORT=8088` → web ở <http://localhost:8088>.

## 1. Mở trên trình duyệt (dùng hằng ngày)

| Tool | Vai trò (ví von) | Link | Đăng nhập | Dùng khi nào |
| ---- | ---------------- | ---- | --------- | ------------ |
| **Web shop** | Cửa hàng | <http://localhost:8080> | Không cần | Dùng web, tạo traffic |
| **Grafana** | Bảng điều khiển | <http://localhost:3001> | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` trong `.env` | **Demo, theo dõi hằng ngày** (dashboard Shop Overview) |
| **Prometheus — gõ PromQL** | Sổ ghi số | <http://localhost:9090/query> | Không cần (chỉ mở trên máy này) | Tra cứu, thử query |
| **Prometheus — danh sách target** | | <http://localhost:9090/targets> | | Xem 6 nơi Prometheus đi lấy số có **UP** không |
| **Prometheus — cảnh báo** | | <http://localhost:9090/alerts> | | Xem luật nào đang **pending** / **firing** (README mục 7.11) |
| **Alertmanager** | Người đưa thư | <http://localhost:9093> | Không cần | Xem cảnh báo đang gửi, tạm tắt thông báo (Silence) |
| **mailpit** (chỉ khi test mail) | Hộp thư giả | <http://localhost:8025> | Không cần | Xem mail cảnh báo khi chưa dùng Gmail thật |
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
| `node-exporter` | ecommerce-node-exporter | `prom/node-exporter:v1.12.1` | `9100` | Không | Prometheus (số đo CPU/RAM/ổ đĩa của máy chủ) |
| `alertmanager` | ecommerce-alertmanager | `prom/alertmanager:v0.34.1` | `9093` | **Chỉ localhost**: `127.0.0.1:9093` | Prometheus đẩy cảnh báo tới; nó gửi Telegram / mail |
| `mailpit` (chỉ khi test) | ecommerce-mailpit | `axllent/mailpit:v1.31.5` | `1025` (nhận mail), `8025` (web) | **Chỉ localhost**: `127.0.0.1:8025` | Alertmanager gửi mail test tới |
| `certbot` (chỉ EC2) | ecommerce-certbot | `certbot/certbot:v5.8.0` | không có | Không | Tự gia hạn chứng chỉ HTTPS (README mục 7.10) |
| `prometheus` | ecommerce-prometheus | `prom/prometheus:v3.1.0` | `9090` | **Chỉ localhost**: `127.0.0.1:9090` | Bạn (trình duyệt); Grafana |
| `grafana` | ecommerce-grafana | `grafana/grafana:11.3.0` | `3000` | **Chỉ localhost**: `127.0.0.1:3001` (đổi bằng `GRAFANA_PORT`) | Bạn (trình duyệt) |

Grafana bên trong container cũng chạy port `3000`, nhưng đó là "cửa riêng" của container Grafana nên **không đụng** port 3000 của backend. Ra ngoài máy thì nó được đổi thành `3001`.

## 3. Đường đi của số liệu

```
Khách ──► nginx :8080 ──► frontend :80 / backend :3000 ──► db :3306

Prometheus :9090 ── mỗi 15s đi lấy số ──► backend :3000/metrics
                                       ├─► nginx-exporter :9113 ──► nginx :8081/stub_status
                                       ├─► mysqld-exporter :9104 ──► db :3306
                                       └─► node-exporter :9100 ──► CPU/RAM/ổ đĩa của máy chủ

Grafana :3001 ── hỏi PromQL mỗi 15s ──► Prometheus :9090

Prometheus ── luật FIRING ──► Alertmanager :9093 ──► Telegram (và/hoặc mail)
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
| Sửa `.env` xong (token, mật khẩu...) | `docker compose up -d --force-recreate <tên service>` (`restart` **không** đọc lại `.env`) |
| Bật trên EC2 có HTTPS | `docker compose -f docker-compose.yml -f docker-compose.https.yml up -d` (lần đầu xem README mục 7.10) |

PromQL mẫu để thử: xem **README mục 4.3**.
