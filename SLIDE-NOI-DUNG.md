# Nội dung thuyết trình — Triển khai hệ thống lên AWS EC2

> File này tổng hợp đúng những gì đã triển khai thật (đối chiếu trực tiếp với
> code/compose/`.env` hiện tại), đã sửa lại vài chỗ bạn nói chưa khớp 100% với
> thực tế — xem phần "Đính chính" ở mỗi mục để biết chỗ nào đã sửa và vì sao.

---

## 1. Hạ tầng: 1 EC2, tách 2 docker-compose

Toàn bộ hệ thống chạy trên **1 EC2**, chia thành **2 file docker-compose độc
lập**, nối nhau qua 1 network Docker dùng chung (`ecommerce-shared`):

**Compose 1 — `docker-compose.app.yml` (phần ứng dụng):**
- `db` (MySQL) — lưu sản phẩm, đơn hàng, tài khoản đăng nhập
- `backend` (Node.js/Express) — API, xuất số liệu `/metrics`
- `frontend` — giao diện tĩnh (HTML/CSS/JS)
- `nginx` — cổng vào public duy nhất (reverse proxy)
- `nginx-exporter`, `mysqld-exporter`, `node-exporter` — đo số liệu webapp/nginx/MySQL/máy chủ
- `alloy` — gom log mọi container, đẩy sang Loki

**Compose 2 — `docker-compose.monitoring.yml` (phần giám sát + AI):**
- `prometheus` — kéo (pull) số liệu từ compose 1 mỗi 15s
- `alertmanager` — nhận cảnh báo từ Prometheus, gửi Telegram
- `loki` — nhận log Alloy đẩy sang
- `grafana` — vẽ dashboard từ Prometheus + Loki
- `holmes` — CLI HolmesGPT (chạy theo lệnh tay, dùng để hỏi trực tiếp)
- `holmes-bridge` — **cầu nối tự động** giữa Alertmanager và AI (xem mục 4)

> **Đính chính so với mô tả gốc**: compose 1 còn có `db` (không chỉ
> "app, nginx, exporter, alloy"); compose 2 còn có `alertmanager` và
> `holmes-bridge` (không chỉ "holmes, grafana, loki, prometheus").

Mọi image custom đều build sẵn, push lên Docker Hub (`thuanperry/ecommerce-*`,
public), EC2 chỉ `pull` về chạy — không build tại chỗ.

---

## 2. Luồng truy cập: ALB → nginx → các service

```
Internet → ALB → EC2:80 → nginx → {frontend, backend, grafana, prometheus, alertmanager}
```

- Khách vào trang web: ALB chuyển tới nginx (port 80), nginx forward `/` sang
  `frontend`, `/api/` sang `backend`.
- Trang quản trị shop (`/admin.html`...): nginx yêu cầu đăng nhập bằng tài
  khoản **admin của shop** (`ADMIN_USERNAME`/`ADMIN_PASSWORD`, lưu session
  trong MySQL) trước khi trả file.
- **Grafana** (`/admin/grafana/`): nginx chỉ đóng vai **trung gian chuyển
  tiếp** (reverse proxy thuần, không gắn thêm điều kiện đăng nhập nào ở
  nginx) — Grafana tự quản lý đăng nhập bằng tài khoản **riêng của nó**
  (`GRAFANA_ADMIN_USER`/`PASSWORD`), **khác** tài khoản admin của shop.
- **Prometheus** (`/admin/prometheus/`) và **Alertmanager**
  (`/admin/alertmanager/`): 2 tool này không có hệ thống đăng nhập riêng, nên
  nginx bảo vệ bằng **Basic Auth riêng** (`MONITORING_BASIC_AUTH_USER`/
  `PASSWORD`) — cũng là tài khoản **khác** với admin của shop.

> **Đính chính so với mô tả gốc**: không phải "vào Grafana phải đăng nhập
> admin [của shop]". Có **3 tài khoản đăng nhập độc lập nhau**: (1) admin của
> shop — lưu trong MySQL; (2) tài khoản riêng của Grafana; (3) Basic Auth của
> nginx cho Prometheus/Alertmanager. Tách riêng có chủ đích: nếu MySQL (tài
> khoản 1 phụ thuộc) bị sự cố, Grafana/Prometheus/Alertmanager (tài khoản 2,
> 3 không phụ thuộc MySQL) **vẫn xem được bình thường** — đúng lúc cần xem
> nhất khi có sự cố.

---

