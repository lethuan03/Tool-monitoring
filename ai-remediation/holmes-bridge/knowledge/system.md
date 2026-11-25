# Hệ thống Nova Store — bản đồ cho AI (đọc kỹ trước khi điều tra)

## Chuỗi phụ thuộc (gốc → ngọn)
`ecommerce-db` (MySQL) → `ecommerce-backend` (Node.js :3000) → `ecommerce-nginx` (cổng vào công khai) ← người dùng.
`ecommerce-frontend` chỉ là file tĩnh, nginx phục vụ giúp. Giám sát chạy riêng, nối qua mạng `ecommerce-shared`.
- DB chết ⇒ backend trả 5xx ⇒ `High5xxRate`; `MySQLDown` bật. Tìm nguyên nhân ở DB trước, đừng restart backend khi DB còn tắt.
- Backend chết ⇒ nginx trả 502; `TargetDown{job=backend}`.
- Chỉ exporter chết ⇒ khách KHÔNG bị ảnh hưởng, chỉ mất số liệu.

## Container ↔ job Prometheus ↔ service Loki
| Container | Job Prometheus (`up`) | Số liệu chính | Nhãn Loki `service` |
| --------- | --------------------- | ------------- | ------------------- |
| `ecommerce-backend` | `backend` (:3000/metrics) | `http_requests_total{route,status_code}`, `http_request_duration_seconds_bucket` | `backend` |
| `ecommerce-nginx-exporter` | `nginx` (:9113) | `nginx_up` (nginx còn trả lời stub_status) | `nginx-exporter` |
| `ecommerce-mysqld-exporter` | `mysql` (:9104) | `mysql_up` (exporter nối được DB), `mysql_global_status_threads_connected` | `mysqld-exporter` |
| `ecommerce-node-exporter` | `node` (:9100) | `node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*` (CPU/RAM/ổ đĩa của máy chủ) | `node-exporter` |
| `ecommerce-prometheus` | `prometheus` | chính nó | không gom log |
| `ecommerce-alertmanager` | `alertmanager` | chính nó | không gom log |
| `ecommerce-db` | không có job riêng | xem `mysql_*` | `db` |
| `ecommerce-nginx` | không có job riêng | xem `nginx_*` (qua exporter) | `nginx` |
| `ecommerce-frontend`, `ecommerce-alloy`, `ecommerce-remediator` | không có job | — | `frontend`, `alloy`, `remediator` |
Loki, Grafana, Alloy KHÔNG có cảnh báo `up`; muốn biết chúng sống không thì dùng `read_status` (`inspect_container`).
Nhãn Loki có thật: `service`, `container`, `level` (error/warn/info). Đừng dùng nhãn khác (path, ip...).

## Điều trông lạ nhưng BÌNH THƯỜNG
- `mysql_up == 0` không có nghĩa DB chết: exporter có thể sống nhưng không nối được DB (DB tắt, hoặc đổi mật khẩu user `exporter`). Phân biệt bằng `inspect_container ecommerce-db` (running?) và log `{service="db"}`.
- Job `nginx` chỉ chứng minh exporter của nginx sống; dùng `nginx_up` để biết nginx.
- Cảnh báo có `for: 1m` (chế độ demo): vừa restart xong cảnh báo vẫn còn ~1-2 phút dù hệ thống đã ổn. Đừng restart lần 2 chỉ vì thế.
- Sau khi restart container, `process_start_time_seconds` mới và vài giây `up == 0` là bình thường.

## Cách điều tra tiết kiệm bước
1. `up == 0` cho biết thành phần nào mất; nhiều thành phần mất cùng lúc thường do một gốc chung (DB, mạng, máy chủ).
2. `read_status` với `inspect_container` cho trạng thái thật của container (chạy/dừng, mã thoát, OOM, số lần restart) trước khi đoán.
3. Log: `{service="<tên>", level="error"}` 15 phút gần nhất; `logs_tail` khi cần dòng cuối của container.
4. Chỉ đề xuất lệnh khi bằng chứng rõ và runbook cho phép; mỗi phiên tối đa 1 đề xuất.

## Kho lệnh (nguồn: policy.json — kiểm tra bằng `list_allowed_actions`)
- Chỉ đọc, chạy ngay (`read_status`): `logs_tail`, `inspect_container`, `container_stats`.
- Cần người duyệt (`propose_fix`): `restart_container`, `start_container`.
- `ecommerce-db`: chỉ được `inspect_container` và `start_container` (bật lại DB đã dừng). KHÔNG restart, KHÔNG xem log qua `logs_tail` (dùng Loki).
- Không bao giờ đụng: `ecommerce-holmes-bridge`, `ecommerce-remediator`, `ecommerce-docker-proxy`.
- Không có lệnh dừng, xoá, dọn dẹp, sửa dữ liệu, rollback: gặp việc đó thì nêu rõ cần người vận hành xử lý tay.
