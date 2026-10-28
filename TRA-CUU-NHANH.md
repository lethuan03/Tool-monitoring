# Tra cứu nhanh — Tool, port, đường dẫn

> `<web>` = địa chỉ ALB hoặc IP public của EC2. Hệ thống chạy trên 1 EC2 bằng 2 file: `docker-compose.app.yml` (web) và `docker-compose.monitoring.yml` (giám sát). Chi tiết: README mục 1 và 12.

## 1. Mở trên trình duyệt

| Tool | Vai trò | Link | Đăng nhập | Dùng khi nào |
| ---- | ------- | ---- | --------- | ------------ |
| **Web shop** | Cửa hàng | `<web>/` | Không cần | Dùng web, tạo traffic |
| **Trang Admin** | Quầy quản lý | `<web>/login.html` → `<web>/admin.html` | `ADMIN_USERNAME` / `ADMIN_PASSWORD` (`.env.app`, chỉ dùng lần tạo đầu) | Thêm / sửa / xoá sản phẩm, đổi mật khẩu; mục **Monitoring** mở các tool bên dưới ở tab mới |
| **Grafana** | Bảng điều khiển | `<web>/admin/grafana/` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` (`.env.monitoring`) | **Theo dõi hằng ngày**: dashboard Shop Overview (số liệu), Shop Logs (log) |
| **Prometheus — PromQL** | Sổ ghi số | `<web>/admin/prometheus/query` | `MONITORING_BASIC_AUTH_USER` / `..._PASSWORD` (`.env.app`) | Tra cứu, thử query |
| **Prometheus — target** | | `<web>/admin/prometheus/targets` | như trên | Xem 6 nguồn số liệu có **UP** không |
| **Prometheus — cảnh báo** | | `<web>/admin/prometheus/alerts` | như trên | Luật nào đang **pending** / **firing** |
| **Alertmanager** | Người đưa thư | `<web>/admin/alertmanager/` | như trên | Xem cảnh báo đang gửi, tạm tắt thông báo (Silence) |
| **Health check** | | `<web>/health` | Không cần | Backend còn sống không |

## 2. Container và port

| Container | File compose | Image | Port nội bộ | Mở ra máy? | Ai gọi tới |
| --------- | ------------ | ----- | ----------- | ---------- | ---------- |
| ecommerce-nginx | app | `thuanperry/ecommerce-nginx` | `80`, `8081` (stub_status) | **Public**: `80` (`HTTP_PORT`) | Trình duyệt; nginx-exporter đọc `:8081` |
| ecommerce-frontend | app | `thuanperry/ecommerce-frontend` | `80` | Không | nginx |
| ecommerce-backend | app | `thuanperry/ecommerce-backend` | `3000` (`/api`, `/health`, `/metrics`) | Không | nginx; Prometheus đọc `/metrics` |
| ecommerce-db | app | `thuanperry/ecommerce-db` (MySQL 8.0.40) | `3306` | Không | backend, mysqld-exporter |
| ecommerce-nginx-exporter | app | `nginx/nginx-prometheus-exporter:1.3.0` | `9113` | Không | Prometheus |
| ecommerce-mysqld-exporter | app | `prom/mysqld-exporter:v0.15.1` | `9104` | Không | Prometheus |
| ecommerce-node-exporter | app | `prom/node-exporter:v1.12.1` | `9100` | Không | Prometheus (CPU/RAM/ổ đĩa EC2) |
| ecommerce-alloy | app | `thuanperry/ecommerce-alloy` | `12345` | Không | Đọc log container, đẩy sang Loki |
| ecommerce-prometheus | monitoring | `thuanperry/ecommerce-prometheus` | `9090` | Chỉ `127.0.0.1` | nginx (`/admin/prometheus/`), Grafana, HolmesGPT |
| ecommerce-alertmanager | monitoring | `thuanperry/ecommerce-alertmanager` | `9093` | Chỉ `127.0.0.1` | Prometheus đẩy cảnh báo; nó gửi Telegram / mail / holmes-bridge |
| ecommerce-loki | monitoring | `thuanperry/ecommerce-loki` | `3100` | Chỉ `127.0.0.1` | Alloy đẩy log; Grafana, HolmesGPT đọc |
| ecommerce-grafana | monitoring | `thuanperry/ecommerce-grafana` | `3000` | Chỉ `127.0.0.1:3001` | nginx (`/admin/grafana/`) |
| ecommerce-holmes-bridge (profile `ai`) | monitoring | `thuanperry/ecommerce-holmes-bridge` | `8080` | Không | Alertmanager (webhook) |
| holmes (profile `ai`, chạy theo lệnh) | monitoring | `thuanperry/ecommerce-holmes` | không có | Không | Bạn gọi bằng `run --rm` |
| ecommerce-mailpit (profile `mail-test`) | monitoring | `axllent/mailpit:v1.31.5` | `1025`, `8025` | Chỉ `127.0.0.1:8025` | Alertmanager gửi mail test |

Security Group chỉ cần mở **80**. Hai file compose nói chuyện với nhau qua network Docker `ecommerce-shared`.

## 3. Đường đi của dữ liệu

```
Khách ──► nginx :80 ──► frontend / backend :3000 ──► db :3306

Prometheus ── mỗi 15s lấy số ──► backend, nginx-exporter, mysqld-exporter, node-exporter
Alloy ── đọc log mọi container ──► Loki
Grafana ── PromQL / LogQL ──► Prometheus, Loki

Prometheus ── luật FIRING ──► Alertmanager ──► Telegram / mail
                                   └─► holmes-bridge ──► HolmesGPT (+ Bedrock) ──► Telegram: phân tích + xác nhận đã ổn
```

## 4. Lệnh hay dùng (trên EC2)

Mỗi lần vào EC2, khai báo trước:

```bash
sudo -i; cd /opt/ecommerce
APP="docker compose -f docker-compose.app.yml --env-file .env.app"
MON="docker compose -f docker-compose.monitoring.yml --env-file .env.monitoring"
```

| Việc | Lệnh |
| ---- | ---- |
| Bật cả hệ thống | `$APP up -d && $MON up -d` |
| Bật AI (holmes-bridge) | `$MON --profile ai up -d holmes-bridge` |
| Cập nhật image mới | `$APP pull && $APP up -d` · `$MON pull && $MON up -d` |
| Xem trạng thái | `docker ps` |
| Xem log | `docker logs -f ecommerce-backend` (đổi tên container) |
| Giả lập DB chết / khôi phục | `docker stop ecommerce-db` / `docker start ecommerce-db` |
| Sửa `.env.*` xong | `$APP up -d --force-recreate <service>` hoặc `$MON ...` (`restart` **không** đọc lại `.env`) |
| Hỏi AI (**tốn phí Bedrock**) | `$MON --profile ai run --rm holmes ask "Vì sao web lỗi 5xx?" --no-interactive` |
| Tắt (giữ dữ liệu) | `$MON down && $APP down` (thêm `-v` để xoá cả dữ liệu) |
| Build + push image (máy local) | `.\scripts\push-images.ps1` |

PromQL mẫu: README mục 4.3. LogQL mẫu: README mục 4.6.