## 3. AI điều tra sự cố: AWS Bedrock

- Model: **Claude Sonnet 4.5** (qua AWS Bedrock)
- Region: **ap-southeast-1 (Singapore)**
- Xác thực: **Bedrock API key**, lưu trong `.env` (biến `AWS_BEARER_TOKEN_BEDROCK`)
- Công cụ: **HolmesGPT** — đọc số liệu (Prometheus) + log (Loki), **chỉ đọc**,
  không có quyền thay đổi hệ thống

Đúng như bạn nói — xác nhận khớp với cấu hình thật.

---

## 4. Cầu nối AI tự động: Alertmanager → holmes-bridge → Telegram

Đây là phần tự động hoá, chạy nền 24/7 (container `holmes-bridge`), **khác
với** việc gõ lệnh hỏi HolmesGPT bằng tay.

**Luồng đúng** (lưu ý: chiều ngược với mô tả gốc):

```
Prometheus phát hiện bất thường
        │ (luật cảnh báo FIRING)
        ▼
  Alertmanager
        │ gửi 2 việc CÙNG LÚC, độc lập nhau:
        ├──► Telegram: tin cảnh báo gốc (ngay lập tức, như trước giờ)
        └──► webhook POST /alert ──► holmes-bridge
                                         │
                                         ▼
                              Chạy HolmesGPT (đọc Prometheus + Loki,
                              gọi Bedrock/Claude Sonnet 4.5 suy luận)
                                         │
                                         ▼
                         Gửi 1 tin Telegram RIÊNG: Vấn đề · Nguyên nhân ·
                         Bằng chứng · Hướng xử lý
```

Khi sự cố **tự hết** (Prometheus đo lại thấy bình thường, Alertmanager báo
RESOLVED): `holmes-bridge` kiểm tra chéo lại Prometheus (không tốn thêm phí
Bedrock cho bước này) rồi gửi 1 tin Telegram xác nhận **"ĐÃ HOẠT ĐỘNG BÌNH
THƯỜNG"**, trả lời (reply) ngay dưới đúng tin phân tích AI lúc nãy.

> **Đính chính so với mô tả gốc**:
> - Không phải "holmes-bridge gửi tin đến Alertmanager" — **ngược lại**:
>   Alertmanager gọi webhook đến holmes-bridge. holmes-bridge gửi thẳng tới
>   Telegram, không đi qua Alertmanager lần nữa.
> - "Gửi tin xác nhận đã xử lý xong" (mục 6) chính xác hơn nên nói là **"đã
>   hoạt động bình thường trở lại"** — đây là kết quả Prometheus đo được (chỉ
>   số về bình thường), không phải AI tự tay sửa lỗi rồi báo đã sửa (HolmesGPT
>   chỉ đọc số liệu/log để phân tích, không có quyền thay đổi hệ thống).

**Các cơ chế chống tốn phí/chống spam đã có sẵn:**
- Mỗi sự cố (tính theo fingerprint) chỉ phân tích AI **1 lần** — tin nhắc lại
  mỗi giờ của Alertmanager không gọi lại Bedrock.
- Giới hạn số lần phân tích AI tối đa mỗi giờ (`AI_MAX_PER_HOUR`, mặc định 6).
- Chỉ phân tích AI cho mức độ cảnh báo đã chọn (`AI_SEVERITIES`, mặc định
  `critical,warning`).
- Phân tích tuần tự (1 luồng), không chạy song song nhiều sự cố cùng lúc.

---

## Tóm tắt 1 câu cho mỗi mục (dùng cho slide)

1. Hệ thống triển khai trên 1 EC2, tách 2 docker-compose (app / monitoring +
   AI), nối qua network Docker dùng chung, image pull từ Docker Hub.
2. 1 cổng vào duy nhất qua ALB + nginx; mỗi tool quản trị có tài khoản đăng
   nhập riêng, độc lập với MySQL để không "mù" theo khi DB gặp sự cố.
3. AI dùng Claude Sonnet 4.5 qua AWS Bedrock (region Singapore), cấu hình
   bằng API key trong `.env`.
4. Cảnh báo FIRING → Alertmanager gọi webhook sang `holmes-bridge` → AI đọc
   Prometheus/Loki, phân tích, gửi Telegram kèm nguyên nhân + hướng xử lý.
5. Cảnh báo tự hết → gửi tin xác nhận "đã hoạt động bình thường", kiểm tra
   chéo lại số liệu Prometheus, không tốn thêm phí AI cho bước xác nhận.
