# Shop Demo — Web thương mại điện tử + Monitoring

Web bán hàng demo, tách **3 tầng**: Frontend (tĩnh) · Backend (REST API) · Database (MySQL), đứng sau **Nginx**.
Đã gắn đủ các bước trong slide "Tool Monitor": **số liệu** (Exporter + Prometheus), **log** (Alloy + Loki), **dashboard** (Grafana), **cảnh báo** (Alertmanager → Telegram / mail) và **AI điều tra sự cố** (HolmesGPT + AWS Bedrock).

Toàn bộ chạy trên **1 EC2** bằng **2 file docker compose**, image kéo từ Docker Hub (`thuanperry/ecommerce-*`).

> **Không nhớ port/link?** Xem bảng tra cứu: **[TRA-CUU-NHANH.md](TRA-CUU-NHANH.md)**.

Mục lục:

1. [Chạy trên EC2 (người mới đọc mục này)](#1-chạy-trên-ec2-người-mới-đọc-mục-này)
2. [Cổng (port)](#2-cổng-port)
3. [Biến môi trường (.env.app, .env.monitoring)](#3-biến-môi-trường-envapp-envmonitoring)
4. [Monitoring giải thích đơn giản](#4-monitoring-giải-thích-đơn-giản)
5. [Các tài khoản (user) MySQL — vì sao có 3 user](#5-các-tài-khoản-user-mysql--vì-sao-có-3-user)
6. [Kiểm tra hệ thống & kịch bản demo sự cố](#6-kiểm-tra-hệ-thống--kịch-bản-demo-sự-cố)
7. [Nhật ký thay đổi: lỗi phát hiện → sửa thành gì → vì sao](#7-nhật-ký-thay-đổi-lỗi-phát-hiện--sửa-thành-gì--vì-sao)
8. [Lưu ý quan trọng & lỗi thường gặp](#8-lưu-ý-quan-trọng--lỗi-thường-gặp)
9. [Cấu trúc thư mục](#9-cấu-trúc-thư-mục)
10. [API](#10-api)
11. [Đã làm / chưa làm](#11-đã-làm--chưa-làm)
12. [Triển khai trên EC2 (chi tiết)](#12-triển-khai-trên-ec2-chi-tiết)

---

## 1. Chạy trên EC2 (người mới đọc mục này)

Hệ thống gồm **2 file docker compose**, chạy trên cùng 1 EC2, nối nhau qua network Docker dùng chung `ecommerce-shared` (mục 12.1):

| File | Chứa gì | File biến (mẫu trên git) |
| ---- | ------- | ------------------------ |
| `docker-compose.app.yml` | **Web**: db, backend, frontend, nginx, nginx-exporter, mysqld-exporter, node-exporter, alloy | `.env.app` (`.env.app.example`) |
| `docker-compose.monitoring.yml` | **Giám sát**: prometheus, alertmanager, loki, grafana. Profile `ai`: holmes, holmes-bridge. Profile `mail-test`: mailpit | `.env.monitoring` (`.env.monitoring.example`) |

Mọi config đã đóng gói sẵn trong image, EC2 chỉ cần 2 file compose + 2 file `.env`. Lần đầu dựng EC2 xem **mục 12.3**. Mỗi lần SSH/SSM vào EC2:

```bash
sudo -i
cd /opt/ecommerce
APP="docker compose -f docker-compose.app.yml --env-file .env.app"
MON="docker compose -f docker-compose.monitoring.yml --env-file .env.monitoring"
```

Hai biến `$APP` và `$MON` được dùng trong cả README này.

| Việc | Lệnh |
| ---- | ---- |
| Bật cả hệ thống | `$APP up -d && $MON up -d` |
| Bật thêm AI (cần khoá Bedrock, mục 7.19–7.20) | `$MON --profile ai up -d holmes-bridge` |
| Cập nhật image mới từ Docker Hub | `$APP pull && $APP up -d` · `$MON pull && $MON up -d` |
| Xem trạng thái | `docker ps` |
| Xem log 1 container | `docker logs -f ecommerce-backend` (đổi tên container) |
| Sửa `.env.*` xong | `$APP up -d --force-recreate <service>` hoặc `$MON ...` (`restart` **không** đọc lại `.env`) |
| Tắt (giữ dữ liệu) / xoá sạch dữ liệu | `$MON down && $APP down` · thêm `-v` để xoá cả volume |

Sau khi bật khoảng 1 phút, mở trên trình duyệt (`<web>` = địa chỉ ALB hoặc IP public của EC2):

| Mở gì | Địa chỉ | Đăng nhập |
| ----- | ------- | --------- |
| Trang web | `<web>/` | Không cần |
| **Trang Admin** | `<web>/login.html` → `<web>/admin.html` | `ADMIN_USERNAME` / `ADMIN_PASSWORD` (`.env.app`). Mục **Monitoring** có link tới các tool bên dưới |
| **Grafana** (màn hình theo dõi chính) | `<web>/admin/grafana/` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` (`.env.monitoring`) |
| Prometheus | `<web>/admin/prometheus/` | `MONITORING_BASIC_AUTH_USER` / `..._PASSWORD` (`.env.app`) |
| Alertmanager | `<web>/admin/alertmanager/` | như Prometheus |
| Loki | Không có giao diện, xem log qua Grafana (dashboard **Shop Logs**) | |

Sửa code thì build + push lại image từ máy local: `.\scripts\push-images.ps1` (mục 12.3), rồi `pull` trên EC2.

---

## 2. Cổng (port)

| Port | Của ai | Mở ra đâu | Ghi chú |
| ---- | ------ | --------- | ------- |
| `80` | nginx (cửa vào web) | **Public** (Security Group / ALB) | Đổi được qua `HTTP_PORT` trong `.env.app`. Là port public **duy nhất** |
| `3001` | Grafana | Chỉ `127.0.0.1` của EC2 | Bình thường vào qua `<web>/admin/grafana/` |
| `9090` | Prometheus | Chỉ `127.0.0.1` của EC2 | Qua `<web>/admin/prometheus/` |
| `9093` | Alertmanager | Chỉ `127.0.0.1` của EC2 | Qua `<web>/admin/alertmanager/` |
| `3100` | Loki | Chỉ `127.0.0.1` của EC2 | Chỉ để gỡ lỗi (`LOKI_PORT`) |
| `8025` | mailpit (hộp thư test) | Chỉ `127.0.0.1` của EC2 | Chỉ có khi bật `--profile mail-test` |
| `3000`, `3306`, `8081`, `9113`, `9104`, `9100`, `12345`, `8080` (holmes-bridge) | backend, MySQL, stub_status, 3 exporter, Alloy, holmes-bridge | Không | Chỉ container nói chuyện với nhau trong mạng Docker |

Các port "chỉ `127.0.0.1`" muốn mở thẳng từ máy bạn thì dùng SSM port-forward (mục 12.3, bước 3).

---

## 3. Biến môi trường (.env.app, .env.monitoring)

File `.env.app` và `.env.monitoring` chứa mật khẩu nên **không bao giờ commit** (đã chặn trong `.gitignore`). Trên git chỉ có 2 file mẫu `.env.app.example`, `.env.monitoring.example`.

**`.env.app`** (phần web):

| Biến | Dùng cho |
| ---- | -------- |
| `MYSQL_ROOT_PASSWORD` | Tài khoản **root** MySQL, chỉ để quản trị |
| `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` | Database `ecommerce` và tài khoản **riêng của web** (`ecommerce_app`) |
| `MYSQL_EXPORTER_PASSWORD` | Tài khoản **exporter** (chỉ đọc) cho mysqld-exporter |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Tài khoản trang admin (chỉ dùng lúc tạo lần đầu, sau đó đổi trong trang admin). Mật khẩu tối thiểu 12 ký tự |
| `SESSION_TTL_HOURS` | Phiên đăng nhập admin sống bao lâu (giờ) |
| `LOG_LEVEL` | Mức log backend: `debug`/`info`/`warn`/`error` |
| `HTTP_PORT` | Port public của nginx (EC2: `80`) |
| `LOKI_PUSH_URL` | Để trống (Alloy tự đẩy log sang `ecommerce-loki`) |
| `MONITORING_BASIC_AUTH_USER`, `MONITORING_BASIC_AUTH_PASSWORD` | Mật khẩu riêng để mở Prometheus / Alertmanager qua web. Tách khỏi login admin nên MySQL chết vẫn vào xem được |

**`.env.monitoring`** (phần giám sát):

| Biến | Dùng cho |
| ---- | -------- |
| `PROMETHEUS_RETENTION_TIME`, `PROMETHEUS_RETENTION_SIZE` | Prometheus giữ số liệu bao lâu / tối đa bao nhiêu ổ đĩa (mặc định `15d`, `5GB`) |
| `LOKI_RETENTION_PERIOD`, `LOKI_PORT` | Loki giữ log bao lâu (mặc định `168h` = 7 ngày), port gỡ lỗi |
| `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_PORT`, `GRAFANA_ROOT_URL` | Tài khoản và địa chỉ Grafana |
| `ALERTMANAGER_URL` | Địa chỉ Alertmanager hiện trong link của tin cảnh báo |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Kênh Telegram (mục 7.15) |
| `SMTP_*`, `ALERT_EMAIL_TO` | Kênh mail (mục 7.14). Phải bật ít nhất 1 trong 2 kênh |
| `BEDROCK_REGION`, `BEDROCK_MODEL_ID`, `AWS_BEARER_TOKEN_BEDROCK` (hoặc `AWS_ACCESS_KEY_ID`...) | HolmesGPT gọi Bedrock (mục 7.19) |
| `AI_WEBHOOK_URL`, `AI_SEVERITIES`, `AI_MAX_PER_HOUR` | Bật AI tự phân tích cảnh báo và nhắn Telegram (mục 7.20) |

Mật khẩu **chỉ dùng chữ và số**: ký tự `$` bị docker compose hiểu là biến, còn `'` làm hỏng câu lệnh SQL tạo user. Thiếu biến bắt buộc thì `docker compose up` dừng ngay và báo rõ, ví dụ `set MYSQL_ROOT_PASSWORD in .env.app`.

---

## 4. Monitoring giải thích đơn giản

### 4.1. Bức tranh chung

Monitoring (giám sát) = **luôn biết hệ thống đang khoẻ hay ốm, và ốm ở đâu**, mà không phải đợi khách báo lỗi.

Hệ thống giám sát ở đây có **2 nhánh song song**, trả lời 2 câu hỏi khác nhau:

| Nhánh | Lưu gì | Trả lời câu hỏi | Công cụ |
| ----- | ------ | --------------- | ------- |
| **Metrics (số liệu)** | Con số theo thời gian | "Có **bao nhiêu** lỗi? Chậm **mức nào**?" | Exporter → Prometheus |
| **Logs (bản ghi)** | Dòng chữ theo thời gian | "Lỗi đó **nội dung là gì**? **Vì sao** lỗi?" | Alloy → Loki |

Số liệu cho biết **có chuyện**, log cho biết **chuyện gì**. Thiếu một bên là mù một nửa: thấy 5xx tăng mà không biết tại sao, hoặc có log lỗi mà không biết nó chiếm bao nhiêu phần trăm traffic.

| Vai trò        | Ví von                | Làm gì |
| -------------- | --------------------- | ------ |
| **Exporter**   | Người phiên dịch      | Đứng cạnh một ứng dụng (Nginx, MySQL…), đọc tình trạng của nó và đọc ra thành **con số** theo định dạng Prometheus hiểu |
| **Prometheus** | Người đi ghi số điện nước | Cứ **15 giây** đi một vòng tới từng exporter, **tự kéo số về** (gọi là *pull / scrape*) và ghi vào sổ theo thời gian |
| **Alloy**      | Người đi gom sổ tay   | Đọc log mọi container qua docker socket, **phân tích** thành nhãn + field, rồi **đẩy** sang Loki |
| **Loki**       | Kho lưu sổ tay        | Lưu log theo thời gian, hỏi bằng **LogQL**. Chỉ index **nhãn**, không index nội dung → nhẹ và rẻ |

```
                  NHÁNH SỐ LIỆU (pull — Prometheus tự đi kéo)
  ỨNG DỤNG              PHIÊN DỊCH (exporter)            GHI SỔ
  ─────────             ─────────────────────            ──────
  Backend  ── tự đọc ra số ở /metrics ──────────────┐
  Nginx    ── stub_status ──► nginx-exporter ───────┼──► Prometheus (kéo mỗi 15s, lưu 15 ngày)
  MySQL    ── user exporter ─► mysqld-exporter ─────┘                    │
                                                                         │
                  NHÁNH LOG (push — Alloy tự đẩy sang)                   │
  Mọi container ── stdout/stderr ──► Docker json-file                    │
                                           │                            │
                                           ▼                            │
                                    Alloy (parse, gắn nhãn)             │
                                           │ push                       │
                                           ▼                            ▼
                                    Loki (lưu 7 ngày) ────────────► GRAFANA
                                                                   (vẽ cả 2 nhánh)
```

- **Backend không cần exporter riêng**. Code của mình tự "nói" ra số ở đường dẫn `/metrics` (thư viện `prom-client`).
- Nginx và MySQL là phần mềm có sẵn, không tự nói tiếng Prometheus, nên phải có **exporter** đứng cạnh phiên dịch.
- Prometheus **tự đi lấy**. Nếu một bên im lặng (chết, treo), Prometheus biết ngay vì lấy không được: chỉ số `up` = 0.

### 4.2. Hai loại con số cần biết

| Loại        | Giống như            | Đọc thế nào |
| ----------- | -------------------- | ----------- |
| **Counter** (bộ đếm) | Đồng hồ công-tơ điện: chỉ tăng, không giảm | Con số tuyệt đối ít ý nghĩa. Thứ cần xem là **tốc độ tăng**, dùng `rate(...[5m])`, ví dụ "bao nhiêu request mỗi giây trong 5 phút qua". Tên thường kết thúc bằng `_total` |
| **Gauge** (đồng hồ đo) | Nhiệt kế: lên xuống tự do | Đọc trực tiếp giá trị hiện tại, ví dụ "đang có 5 kết nối" |

Thêm một loại nữa là **Histogram**, dùng để đo **thời gian phản hồi**. Nó đếm xem bao nhiêu request nhanh dưới 0.1s, dưới 0.5s, v.v., từ đó tính được "95% khách được phục vụ trong bao lâu" (gọi là **p95**).

**Label (nhãn)** là phần trong ngoặc nhọn, ví dụ `http_requests_total{route="/api/orders", status_code="201"}`. Nhãn giúp chia nhỏ con số theo từng trang, từng mã lỗi.

### 4.3. PromQL — "câu hỏi" đặt cho Prometheus

PromQL là ngôn ngữ để hỏi Prometheus. Ví dụ đơn giản nhất: `up` → trả về 1 (đang sống) hoặc 0 (im lặng) cho từng mục tiêu.

**Cách gõ:**

1. Mở **`<web>/admin/prometheus/query`**.
2. Dán query vào ô trên cùng → bấm **Execute**.
3. Tab **Table** cho con số hiện tại. Tab **Graph** vẽ biểu đồ theo thời gian (chỉnh khung giờ ngay trên biểu đồ).
4. Trang này **không tự làm mới**: muốn số mới thì bấm Execute lại. Muốn tự cập nhật thì xem trên Grafana.

**Query mẫu để thử:**

| # | Muốn biết | Query |
| - | --------- | ----- |
| 1 | Các thành phần còn sống không (1 = sống, 0 = chết) | `up` |
| 2 | Tổng request backend theo trang và mã kết quả | `sum by (route, status_code) (http_requests_total)` |
| 3 | Request/giây theo trang, không tính request tự động (xem tab Graph) | `sum by (route) (rate(http_requests_total{route!~"/health\|/metrics"}[1m]))` |
| 4 | Tỉ lệ lỗi 5xx của khách trong 5 phút (0.05 = 5%) | `(sum(rate(http_requests_total{route!~"/health\|/metrics",status_code=~"5.."}[5m])) or vector(0)) / sum(rate(http_requests_total{route!~"/health\|/metrics"}[5m]))` |
| 5 | Thời gian phản hồi p95 (giây) | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))` |
| 6 | Số đơn đặt thành công trong 1 giờ qua | `sum(increase(http_requests_total{method="POST", route="/api/orders/", status_code="201"}[1h]))` |
| 7 | MySQL đăng nhập được không (1/0) | `mysql_up` |
| 8 | Số kết nối MySQL đang mở | `mysql_global_status_threads_connected` |
| 9 | % kết nối MySQL đã dùng | `mysql_global_status_threads_connected / mysql_global_variables_max_connections * 100` |
| 10 | MySQL chạy bao nhiêu query/giây | `rate(mysql_global_status_queries[1m])` |
| 11 | Nginx đang có bao nhiêu kết nối | `nginx_connections_active` |
| 12 | RAM backend đang dùng (MB) | `process_resident_memory_bytes{job="backend"} / 1024 / 1024` |

Ở query số 3, khi gõ vào Prometheus thì dùng `|` (không có dấu `\`). Dấu `\` trong bảng chỉ để hiển thị markdown.

**Đọc ký hiệu trong query:**

- `{...}`: lọc theo nhãn. Dùng `=` là bằng, `!=` là khác, `=~` là khớp mẫu, `!~` là không khớp mẫu. Ví dụ `{status_code=~"5.."}` = mọi mã 5xx.
- `[5m]`: lấy dữ liệu trong 5 phút gần nhất. Đơn vị: `s` giây, `m` phút, `h` giờ, `d` ngày.
- `rate(...)`: tốc độ tăng mỗi giây của một **counter**.
- `increase(...)`: counter tăng thêm bao nhiêu trong khoảng đó.
- `sum by (x)`: cộng gộp lại, giữ riêng theo nhãn `x`.

**Thử thấy số thay đổi:**

1. Chạy query 2 và ghi nhớ số.
2. Vào web bấm vài trang, đặt 1 đơn.
3. Đợi khoảng 15 giây rồi Execute lại → số tăng đúng theo những gì bạn vừa bấm.

**Thử lỗi:** chạy `docker stop ecommerce-db`, vào web vài lần.

- Query 7 thành `0`, query 4 tăng lên.
- Query 1 với `mysql` vẫn là `1`, vì exporter còn sống, chỉ MySQL chết.

Khôi phục bằng `docker start ecommerce-db`.

### 4.4. Từng tool — làm gì & chỉ số quan trọng

#### a) Backend (web app) — tự xuất số, file `backend/src/metrics.js`

**Làm gì:** mỗi request vào backend được đếm lại và bấm giờ. Ngoài ra thư viện tự đo sức khoẻ của chương trình Node.js (CPU, RAM…).

| Chỉ số | Loại | Ý nghĩa đời thường | Câu hỏi PromQL mẫu |
| ------ | ---- | ------------------ | ------------------ |
| `http_requests_total` | Counter | Tổng số request, chia theo `method`, `route`, `status_code` | Request/giây theo trang: `sum by (route) (rate(http_requests_total[5m]))` |
| ↳ lọc theo mã lỗi 5xx | | Bao nhiêu request **bị lỗi phía server** | `sum(rate(http_requests_total{status_code=~"5.."}[5m]))` |
| `http_request_duration_seconds` | Histogram | Web trả lời **nhanh hay chậm** | p95: `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))` |
| `process_cpu_seconds_total` | Counter | Backend ngốn bao nhiêu CPU | `rate(process_cpu_seconds_total{job="backend"}[5m])` (0.5 = nửa lõi CPU) |
| `process_resident_memory_bytes` | Gauge | Backend đang dùng bao nhiêu RAM | `process_resident_memory_bytes{job="backend"}` — tăng mãi không giảm → nghi rò rỉ bộ nhớ |
| `nodejs_eventloop_lag_seconds` | Gauge | Node.js có bị "kẹt" không. Bình thường gần 0 | Cao (> 0.1s) nghĩa là có đoạn code chạy nặng chặn mọi request khác |

`route` ghi theo **mẫu đường dẫn** (ví dụ `/api/orders/:id`), không ghi id thật (`/api/orders/42`). Nếu ghi id thật thì mỗi đơn hàng sinh ra một dòng số liệu mới, làm Prometheus phình to.

#### b) Nginx + nginx-exporter

**Nginx là gì:** "lễ tân", là cửa vào duy nhất của web. Khách vào đây rồi được chuyển tới frontend hoặc backend.
**Làm gì trong bài:** Nginx bật một trang trạng thái nhỏ (`stub_status`) ở port nội bộ 8081. `nginx-exporter` đọc trang đó và đổi ra số cho Prometheus.

| Chỉ số | Loại | Ý nghĩa đời thường |
| ------ | ---- | ------------------ |
| `nginx_up` | Gauge | Exporter có đọc được Nginx không (1/0) |
| `nginx_connections_active` | Gauge | Số khách **đang** kết nối ngay lúc này |
| `nginx_http_requests_total` | Counter | Tổng request Nginx đã nhận → `rate(...)` ra lưu lượng truy cập |
| `nginx_connections_accepted` / `_handled` | Counter | Nhận vào / xử lý được. Hai số **lệch nhau** nghĩa là Nginx đang phải **bỏ khách** vì quá tải |
| `nginx_connections_reading` / `_writing` / `_waiting` | Gauge | Đang đọc yêu cầu / đang trả lời / đang chờ (giữ kết nối) |

#### c) MySQL + mysqld-exporter

**MySQL là gì:** "kho sổ sách", nơi lưu sản phẩm và đơn hàng.
**Làm gì trong bài:** `mysqld-exporter` đăng nhập MySQL bằng tài khoản **exporter** chỉ có quyền đọc (xem mục 5), đọc các bảng trạng thái nội bộ của MySQL và đổi ra số.

| Chỉ số | Loại | Ý nghĩa đời thường |
| ------ | ---- | ------------------ |
| `mysql_up` | Gauge | Exporter có đăng nhập được MySQL không (1/0) |
| `mysql_global_status_threads_connected` | Gauge | Số kết nối **đang mở** tới MySQL |
| `mysql_global_variables_max_connections` | Gauge | Số kết nối **tối đa** cho phép. Gần chạm trần thì app sẽ không vào được DB |
| `mysql_global_status_queries` | Counter | Tổng câu lệnh SQL → `rate(...)` = số query mỗi giây |
| `mysql_global_status_slow_queries` | Counter | Số câu lệnh **chạy chậm**. Tăng đều là dấu hiệu cần tối ưu |
| `mysql_global_status_aborted_connects` | Counter | Số lần đăng nhập **thất bại**: sai mật khẩu, bị từ chối… |
| `mysql_global_status_uptime` | Gauge | MySQL đã chạy bao lâu. Tự nhiên về gần 0 nghĩa là vừa bị **khởi động lại** |

Ví dụ % kết nối đã dùng:
`mysql_global_status_threads_connected / mysql_global_variables_max_connections * 100`

#### d) Prometheus

**Làm gì:** đọc file `prometheus/prometheus.yml` để biết đi lấy số ở đâu (4 "job": `prometheus`, `backend`, `nginx`, `mysql`), kéo số mỗi 15 giây, lưu tối đa 15 ngày hoặc 5GB (cái nào tới trước). Dữ liệu nằm trong volume `prometheus-data` nên restart không mất.

| Chỉ số | Ý nghĩa |
| ------ | ------- |
| `up` | Quan trọng nhất: mỗi mục tiêu 1 = sống, 0 = im lặng |
| `scrape_duration_seconds` | Mất bao lâu để lấy số từ một mục tiêu |
| `prometheus_tsdb_head_series` | Prometheus đang theo dõi bao nhiêu "dòng số liệu". Tăng vọt bất thường thường do nhãn bị đặt sai |

**Trang web của Prometheus (`:9090`) chỉ là chỗ "lật sổ ra đọc"**, không phải chỗ ghi sổ. Việc ghi sổ chạy ngầm liên tục dù không ai mở trang. Trang này không tự làm mới, đóng tab là mất query. Nó dùng để kỹ sư tra cứu và gỡ lỗi, **không phải màn hình để theo dõi**. Theo dõi thì dùng Grafana.

### 4.5. Grafana — màn hình theo dõi tự cập nhật

**Grafana là gì:** "bảng điều khiển", giống màn hình trong phòng điều hành. Grafana **không tự đo và không lưu số liệu**. Nó chỉ **hỏi Prometheus** bằng các câu PromQL đã cài sẵn rồi **vẽ thành biểu đồ**, tự làm mới mỗi **15 giây**.

```
Prometheus (sổ ghi số)  ◄── hỏi PromQL mỗi 15s ──  Grafana  ──► bạn mở xem (biểu đồ tự nhảy)
```

**Đã cài sẵn hết, không cần bấm tay:**

- Grafana tự kết nối tới Prometheus khi khởi động (`grafana/provisioning/datasources/prometheus.yml`).
- Dashboard **Shop Overview** tự được nạp, và là **trang chủ** khi đăng nhập (`grafana/dashboards/shop-overview.json`).
- Toàn bộ query đã viết **một lần** trong file JSON này. Ai `git pull` về cũng có dashboard y hệt.

**Dashboard "Shop Overview" có 4 hàng.** Rê chuột vào biểu tượng ⓘ cạnh tên mỗi ô để xem giải thích.

| Hàng | Ô | Nhìn thấy gì / khi nào đáng lo |
| ---- | -- | ------------------------------ |
| **Tổng quan** | Backend, Nginx exporter, MySQL exporter, Prometheus | **UP** xanh / **DOWN** đỏ. Prometheus còn lấy được số từ thành phần đó không |
| | Nginx đọc được stub_status · MySQL đăng nhập được | Exporter còn sống nhưng **không đọc được ứng dụng** (vd: MySQL chết) → đỏ |
| | Đơn đặt thành công | Số đơn tạo thành công trong khung thời gian đang xem |
| | Tỉ lệ lỗi 5xx | % request **của khách** bị server làm hỏng (mã 500, 502…). Không tính `/health`, `/metrics` vì đó là request tự động luôn thành công, tính vào sẽ làm tỉ lệ thấp hơn thực tế. Xanh < 1%, vàng < 5%, **đỏ ≥ 5%** |
| **Web (backend)** | Lưu lượng theo trang · theo mã trạng thái | Khách đang vào trang nào. 4xx tăng = khách gửi sai, **5xx tăng = server lỗi** |
| | Thời gian phản hồi p50 / p95 | Vàng ≥ 0.5s, **đỏ ≥ 1s** |
| | Đơn/phút | Tốc độ bán hàng. Tụt về 0 giữa giờ cao điểm là bất thường |
| | CPU · RAM · event loop | Backend có quá tải hay rò rỉ bộ nhớ không |
| **Nginx** | Kết nối đang mở · request/giây · kết nối bị bỏ | "Bị bỏ" khác 0 = Nginx quá tải |
| **MySQL** | % kết nối đã dùng · query/giây · query chậm & đăng nhập lỗi · uptime | % kết nối vàng ≥ 70%, **đỏ ≥ 85%**. Uptime về 0 = MySQL vừa khởi động lại |

**Thao tác trên Grafana:**

- Góc trên bên phải: chọn khung thời gian (mặc định 30 phút gần nhất) và tốc độ làm mới (mặc định 15s).
- Kéo chuột trên một biểu đồ để phóng to đoạn đó. Bấm vào tên ô → **View** để xem to.

**Muốn thêm hoặc sửa ô biểu đồ:**

1. Sửa trên giao diện Grafana.
2. Bấm **Save** sẽ báo không lưu được. Đây là cố ý: dashboard được quản lý bằng file trong git để không ai sửa ngầm.
3. Bấm **Export → Export as JSON**, chép nội dung đè vào `grafana/dashboards/shop-overview.json`.
4. Commit file đó, chạy `.\scripts\push-images.ps1` để build lại image `ecommerce-grafana`, rồi trên EC2 `$MON pull grafana && $MON up -d grafana`.

**"Realtime" chậm bao nhiêu?** Prometheus lấy số mỗi 15 giây, Grafana làm mới mỗi 15 giây. Vì vậy biểu đồ chậm hơn thực tế tối đa khoảng **15–30 giây**. Đây là mức chuẩn của production. Các biểu đồ "mỗi giây" tính trung bình trên khoảng **1 phút** gần nhất, nên một sự kiện cần khoảng 1 phút để hiện rõ.

### 4.6. Log — Alloy + Loki (bước 4/8)

#### a) Vì sao cần log khi đã có số liệu

Số liệu trả lời *"có bao nhiêu"*, log trả lời *"là gì / vì sao"*. Ví dụ thật đã test:

| Nguồn | Nói gì khi tắt MySQL |
| ----- | -------------------- |
| Prometheus (số) | `mysql_up = 0`, tỉ lệ 5xx = 100% → **biết có sự cố, không biết nguyên nhân** |
| Loki (log) | `{"level":"error","msg":"unhandled_error","err_code":"ENOTFOUND","err_message":"getaddrinfo ENOTFOUND db"}` → **biết chính xác backend không phân giải được tên `db`** |

#### b) Ai làm gì

| Thành phần | Container riêng | Làm gì |
| ---------- | --------------- | ------ |
| **Alloy**  | `ecommerce-alloy` | Đọc log mọi container qua `/var/run/docker.sock` (cần quyền cao, xem #31), parse, gắn nhãn, **đẩy** sang Loki |
| **Loki**   | `ecommerce-loki`  | Lưu log, giữ 7 ngày (`LOKI_RETENTION_PERIOD`), trả lời **LogQL** |
| **Grafana**| `ecommerce-grafana` | Đã có sẵn — thêm datasource Loki và dashboard **Shop Logs** |

Alloy **đẩy** (push), khác Prometheus **kéo** (pull). Nhờ vậy Loki không cần biết có bao nhiêu container.

#### c) Nhãn (label) — điểm quan trọng nhất của Loki

Loki **không index nội dung log**, chỉ index **nhãn**. Mỗi tổ hợp nhãn khác nhau = một "stream" riêng. Đặt nhãn sai là cách nhanh nhất làm Loki sụp (gọi là *cardinality explosion*).

| Được đặt làm nhãn (hữu hạn giá trị) | **Không** được đặt làm nhãn (vô hạn giá trị) |
| ----------------------------------- | -------------------------------------------- |
| `service` (10 service) · `container` · `level` (3 mức) · `method` (vài loại) · `status` (~10 mã) | `path`, `duration_ms`, `request_time`, `ip`, `user_agent`, `order_id`… |

Những field "vô hạn" vẫn **lọc và tính được** — chỉ là bóc ra lúc truy vấn bằng `| json`, chứ không index trước:

```logql
{service="nginx"} | json | path="/api/orders/"          # lọc theo đường dẫn
{service="backend"} | json | duration_ms > 500          # chỉ request chậm > 500ms
```

Hệ thống hiện tại có **18 stream** — đã kiểm tra, không có cardinality explosion.

#### d) Mỗi service log kiểu khác nhau nên parse khác nhau

Toàn bộ nằm trong `alloy/config.remote.alloy`:

| Service | Định dạng log | Alloy làm gì |
| ------- | ------------- | ------------ |
| `nginx` | **JSON** (đã đổi trong `nginx/default.conf`) | `stage.json` bóc status/method/path/request_time. Suy ra `level`: 5xx→error, 4xx→warn, còn lại→info |
| `backend` | **JSON** (`backend/src/logger.js`) | Bóc level/route/status/duration_ms, lấy luôn mốc thời gian do app ghi |
| `frontend` | combined (mặc định) | Regex bóc method/status |
| `db` (MySQL) | `[System] [MY-010931] …` | Regex bóc mức trong ngoặc vuông → error/warn/info |
| `prometheus`, `grafana`, `loki`, 2 exporter | logfmt (`level=info …`) | `stage.logfmt` lấy key `level` |

Cuối pipeline có bước **chuẩn hoá**: `INFO`→`info`, `warning`→`warn`, `critical`/`fatal`→`error`, không parse được thì mặc định `info`. Nhờ vậy ô lọc "Mức độ" trên dashboard dùng được cho mọi service.

#### e) LogQL — "câu hỏi" đặt cho Loki

Cú pháp: **bắt buộc** bắt đầu bằng bộ chọn nhãn trong `{}`, rồi mới tới các toán tử lọc.

| # | Muốn biết | Query |
| - | --------- | ----- |
| 1 | Toàn bộ log của backend | `{service="backend"}` |
| 2 | Chỉ log lỗi của mọi service | `{job="docker", level="error"}` |
| 3 | Tìm chữ trong log (không cần parse) | `{service="backend"} \|= "ENOTFOUND"` |
| 4 | Loại bỏ dòng chứa chữ | `{service="nginx"} != "/health"` |
| 5 | Lọc theo field trong JSON | `{service="nginx"} \| json \| status=500` |
| 6 | Chỉ request chậm hơn 500ms | `{service="backend"} \| json \| duration_ms > 500` |
| 7 | Số dòng log mỗi giây theo service | `sum by (service) (rate({job="docker"}[1m]))` |
| 8 | Đếm số dòng lỗi trong 30 phút | `sum(count_over_time({job="docker", level="error"}[30m]))` |
| 9 | Top 10 đường dẫn bị gọi nhiều nhất | `topk(10, sum by (path) (count_over_time({service="nginx"} \| json \| __error__="" [30m])))` |
| 10 | p95 thời gian xử lý **tính từ log** | `quantile_over_time(0.95, {service="nginx"} \| json \| __error__="" \| unwrap request_time [1m]) by ()` |

Ký hiệu cần biết:

- `|=` chứa chữ · `!=` không chứa · `|~` khớp regex · `!~` không khớp regex
- `| json` bóc JSON thành field · `| logfmt` bóc dạng `key=value`
- `| __error__=""` **bỏ các dòng parse lỗi**. Thiếu nó thì dòng không phải JSON sẽ làm query báo lỗi.
- `| unwrap <field>` biến field số thành giá trị để tính p95, trung bình…
- `[1m]`, `[30m]` khoảng thời gian, giống PromQL.

Gõ thử: Grafana → **Explore** → chọn datasource **Loki** → dán query. Hoặc dùng dashboard **Shop Logs**.

#### f) Dashboard "Shop Logs"

4 hàng, 18 ô, tự làm mới 15s. Đầu trang có **3 ô lọc**: Service · Mức độ · Tìm chữ trong log.

| Hàng | Có gì |
| ---- | ----- |
| **Tổng quan log** | Tổng log/giây · số dòng ERROR · số dòng WARN · số service đang gửi log |
| **Luồng log theo nguồn** | Log/giây theo service · theo mức độ (đỏ nhô lên = bắt đầu có sự cố) · top container ồn nhất · phân bố theo service |
| **Phân tích log web** | Request/giây theo mã trạng thái **đếm từ access log** · p95/p50 thời gian xử lý từ log nginx · top đường dẫn · p95 `duration_ms` của backend |
| **Xem log chi tiết** | Ô log chỉ error+warn · ô log tất cả (lọc bằng 3 ô chọn ở đầu trang) |

Hàng "Phân tích log web" cố ý tính **lại** request/giây và p95 từ log, dù Prometheus đã có. Hai nguồn độc lập phải cho kết quả **xấp xỉ nhau** — lệch nhiều nghĩa là một bên đang mất dữ liệu.

#### g) Giới hạn đã đặt để log không làm đầy ổ đĩa

| Giới hạn | Giá trị | Ở đâu |
| -------- | ------- | ----- |
| Giữ log | 7 ngày (`LOKI_RETENTION_PERIOD`) | `loki/loki-config.yml` |
| Tốc độ nhận log | 8 MB/s (burst 16 MB) | 〃 |
| Một stream tối đa | 4 MB/s | 〃 |
| Độ dài 1 dòng | 256 KB (dài hơn thì cắt) | 〃 |
| Log file của container | 5 file × 10 MB mỗi container | `docker-compose.app.yml`, `docker-compose.monitoring.yml` |

Backend **không ghi log ra file**, chỉ ghi stdout/stderr — Docker thu lại và Alloy đọc từ đó. Ghi file trong container là phản pattern: mất khi container bị xoá, lại phải tự lo xoay vòng file.

Log `/health` và `/metrics` của backend để mức `debug` nên **mặc định không ghi**. Đây là request của máy (healthcheck + Prometheus scrape 15s/lần), luôn thành công, ghi vào chỉ nhấn chìm log thật của khách. Muốn xem thì đặt `LOG_LEVEL=debug`.

---

## 5. Các tài khoản (user) MySQL — vì sao có 3 user

**"User" ở đây không phải tài khoản của người dùng web**, cũng không phải tài khoản cho người cài đặt. Đây là **tài khoản đăng nhập vào MySQL** dành cho **các chương trình**. Giống một toà nhà có nhiều loại chìa khoá: mỗi người chỉ được cầm chìa mở đúng phòng mình cần.

| User MySQL       | Ai dùng          | Được làm gì | Vì sao tách riêng |
| ---------------- | ---------------- | ----------- | ----------------- |
| `root`           | Chỉ admin (con người, khi cần sửa chữa) | Mọi thứ, kể cả xoá toàn bộ database | Chìa vạn năng, không đưa cho chương trình nào |
| `ecommerce_app`  | Backend (web)    | Đọc/ghi **chỉ trong database `ecommerce`** | Nếu web bị hack, kẻ tấn công chỉ chạm được dữ liệu shop, không phá được cả MySQL |
| `exporter`       | mysqld-exporter  | **Chỉ đọc** số liệu trạng thái, tối đa 3 kết nối | Công cụ giám sát chỉ cần nhìn, không được sửa. Giới hạn kết nối để giám sát không làm nghẽn DB |

Đây là nguyên tắc **"quyền tối thiểu"** (least privilege), một yêu cầu cơ bản của production.

Các user được **tự tạo khi MySQL khởi tạo lần đầu**:

- `root`, `ecommerce_app` do image MySQL tạo từ biến `MYSQL_*` trong `.env`.
- `exporter` do script `mysql/init/01-exporter-user.sh` tạo.

**Chỉ chạy khi volume MySQL còn trống.** Nếu bạn đổi mật khẩu trong `.env` **sau khi** đã chạy lần đầu, MySQL **không** tự cập nhật. Xem mục 8.

---

## 6. Kiểm tra hệ thống & kịch bản demo sự cố

### 6.1. Kiểm tra nhanh

1. `docker ps` → 12 container `Up` (8 phần web + 4 phần giám sát; bật AI thì thêm `ecommerce-holmes-bridge`). `db`, `backend`, `prometheus`, `grafana` hiện thêm `(healthy)`. **`loki` và `alloy` không có `(healthy)`**, đúng như thiết kế (mục 8).
2. Mở Grafana `<web>/admin/grafana/` → hàng **Tổng quan** có 6 ô **UP** xanh.
3. Vào web, xem sản phẩm, đặt vài đơn → khoảng 1 phút sau các biểu đồ lưu lượng và đơn/phút nhảy lên.
4. Mở dashboard **Shop Logs** → ô "Tổng log/giây" > 0 và ô log dưới cùng đang chạy chữ. Nếu trống hẳn thì Alloy không gom được log.
5. Kiểm tra bảo mật: `<web>/metrics` phải ra **404**; `<web>/admin/prometheus/` khi chưa nhập mật khẩu phải ra **401**.
6. (Kỹ thuật) `<web>/admin/prometheus/targets` → 6 mục **UP**.
7. (Kỹ thuật, chạy trên EC2) Loki có nhận log chưa:

   ```bash
   curl http://127.0.0.1:3100/ready                                  # -> ready
   curl "http://127.0.0.1:3100/loki/api/v1/label/service/values"     # -> danh sách service
   ```

### 6.2. Kịch bản demo: giả lập sự cố và xem dashboard phản ứng

Mở Grafana ở một cửa sổ, chạy lệnh ở cửa sổ khác. Đợi khoảng **30–60 giây** sau mỗi lệnh để dashboard cập nhật.

| # | Giả lập | Lệnh | Dashboard sẽ thấy | Khôi phục |
| - | ------- | ---- | ----------------- | --------- |
| 1 | **Database chết** | `docker stop ecommerce-db` | **Shop Overview:** "MySQL đăng nhập được" → **DOWN** đỏ, "Tỉ lệ lỗi 5xx" **đỏ**, cột `500` xuất hiện. **Shop Logs:** ô "Dòng ERROR" nhảy lên, log hiện rõ nguyên nhân `getaddrinfo ENOTFOUND db` | `docker start ecommerce-db`. Web **tự kết nối lại**, không cần khởi động lại backend |
| 2 | **Backend chết** | `docker stop ecommerce-backend` | "Backend (web app)" → **DOWN** đỏ. Biểu đồ backend ngừng vẽ | `docker start ecommerce-backend` |
| 3 | **Mất giám sát** (exporter chết, app vẫn chạy) | `docker stop ecommerce-mysqld-exporter` | "MySQL exporter" **DOWN**, nhưng web vẫn chạy bình thường. Đây là cách phân biệt "app chết" với "công cụ đo bị chết" | `docker start ecommerce-mysqld-exporter` |
| 4 | **Khách gửi yêu cầu sai** (lỗi 4xx) | Mở `<web>/api/products/9999` vài lần (sản phẩm không tồn tại → 404) | Cột `404` xuất hiện ở "Request theo mã trạng thái", nhưng tỉ lệ **5xx vẫn xanh**. Lỗi phía khách không phải sự cố server | — |
| 5 | **Lưu lượng tăng đột biến** | Chạy vòng lặp gọi API (dưới đây) | Request/giây, query MySQL/giây, CPU backend tăng vọt | Bấm `Ctrl+C` |

Tạo tải cho kịch bản 5 — cách nhanh và thô (chạy trên EC2):

```bash
while true; do curl -s -o /dev/null http://localhost/api/products; done
```

Cách này chạy **tuần tự** nên chỉ đạt khoảng 30 request/giây — không đủ để ép hệ thống. Muốn tải thật thì dùng **k6** (`loadtest/load.js`), nó mô phỏng nhiều khách **đồng thời**:

```bash
# Chép thư mục loadtest/ của repo lên EC2 trước (ví dụ /opt/ecommerce/loadtest)
docker run --rm --network ecommerce-shared \
  -v "$PWD/loadtest:/scripts:ro" grafana/k6:0.54.0 run /scripts/load.js
```

Kịch bản tăng tải theo bậc 10 → 50 → 120 khách đồng thời trong khoảng 3 phút, trộn các loại request theo tỉ lệ giống thật (xem danh sách sản phẩm nhiều nhất, đặt hàng ít nhất).

Giá trị lớn nhất của k6 là nó **tự đếm số request ở phía khách** — thành nguồn thứ ba độc lập để đối chiếu với Prometheus và Loki. Nếu 3 con số khớp nhau thì cả hai đường ống giám sát đang đo đúng. Kết quả đo thực tế xem mục 7.7.

**Kịch bản dành riêng cho log** (mở dashboard **Shop Logs**):

| # | Giả lập | Lệnh | Dashboard sẽ thấy | Khôi phục |
| - | ------- | ---- | ----------------- | --------- |
| 6 | **Mất khả năng gom log** (Alloy chết, app vẫn chạy) | `docker stop ecommerce-alloy` | "Tổng log/giây" tụt về **0**, log ngừng chảy, nhưng web và Shop Overview vẫn bình thường. Đây là cách phân biệt "hệ thống im" với "mất công cụ thu log" | `docker start ecommerce-alloy` |
| 7 | **Kho log chết** (Loki chết) | `docker stop ecommerce-loki` | Mọi ô trên Shop Logs báo lỗi datasource. Alloy giữ log trong bộ đệm và **tự đẩy lại** khi Loki sống lại | `docker start ecommerce-loki` |
| 8 | **Tìm nguyên nhân bằng log** | `docker stop ecommerce-db` rồi vào web vài lần | Gõ `ENOTFOUND` vào ô "Tìm chữ trong log" → thấy đúng dòng lỗi kèm `route`, `err_code`, stack trace | `docker start ecommerce-db` |

Mỗi kịch bản đỏ quá 1 phút thì Alertmanager tự nhắn Telegram / mail (mục 7.14–7.15). Bật AI thì có thêm tin phân tích nguyên nhân và tin xác nhận đã hoạt động lại (mục 7.20).

---

## 7. Nhật ký thay đổi: lỗi phát hiện → sửa thành gì → vì sao

> Nhật ký ghi theo thời gian. Từ mục 7.1 đến 7.18 hệ thống còn chạy bằng 1 file `docker-compose.yml` (đã bỏ, nay dùng 2 file trên EC2 — mục 1). Lệnh `docker compose up/down/run ...` trong các mục đó là lệnh của thời điểm ấy; lệnh tương đương hiện nay xem mục 1.

### 7.1. Lỗi có sẵn trong code cũ (làm hệ thống không chạy được)

| # | Lỗi phát hiện | Hậu quả | Đã sửa thành | File |
| - | ------------- | ------- | ------------ | ---- |
| 1 | `db.js` vẫn là code **SQLite** (`better-sqlite3`), trong khi các route gọi `pool` của **MySQL**. `better-sqlite3` cũng không có trong `package.json` | Backend **crash ngay khi khởi động** | Viết lại `db.js` bằng **MySQL** (`mysql2`): pool kết nối, tự chờ MySQL sẵn sàng (retry 30 lần × 2s), tạo bảng, seed 6 sản phẩm | `backend/src/db.js` |
| 2 | Giá tiền nếu để kiểu `DECIMAL`, thư viện mysql2 mặc định trả về **chuỗi** `"1290000.00"` | Tính tổng đơn thành **nối chuỗi** thay vì cộng số | Bật `decimalNumbers: true`. Cột giá dùng `DECIMAL(12,2)` (chuẩn cho tiền, không sai số như số thực) | `backend/src/db.js` |
| 3 | `.env` đặt `MYSQL_USER=root` | Image MySQL 8 **từ chối khởi động** (không cho tạo lại user root) | Tạo user riêng `ecommerce_app` cho web (xem mục 5) | `.env`, `docker-compose.yml` |
| 4 | Backend trong Docker đọc `DB_HOST=localhost` | Trong container, `localhost` là **chính container backend**, không phải MySQL → không kết nối được | Compose đặt cố định `DB_HOST=db` (tên service MySQL) | `docker-compose.yml` |
| 5 | `server.js` không gọi `dotenv`, dù README cũ nói có | Chạy `npm start` local không đọc được `.env` | Nạp `.env` ở thư mục gốc project | `backend/src/server.js` |
| 6 | `.env.example` bị xoá, nội dung mẫu lại nằm trong `.env` thật | Người clone về không có file mẫu | Khôi phục `.env.example` (mật khẩu `change_me_...`). `.env` thật dùng mật khẩu ngẫu nhiên | `.env.example` |

### 7.2. Nâng lên chuẩn production

| # | Trước | Sau | Vì sao |
| - | ----- | --- | ------ |
| 7 | Web kết nối MySQL bằng `root`, mật khẩu `root` | 3 user tách quyền, mật khẩu mạnh trong `.env` | Quyền tối thiểu (mục 5) |
| 8 | `/metrics` mở công khai qua Nginx | Nginx trả **404** cho `/metrics`. Prometheus đọc trực tiếp `backend:3000` trong mạng nội bộ | Số liệu nội bộ (tên route, RAM…) không nên để người ngoài xem |
| 9 | Image dùng tag trôi (`nginx:alpine`, `mysql:8.0`) | Ghim version cụ thể (`nginx:1.27-alpine`, `mysql:8.0.40`, …) | Build hôm nay và tháng sau ra **giống hệt nhau**, không tự nhiên vỡ vì image mới |
| 10 | Dockerfile dùng `npm install`, chạy bằng user `root` | `npm ci` theo `package-lock.json`, chạy bằng user `node` | Cài đúng version đã khoá. Nếu app bị hack thì kẻ tấn công không có quyền root trong container |
| 11 | `.env` có thể bị copy vào image | Thêm `.env` vào `.dockerignore` | Mật khẩu không nằm trong image |
| 12 | Log container không giới hạn | Mỗi container tối đa 5 file × 10MB | Chạy lâu ngày không đầy ổ cứng |
| 13 | `docker stop` là tắt ngang | Backend bắt tín hiệu tắt, trả nốt request đang dở, đóng kết nối DB rồi mới thoát | Không đứt request/giao dịch giữa chừng |
| 14 | Thiếu biến trong `.env` vẫn chạy với giá trị rỗng | Compose báo lỗi và dừng ngay | Không bao giờ chạy DB với mật khẩu rỗng |
| 15 | Port web cố định `8080` | Đổi được qua `HTTP_PORT` | Máy nào trùng port thì tự đổi trong `.env` (mục 2) |
| 16 | — | Thêm `.gitattributes` ép file `.sh`/`.yml`/`.conf` dùng xuống dòng kiểu Linux | Clone trên Windows không làm hỏng script chạy trong container Linux |

### 7.3. Phần monitoring mới thêm (bước 3/8)

| Thêm gì | File |
| ------- | ---- |
| Backend tự xuất số bằng `prom-client` (đếm request, đo thời gian phản hồi, CPU/RAM Node.js) | `backend/src/metrics.js`, `backend/src/server.js` |
| Nginx bật `stub_status` ở port nội bộ 8081, chỉ cho IP mạng nội bộ | `nginx/default.conf` |
| Container `nginx-exporter` (`nginx/nginx-prometheus-exporter:1.3.0`) | `docker-compose.yml` |
| Script tạo user `exporter` chỉ đọc + container `mysqld-exporter` (`prom/mysqld-exporter:v0.15.1`) | `mysql/init/01-exporter-user.sh`, `docker-compose.yml` |
| Container `prometheus` (`prom/prometheus:v3.1.0`), cấu hình 4 job, lưu 15 ngày/5GB, chỉ mở trên `localhost` | `prometheus/prometheus.yml`, `docker-compose.yml` |

Đã kiểm tra thực tế: 4 target **UP**, `up` = 1, web đặt hàng được, `/metrics` công khai trả 404, port 8081 không truy cập được từ ngoài.

### 7.4. Grafana dashboard (bước 5/8)

| Thêm gì | File |
| ------- | ---- |
| Container `grafana` (`grafana/grafana:11.3.0`), chỉ mở trên `localhost:3001`, bắt buộc đăng nhập, tắt tự đăng ký, tắt gửi thống kê ra ngoài, dữ liệu trong volume `grafana-data` | `docker-compose.yml` |
| Tự kết nối Prometheus khi khởi động | `grafana/provisioning/datasources/prometheus.yml` |
| Tự nạp dashboard từ file, khoá sửa trên giao diện (dashboard quản lý bằng git) | `grafana/provisioning/dashboards/shop.yml` |
| Dashboard **Shop Overview**: 4 hàng, 24 ô, tự làm mới 15s, đặt làm trang chủ | `grafana/dashboards/shop-overview.json` |
| Thư mục rỗng cho provisioning cảnh báo (tránh log lỗi, dùng ở bước 6) | `grafana/provisioning/alerting/.gitkeep` |

Đã kiểm tra thực tế:

- Truy cập không đăng nhập bị chặn (401). Kết nối Prometheus OK. Dashboard là trang chủ, refresh 15s.
- **Cả 30 query trong dashboard đều trả về dữ liệu.**
- Giả lập tắt MySQL: web trả 500, `mysql_up` = 0, tỉ lệ lỗi 5xx lên ~87% (ô đỏ; đo lại sau bản sửa #17).
- Bật lại MySQL: web tự hồi phục về 200, `mysql_up` = 1.

### 7.5. Rà soát trước khi đưa lên GitHub

| # | Phát hiện | Đã sửa thành | File |
| - | --------- | ------------ | ---- |
| 17 | Ô "Tỉ lệ lỗi 5xx" tính cả `/health`, `/metrics` vào mẫu số. Đây là request tự động luôn thành công | Chỉ tính request của khách. Khi tắt MySQL, tỉ lệ đo được ~87% (sát thực tế) thay vì ~27% | `grafana/dashboards/shop-overview.json` |
| 18 | Ô "Kết nối bị bỏ" tô đỏ toàn nền dù giá trị = 0. Lúc demo dễ hiểu nhầm là có sự cố | Chỉ vẽ vạch ngưỡng | `grafana/dashboards/shop-overview.json` |
| 19 | Backend gửi header `X-Powered-By: Express`, nginx ghi rõ version trong header/trang lỗi | Tắt cả hai (`app.disable('x-powered-by')`, `server_tokens off`) | `backend/src/server.js`, `nginx/default.conf` |
| 20 | Nginx khởi động trước khi backend sẵn sàng | Nginx đợi backend **healthy** mới bật | `docker-compose.yml` |
| 21 | `.gitignore` còn dòng của SQLite (thừa), và chỉ chặn đúng file `.env` | Bỏ dòng thừa. Chặn mọi `.env.*` trừ `.env.example` | `.gitignore` |
| 22 | Script tạo user `exporter` có lệnh `FLUSH PRIVILEGES` thừa (`CREATE USER`/`GRANT` đã tự có hiệu lực) | Bỏ | `mysql/init/01-exporter-user.sh` |
| 23 | README ghi chạy backend local là "mở web ở :3000", thực tế chỉ có API | Sửa lại cho đúng | `README.md` |

**Về commit `f6b9e3f` trên GitHub** ("Fix db.js back to MySQL…"): commit này cũng chuyển `db.js` sang MySQL, song song với bài này. Khi gộp, **giữ bản `db.js` của bài này** vì:

- Bản kia tự dùng `root`/`root` khi thiếu biến môi trường. Bản này không có giá trị mặc định cho user và mật khẩu, nên không bao giờ âm thầm chạy bằng root.
- Bản kia tự chạy `CREATE DATABASE` (cần quyền cao). Bản này để MySQL container tạo database, app chỉ có quyền trong database của nó.

Phần chú thích commit đó thêm vào `docker-compose.yml` và `nginx/default.conf` đã được thể hiện tương đương trong bản hiện tại.

### 7.6. Tầng log: Alloy + Loki (bước 4/8)

**Thêm mới:**

| Thêm gì | File |
| ------- | ---- |
| Container `loki` (`grafana/loki:3.7.6`) — single binary, TSDB schema v13, giữ 7 ngày, giới hạn tốc độ nhận log, chỉ mở `localhost:3100` | `loki/loki-config.yml`, `docker-compose.yml` |
| Container `alloy` (`grafana/alloy:v1.20.1`) — gom log qua docker socket (xem lưu ý bảo mật #31), parse riêng cho từng service, chuẩn hoá `level`, đẩy sang Loki | `alloy/config.alloy`, `docker-compose.yml` |
| Backend xuất **log JSON có cấu trúc**, 1 dòng/request, kèm `route`/`status`/`duration_ms`; `/health` + `/metrics` hạ xuống mức `debug` để không nhiễu | `backend/src/logger.js`, `backend/src/server.js`, `backend/src/db.js` |
| Nginx đổi access log sang **JSON** (`log_format json_access`) để parse được thành field | `nginx/default.conf` |
| Grafana thêm datasource **Loki** và dashboard **Shop Logs** (18 ô, 3 ô lọc, 4 hàng) | `grafana/provisioning/datasources/loki.yml`, `grafana/dashboards/shop-logs.json` |

**Lỗi phát hiện khi test và đã sửa:**

| # | Lỗi phát hiện | Hậu quả | Đã sửa thành | File |
| - | ------------- | ------- | ------------ | ---- |
| 24 | **Nginx nhớ IP cũ của backend.** Nginx phân giải tên `backend` một lần lúc khởi động. Mỗi lần `docker compose up -d --build` tạo lại container backend, nó nhận IP mới | **Toàn bộ API trả 502** cho tới khi restart nginx — vỡ mỗi lần deploy lại. Đã gặp thật: backend đổi sang `172.19.0.10` mà nginx vẫn gọi `172.19.0.5` | Dùng DNS nội bộ Docker `resolver 127.0.0.11 valid=10s` + đặt upstream vào **biến** trong `proxy_pass` → nginx phân giải lại theo từng request | `nginx/default.conf` |
| 25 | **Nhãn `route` sai với mọi request lỗi.** Express phục hồi `req.baseUrl` về rỗng trước khi lỗi tới error handler, nên lúc đọc nhãn thì baseUrl đã mất | Mọi lỗi 500 bị gắn `route="/"` → biểu đồ "lỗi 5xx theo trang" **vô dụng**, không biết trang nào vỡ. Ảnh hưởng cả Prometheus lẫn Loki | Đóng dấu `req.mountPath` khi request vào router; hàm `routeTemplate()` dùng chung cho metrics và log. Nay 500 hiện đúng `/api/products/`, `/api/products/:id`, `/api/orders/` | `backend/src/http-route.js`, `metrics.js`, `logger.js`, `server.js` |
| 26 | Access log nginx bị ghi **2 lần** (1 bản combined + 1 bản JSON), do `access_log` khai ở http context **cộng dồn** với bản mặc định | Loki lưu gấp đôi, mọi phép đếm từ log sai gấp 2 | Khai `access_log` **trong `server` block** để ghi đè bản mặc định | `nginx/default.conf` |
| 27 | Healthcheck của `loki`/`alloy` dùng `wget` | Cả 2 image là **distroless**, không có `sh`/`wget`/`curl` → lệnh luôn fail, container bị báo `unhealthy` oan và chặn `depends_on` | Bỏ healthcheck 2 service này, Alloy chỉ `depends_on: loki` (không chờ healthy) vì `loki.write` **tự retry** nên không mất log | `docker-compose.yml` |

**Đã kiểm tra thực tế:**

- Log của **9 service** chảy vào Loki, nhãn `level` chuẩn hoá đúng 3 mức (`error`/`warn`/`info`).
- **18 stream** tổng cộng — không có cardinality explosion. `/loki/api/v1/labels` xác nhận `path`, `duration_ms`, `route`, `request_time` **không** bị index thành nhãn.
- **15/15 query** của dashboard Shop Logs chạy được qua chính API của Grafana (`/api/ds/query`).
- Tắt MySQL: nginx log `status:500` kèm `request_time:3.845`, backend log `err_code:"ENOTFOUND"` — log chỉ ra **nguyên nhân**, số liệu chỉ ra **mức độ**.
- Tắt rồi bật lại MySQL: web tự hồi phục HTTP 200, log MySQL lúc khởi động lại được parse đúng mức `[System]`/`[Warning]`.
- Nhãn `route` trên Prometheus và trên Loki **khớp nhau** sau khi sửa lỗi #25.

### 7.7. Kiểm chứng bằng load test: 3 nguồn độc lập có khớp nhau không

Chạy `loadtest/load.js` (k6, tăng dần tới 120 khách đồng thời, 3 phút 5 giây). k6 đếm ở phía **khách**, Prometheus đếm ở phía **server**, Loki đếm theo **số dòng log**. Ba nguồn hoàn toàn độc lập:

| Hạng mục | k6 đếm | Đo được | Lệch |
| -------- | ------ | ------- | ---- |
| nginx: tổng request | 36.573 | 36.591 | +0.05% |
| backend `GET /api/products/` | 16.557 | 16.557 | **0%** |
| backend `GET /api/products/:id` | 9.113 | 9.113 | **0%** |
| backend `POST /api/orders` (201+409) | 3.646 | 3.646 | **0%** |
| backend: tổng request của khách | 29.316 | 29.316 | **0%** |
| **Loki: số dòng log backend** | 29.316 | 29.316 | **0%** |
| **Loki: số dòng log nginx** | 36.573 | 36.573 | **0%** |

Hai dòng cuối là điều đáng chú ý nhất: **đúng một dòng log cho mỗi request**, không thiếu, không nhân đôi — xác nhận bản sửa lỗi #26 (log nginx bị ghi 2 lần) đã có hiệu lực thật. Chênh 18 request ở dòng nginx là các lệnh `curl` kiểm tra thủ công của chính tôi trong lúc đo.

Đơn hàng: **123 đơn trả 201**, **3.523 đơn trả 409** (hết hàng). Tổng khớp đúng 3.646. Trả 409 khi cạn tồn kho là **hành vi đúng**, không phải lỗi — và đáng chú ý là nó **không** làm tỉ lệ 5xx đỏ, vì 409 là lỗi phía khách. Tổng 5xx trong cả bài test = **0**.

**Nhưng hệ thống KHÔNG hề quá tải**, dù đã đẩy tới 120 khách đồng thời:

| Chỉ số | Đỉnh cao đo được | Nhận xét |
| ------ | ---------------- | -------- |
| Request/giây | 292 | |
| p95 thời gian phản hồi | **0.029 s** | Ngưỡng vàng của dashboard là 0.5s — còn rất xa |
| p99 thời gian phản hồi | 0.073 s | |
| MySQL `threads_connected` | **6** / 151 | Pool backend tối đa 10, mới dùng tới 6 |
| MySQL query/giây | 374 | |
| MySQL `slow_queries` tăng thêm | **0** | Không có query nào chậm |
| CPU backend | 0.71 lõi (máy có 12 lõi) | |
| RAM backend | 114 MB | |
| nginx kết nối đang mở | 121 | |
| nginx kết nối **bị bỏ** | **0** | Không bỏ khách nào |

Lý do không quá tải: database chỉ có **6 sản phẩm**, mọi truy vấn đều là tra cứu theo khoá chính nên xong trong khoảng 1ms. Pool 10 kết nối quay vòng quá nhanh nên chưa bao giờ phải xếp hàng. Kịch bản k6 còn có `sleep(0.3)` giữa các lần gọi, nên 120 khách chỉ tạo ra ~200–290 request/giây.

Kết luận thật thà: bài test này **chứng minh được metric và log đo đúng** (lệch 0%), nhưng **chưa chứng minh được hệ thống hành xử thế nào khi thật sự quá tải**. Muốn thấy quá tải cần bỏ `sleep`, tăng số khách lên nhiều lần, hoặc tạo dữ liệu lớn hơn để truy vấn trở nên đắt. Ngoài ra để thấy **CPU/RAM của cả máy** (chứ không chỉ của tiến trình backend) thì phải thêm `node-exporter` — lúc làm bài test này Prometheus **chưa có** metric nào thuộc nhóm `node_*` hay `container_*` (node-exporter đã được thêm sau, ở mục 7.10).

### 7.8. Đã biết nhưng **chưa** xử lý (ngoài phạm vi monitoring)

| Vấn đề | Rủi ro | Hướng xử lý sau |
| ------ | ------ | --------------- |
| `POST /api/products` (thêm sản phẩm) **không cần đăng nhập** | Ai vào được web cũng thêm được sản phẩm | Thêm xác thực admin, hoặc chặn route này ở nginx khi lên production |
| Không giới hạn tần suất gọi API (rate limit) | Có thể bị spam đặt đơn | `limit_req` ở nginx |
| `cors()` cho phép mọi domain | Không cần thiết vì frontend và API cùng domain qua nginx | Bỏ hoặc giới hạn domain khi có domain thật |
| Chưa có HTTPS | Dữ liệu đi dạng rõ | **Đã chuẩn bị** chế độ HTTPS bằng Let's Encrypt cho EC2 (mục 7.10). Local vẫn là HTTP |

### 7.9. Rà soát lại tầng log trước khi merge (13/10/2026)

Soát lại commit "Add log pipeline: Alloy + Loki" theo 2 tiêu chí: **logic có đúng không** và **có đạt chuẩn production không**. Không có lỗi nào làm hệ thống sập. Tìm ra 6 chỗ cần sửa:

| # | Vấn đề phát hiện | Hậu quả | Đã sửa thành | File |
| - | ---------------- | ------- | ------------ | ---- |
| 28 | **Dòng `[notice]` của nginx bị gắn nhãn `level=error`.** Template của Alloy coi mọi dòng error log của nginx (có `[mức]` trong ngoặc vuông) là `error`, kể cả `[notice]` | Mỗi lần nginx khởi động in ra vài dòng `[notice] start worker process` → ô **"Dòng ERROR"** trên dashboard Shop Logs tăng vọt dù không có lỗi gì. Lúc test, 1 lần restart nginx tạo ra 10 "lỗi" giả | Ánh xạ đúng theo mức: `[notice]` → `info`, `[warn]` → `warn`, `[error]`/`[crit]`/`[alert]`/`[emerg]` → `error`. Đã test: restart nginx, toàn bộ dòng `[notice]` mang nhãn `info` | `alloy/config.alloy` |
| 29 | **Log backend không ghi được IP thật của khách.** Backend đứng sau nginx nên mọi kết nối đến backend đều từ container nginx. Express mặc định không đọc header `X-Forwarded-For` mà nginx gửi kèm | Mọi dòng log backend đều có `ip` là IP nội bộ của nginx (ví dụ `172.18.0.8`) → không biết khách nào gây lỗi, không truy được ai spam | Thêm `app.set('trust proxy', 1)`: tin **đúng 1 lớp proxy** (nginx). Express lấy IP mà nginx tự ghi vào cuối `X-Forwarded-For` và bỏ qua phần khách tự gửi lên, nên khách **không giả mạo** IP được. (Lúc đầu thử `'loopback, uniquelocal'` thì bị giả mạo được khi chạy trên Docker Desktop, vì IP của khách khi đó cũng là IP nội bộ — đã test và đổi sang `1`) | `backend/src/server.js` |
| 30 | **Log backend ghi cả query string.** Trường `path` dùng `req.originalUrl` (ví dụ `/api/orders?email=a@b.com`), trong khi nginx đã cố ý dùng `$uri` để **bỏ** query string | Hai nơi làm ngược nhau; nếu sau này có API nhận email/token qua query string thì dữ liệu nhạy cảm nằm trong Loki 7 ngày, ai xem được Grafana đều đọc được | Cắt bỏ phần sau dấu `?`: `req.originalUrl.split('?')[0]` — giờ log backend và log nginx cùng một quy tắc | `backend/src/logger.js` |
| 31 | **Comment sai về an toàn của docker socket.** Comment cũ nói mount `docker.sock:ro` thì Alloy "chỉ đọc được, không điều khiển Docker" | Hiểu sai rủi ro. `:ro` chỉ chặn ghi vào **file** socket, **không** chặn lệnh gửi **qua** socket: container có socket vẫn tạo/xoá container, mount ổ đĩa host được = **quyền root trên máy** | Sửa comment và README cho đúng. Giữ nguyên cách chạy vì chấp nhận được cho demo. Ghi chú hướng production: đặt một **docker-socket-proxy** ở giữa, chỉ mở API đọc (containers, events, logs) | `docker-compose.yml`, `README.md` |
| 32 | **Crash của backend bị cắt thành nhiều dòng log.** Khi có lỗi không ai bắt, Node in stack trace ra nhiều dòng thường (không phải JSON) | Loki lưu mỗi dòng của stack trace thành một bản ghi riêng, mức `info` → khó đọc, và lỗi crash **không** hiện trong ô "Dòng ERROR" | Bắt `uncaughtException` và `unhandledRejection`, ghi thành **một** dòng JSON mức `error` kèm `stack`, rồi thoát để Docker tự bật lại container (`restart: unless-stopped`). Riêng lỗi xảy ra **trước** khi logger được nạp (ví dụ thiếu thư viện lúc khởi động) thì vẫn là nhiều dòng — trường hợp này hiếm và nhìn `docker logs ecommerce-backend` là thấy ngay | `backend/src/server.js` |
| 33 | **Thời gian truy vấn log ghi cứng `168h`.** `max_query_lookback` không đi theo biến `LOKI_RETENTION_PERIOD` | Nếu tăng thời gian giữ log lên 30 ngày trong `.env` mà quên sửa dòng này, Grafana vẫn chỉ xem được 7 ngày gần nhất dù dữ liệu cũ vẫn còn trên ổ đĩa | Dùng chung biến: `max_query_lookback: ${LOKI_RETENTION_PERIOD}`. Đổi 1 chỗ trong `.env` là cả 2 cùng đổi | `loki/loki-config.yml` |

**Đã kiểm tra lại sau khi sửa** (chạy đủ 10 container):

- Restart nginx: toàn bộ dòng `[notice]` mang nhãn `level=info`, không còn `error` giả (#28).
- Gửi request kèm header giả `X-Forwarded-For: 6.6.6.6`: log backend vẫn ghi IP thật mà nginx thấy, **không** bị giả mạo (#29).
- Gọi `/api/orders/998?email=x`: log backend ghi `path: "/api/orders/998"`, không còn query string (#30).
- Cố ý ném lỗi không ai bắt trong backend: log ra **một** dòng JSON `level=error`, `msg=uncaught_exception`, có đủ `stack` (#32).
- Loki khởi động bình thường, `/config` cho thấy `max_query_lookback` = `retention_period` = 7 ngày (#33).
- Prometheus vẫn đủ 4 target `UP`; dashboard Shop Overview và Shop Logs chạy bình thường.
- Lưu ý khi chạy trên **Docker Desktop (Windows)**: IP trong log sẽ là IP cổng mạng nội bộ của Docker (dạng `172.x.x.1`), vì Docker Desktop đứng giữa máy bạn và container. Lên server Linux thật thì sẽ thấy IP thật của khách.

### 7.10. Thêm node-exporter (số đo máy chủ) và HTTPS bằng Let's Encrypt (14/10/2026)

Hai việc chuẩn bị cho lúc đưa lên EC2. Theo thoả thuận: **chưa** làm backup MySQL, tách máy monitoring, chạy nhiều bản (ở quy mô localhost chưa cần); **không** dùng CloudWatch.

| # | Thêm gì | Vì sao | Làm ra sao | File |
| - | ------- | ------ | ---------- | ---- |
| 34 | **node-exporter**: đo CPU, RAM, ổ đĩa, mạng của **cả máy chủ** | Trước đây chỉ đo từng ứng dụng (backend, nginx, MySQL). Nếu cả máy hết RAM hay đầy ổ đĩa thì MySQL, Loki, Prometheus chết theo mà dashboard không báo trước. EC2 tự có biểu đồ CPU, nhưng **không có RAM và ổ đĩa còn trống** nếu không cài thêm agent, nên đo từ bên trong máy là cách đơn giản nhất | Thêm service `node-exporter` (đọc ổ đĩa máy chủ ở chế độ **chỉ đọc**, không mở cổng ra ngoài). Prometheus thêm job `node` đọc `node-exporter:9100`. Dashboard Shop Overview thêm hàng **"Máy chủ (node-exporter)"**: 4 ô (còn sống, CPU %, RAM %, ổ đĩa %) và 3 biểu đồ theo thời gian; vàng từ 80%, đỏ từ 90% | `docker-compose.yml`, `prometheus/prometheus.yml`, `grafana/dashboards/shop-overview.json` |
| 35 | **HTTPS** bằng chứng chỉ miễn phí của **Let's Encrypt** (chế độ riêng cho EC2) | Lead yêu cầu sản phẩm trên EC2 phải có HTTPS. Không có HTTPS thì mật khẩu, email, đơn hàng đi trên mạng dạng chữ rõ, ai đứng giữa đường cũng đọc được; trình duyệt còn gắn nhãn "Not secure" | Thêm **chế độ HTTPS** tách riêng, local vẫn chạy HTTP như cũ (xem giải thích bên dưới) | `nginx/https.conf`, `nginx/snippets/*`, `docker-compose.https.yml`, `certbot/www/`, `.env.example` |
| 36 | **Tách cấu hình nginx** thành phần chung + 2 chế độ | Cấu hình HTTP và HTTPS dùng chung toàn bộ đường đi (API, `/health`, chặn `/metrics`, frontend). Chép 2 bản thì sau này sửa một bản quên bản kia | Phần chung đưa vào `nginx/snippets/` (`common-http.conf`: log JSON + stub_status; `app-locations.conf`: các đường đi; `acme.conf`: đường xác minh của Let's Encrypt). `default.conf` (HTTP) và `https.conf` (HTTPS) chỉ còn vài dòng `include` | `nginx/` |

**node-exporter — cần biết khi đọc số:**

- Trên **EC2** (Linux thật) nó đo đúng chính con EC2 đó.
- Trên **Docker Desktop (Windows)** Docker chạy trong một máy ảo Linux, nên node-exporter đo **máy ảo đó**, không phải Windows. Số trên dashboard sẽ khác Task Manager, đó là bình thường.
- RAM tính theo `MemAvailable` (RAM còn cấp được cho chương trình), không theo `MemFree`, vì Linux hay dùng RAM rảnh làm bộ nhớ đệm; tính theo `MemFree` thì lúc nào cũng thấy gần đầy.
- Ổ đĩa chỉ xem ổ gốc `/`, nơi chứa dữ liệu Docker (MySQL, Loki, Prometheus đều nằm ở đây).

**HTTPS — hoạt động thế nào (bức tranh lớn):**

```
Trình duyệt ──http://──►  nginx :80  ──► 301 chuyển sang https://   (trừ đường /.well-known/acme-challenge/)
Trình duyệt ──https://─►  nginx :443 ──► giải mã bằng chứng chỉ ──► frontend / backend như cũ

certbot ── xin / gia hạn chứng chỉ ──► Let's Encrypt
Let's Encrypt ── gọi http://<domain>/.well-known/acme-challenge/<mã> ──► nginx :80 trả file mã do certbot để sẵn
```

- **Let's Encrypt** cấp chứng chỉ miễn phí, nhưng phải chứng minh bạn giữ domain: nó gọi vào domain qua **cổng 80** để lấy một file mã. Vì vậy cổng 80 luôn phải mở, kể cả sau khi đã có HTTPS.
- Chứng chỉ chỉ sống **90 ngày**. Container `certbot` cứ 12 tiếng hỏi "sắp hết hạn chưa?", còn dưới 30 ngày thì tự gia hạn. nginx cứ 6 tiếng tự nạp lại để dùng chứng chỉ mới (không làm rớt kết nối).
- Chứng chỉ và khoá bí mật nằm trong **volume Docker `letsencrypt`**, không nằm trong thư mục code, nên không thể lỡ tay commit lên git.
- Chỉ cho phép TLS 1.2 và 1.3. Có thêm header **HSTS** ("từ giờ chỉ vào bằng HTTPS"), để ngắn 1 ngày lúc mới bật; chạy ổn vài tuần rồi mới tăng lên 1 năm trong `nginx/https.conf`.
- Backend không phải sửa gì: nginx giải mã xong mới chuyển vào trong, và đã báo `X-Forwarded-Proto: https` cho backend biết.

**Chạy local (máy bạn): không đổi gì.** `docker compose up -d --build` như cũ, vẫn là HTTP ở cổng `HTTP_PORT`. Không dùng `docker-compose.https.yml` ở local vì localhost không xin được chứng chỉ Let's Encrypt.

**Các bước bật HTTPS trên EC2 (làm 1 lần):**

1. Cần có **domain** (hỏi lead) và trỏ bản ghi DNS **A** của domain về **IP public** của EC2 (nên dùng Elastic IP để IP không đổi khi tắt/bật máy).
2. **Security group** của EC2 mở cổng **80** và **443** cho mọi nơi (`0.0.0.0/0`). Cổng 22 (SSH) chỉ mở cho IP của bạn. **Không** mở 3001, 9090, 3100, 12345.
3. Trong `.env` trên EC2: `HTTP_PORT=80`, điền `DOMAIN=` (ví dụ `shop.congty.com`) và `LETSENCRYPT_EMAIL=` (email nhận thư nếu gia hạn lỗi).
4. Bật ở chế độ HTTP trước (lúc này chưa có chứng chỉ nên chưa bật HTTPS được):
   ```bash
   docker compose up -d --build
   ```
5. Xin chứng chỉ lần đầu (lệnh này đọc `DOMAIN` và `LETSENCRYPT_EMAIL` từ `.env`):
   ```bash
   set -a; . ./.env; set +a
   docker compose -f docker-compose.yml -f docker-compose.https.yml run --rm --entrypoint certbot certbot \
     certonly --webroot -w /var/www/certbot --cert-name app \
     -d "$DOMAIN" --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email
   ```
   Thấy `Successfully received certificate` là xong. Muốn thử trước cho chắc thì thêm `--staging` (chứng chỉ thử, trình duyệt sẽ báo không tin cậy), thành công rồi chạy lại không có `--staging`. Let's Encrypt giới hạn số lần xin, nên đừng chạy lệnh thật nhiều lần liền.
6. Chuyển sang chế độ HTTPS (từ giờ luôn bật bằng lệnh này trên EC2):
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
   ```
7. Kiểm tra: mở `http://<domain>` phải tự nhảy sang `https://<domain>` có ổ khoá; `docker compose -f docker-compose.yml -f docker-compose.https.yml ps` thấy thêm container `ecommerce-certbot`.

Grafana và Prometheus **vẫn chỉ mở trên localhost** của EC2 như cũ. Muốn xem từ máy bạn thì dùng SSH tunnel: `ssh -L 3001:127.0.0.1:3001 ubuntu@<IP-EC2>` rồi mở `<web>/admin/grafana/`.

**Đã kiểm tra trên bản sao (cloud):**

- Chế độ HTTP (local): web và API trả 200, đường `/.well-known/acme-challenge/` trả đúng file, Prometheus đủ **5** target `UP` (thêm `node`).
- node-exporter: CPU, RAM, ổ đĩa đều ra số; 8 ô mới trên dashboard được Grafana nạp và truy vấn ra số.
- Chế độ HTTPS (dùng chứng chỉ **tự ký** thay cho chứng chỉ thật): `http://` trả **301** sang `https://` và giữ nguyên đường dẫn; đường xác minh vẫn đi được qua HTTP; qua HTTPS web, API, `/health` trả 200, `/metrics` vẫn 404; chạy HTTP/2, TLS 1.3; có header HSTS; vòng nạp lại của nginx và vòng gia hạn của certbot đều đang chạy.
- **Chưa** kiểm tra được bước xin chứng chỉ thật từ Let's Encrypt (bước 5), vì việc này cần domain thật trỏ về một máy có IP public. Lần đầu làm trên EC2 nên chạy với `--staging` trước.

### 7.11. Luật cảnh báo cho Prometheus (14/10/2026)

| # | Thêm gì | Vì sao | Làm ra sao | File |
| - | ------- | ------ | ---------- | ---- |
| 37 | **10 luật cảnh báo** chia 3 nhóm: có gì chết không, khách có bị lỗi hay chậm không, máy chủ còn đủ sức không | Trước đây phải **ngồi nhìn** dashboard mới biết có sự cố. Luật cảnh báo cho Prometheus **tự** kiểm tra mỗi 15 giây và bật đèn khi có chuyện | Thêm file luật, cho Prometheus đọc qua `rule_files`, mount thư mục `prometheus/rules` vào container | `prometheus/rules/alerts.yml`, `prometheus/prometheus.yml`, `docker-compose.yml` |

**Danh sách luật** (ngưỡng nào cũng sửa được trong `alerts.yml`):

| Nhóm | Luật | Khi nào bật | Chờ bao lâu | Mức |
| ---- | ---- | ----------- | ----------- | --- |
| Sống/chết | `TargetDown` | Prometheus gọi một target không được | 1 phút | critical |
| | `NginxDown` | nginx-exporter không đọc được nginx | 1 phút | critical |
| | `MySQLDown` | mysqld-exporter không đăng nhập được MySQL | 1 phút | critical |
| Ứng dụng | `High5xxRate` | Hơn 5% request bị lỗi 5xx (chỉ tính khi có trên 0,1 request/giây) | 1 phút | critical |
| | `HighLatencyP95` | 5% request chậm nhất mất hơn 1 giây | 1 phút | warning |
| | `MySQLTooManyConnections` | Dùng quá 80% số kết nối tối đa của MySQL | 1 phút | warning |
| Máy chủ | `HostHighCpu` | CPU trên 90% | 1 phút | warning |
| | `HostHighMemory` | RAM trên 90% | 1 phút | warning |
| | `HostDiskAlmostFull` | Ổ đĩa `/` trên 85% | 1 phút | warning |
| | `HostDiskFull` | Ổ đĩa `/` trên 95% | 1 phút | critical |

**Cần biết khi đọc:**

- Mỗi luật có 3 trạng thái: **inactive** (bình thường), **pending** (điều kiện đã đúng nhưng chưa đủ thời gian chờ), **firing** (đã đúng đủ lâu, đây là cảnh báo thật). Thời gian chờ (`for`) để không báo nhầm vì một cú tăng vọt vài giây.
- **critical** = hỏng thật, xử lý ngay. **warning** = sắp có chuyện, xem trong ngày.
- Mỗi cảnh báo có `summary` (chuyện gì) và `description` (nên kiểm tra gì trước), viết bằng tiếng Việt.
- Xem ở đâu: `<web>/admin/prometheus/alerts`, hoặc trong Grafana: **Alerting → Alert rules** (Grafana tự đọc luật từ Prometheus, chỉ xem, không sửa ở đó).
- **Gửi đi đâu?** Prometheus chỉ *phát hiện*. Việc nhắn đi do **Alertmanager** làm, đã thêm ở mục 7.14 (mail) và 7.15 (Telegram): chỉ cần thêm phần `alerting:` vào `prometheus.yml`, các luật này giữ nguyên.

**Đã kiểm tra trên bản sao (cloud):**

- `promtool check rules`: 10 luật đúng cú pháp; `promtool check config`: `prometheus.yml` hợp lệ.
- Chạy thử luật bằng dữ liệu giả (`promtool test rules`): `TargetDown`, `MySQLDown`, `High5xxRate` (10% lỗi), `HostHighMemory` (RAM 95%) đều bật đúng lúc và đúng nhãn; `HostDiskAlmostFull` không bật khi ổ đĩa mới 50%.
- Diễn tập thật: `docker stop ecommerce-db` → `MySQLDown` chuyển **firing** sau khoảng 70 giây; `docker start ecommerce-db` → tự hết cảnh báo.
- Grafana đọc được danh sách luật từ Prometheus.

**Tự diễn tập trên máy bạn:** `docker stop ecommerce-db`, mở `<web>/admin/prometheus/alerts`, đợi khoảng 1-2 phút sẽ thấy `MySQLDown` từ vàng (pending) sang đỏ (firing). Xong thì `docker start ecommerce-db`.

### 7.12. Giả lập quá tải: dồn traffic và ép CPU/RAM máy chủ (14/10/2026)

| # | Thêm gì | Vì sao | File |
| - | ------- | ------ | ---- |
| 38 | Kịch bản k6 **quá tải** `overload.js`: 300 khách ảo gọi liên tục không nghỉ, 30% là đặt hàng, giữ tải 16 phút (có thể rút ngắn bằng `-e HOLD=3m`, luật demo chỉ cần ~2 phút) | `load.js` (mục 7.7) quá nhẹ, không làm web "đau". Muốn xem dashboard và luật cảnh báo phản ứng thì cần tải nặng và **đủ lâu** (luật CPU, độ trễ chờ 10 phút) | `loadtest/overload.js` |

Có **2 kiểu** quá tải, kết quả khác nhau, nên làm cả hai:

**Kiểu 1: dồn traffic vào web (k6).** Giống ngày khuyến mãi khách vào ồ ạt.

```powershell
docker run --rm --network tool-monitoring_appnet -v "${PWD}/loadtest:/scripts:ro" grafana/k6:0.54.0 run /scripts/overload.js
```

Muốn nặng hơn hoặc ngắn hơn: thêm `-e VUS=500 -e HOLD=5m` ngay trước `grafana/k6:0.54.0`. Dừng giữa chừng: `Ctrl+C`.

**Kiểu 2: ép thẳng CPU và RAM của máy chủ (stress-ng).** Giống một chương trình khác trên cùng máy bị lỗi, ăn hết tài nguyên.

```powershell
docker run --rm --name stress alexeiled/stress-ng:0.12.05 --cpu 0 --vm 2 --vm-bytes 100% --vm-keep --timeout 20m
```

- `--cpu 0`: chạy hết **mọi** nhân CPU. `--vm 2 --vm-bytes 100%`: chiếm gần hết RAM còn trống. `--timeout 20m`: tự dừng sau 20 phút.
- Luật đang ở **chế độ demo** (mục 7.13): CPU 100% khoảng 2 phút là `HostHighCpu` đỏ, RAM đỏ sau khoảng 2-3 phút (cần thêm ~1 phút để stress-ng chiếm đủ RAM). Không cần chạy hết 20 phút; thấy đỏ rồi thì `Ctrl+C`.
- Dừng sớm: `Ctrl+C`, hoặc mở PowerShell khác chạy `docker stop stress`. Tắt là RAM và CPU trả lại ngay.
- Chỉ ảnh hưởng **máy ảo Docker**, không làm treo Windows, vì máy ảo bị giới hạn RAM sẵn. Nhưng trong lúc chạy máy có thể chậm; nên đóng bớt app nặng.

**Kết quả đo trên bản sao (cloud, máy ảo 4 nhân, 15,7 GB RAM):**

| | Kiểu 1: k6 300 khách | Kiểu 2: stress-ng |
| - | -------------------- | ----------------- |
| CPU máy chủ | 90-95% (riêng k6 cũng ăn CPU vì chạy cùng máy) | 100% |
| RAM máy chủ | ~10%, gần như **không đổi** | 93% sau khoảng 1 phút |
| Request/giây | ~800 | (không có traffic) |
| Thời gian phản hồi phía khách (k6 đo) | p95 = 0,53 giây | |
| Thời gian phản hồi phía backend (Prometheus đo) | p95 = 0,02-0,05 giây | |
| Luật cảnh báo | `HostHighCpu` pending (cần giữ đủ 10 phút mới firing) | `HostHighCpu`, `HostHighMemory` pending rồi firing nếu giữ đủ lâu |
| Container nào bị chết? | Không | Không |

**Rút ra kinh nghiệm:**

- **Traffic web gần như không làm tăng RAM.** Web này nhẹ (6 sản phẩm), nên quá tải traffic chỉ thấy ở CPU và thời gian phản hồi. Muốn thấy luật RAM chạy thì phải dùng kiểu 2.
- **Máy nhiều nhân thì khó đỏ CPU bằng traffic.** Backend Node.js chỉ dùng **1 nhân** (backend CPU tối đa khoảng 0,7 nhân trong test). Máy bạn có 16 nhân nên k6 có thể chỉ đẩy CPU máy chủ lên khoảng 20-40% (đoán, chưa đo trên máy bạn). Trên EC2 nhỏ (2 nhân) thì đỏ rất nhanh.
- **Backend đo mình nhanh, khách thấy chậm.** Backend chỉ tính từ lúc nó *bắt đầu* xử lý, không tính thời gian request *xếp hàng chờ*. Vì vậy `HighLatencyP95` (dựa trên số của backend) **không** bật dù khách chờ 0,5 giây. Đây là giới hạn đã biết; muốn đo đúng trải nghiệm khách thì phải lấy thời gian từ log nginx (`request_time` trong Loki).
- `http_req_failed` của k6 báo khoảng 30% "lỗi" nhưng `checks` 100%: đó là đơn hàng trả **409** (hết hàng) sau khi bán hết kho, là hành vi đúng chứ không phải lỗi máy chủ.

**Cách xem:** mở song song Grafana **Shop Overview** (`Last 15 minutes`, tự làm mới `5s`, kéo xuống hàng "Máy chủ") và `<web>/admin/prometheus/alerts`.

### 7.13. Luật cảnh báo chế độ demo + bảng cảnh báo tự cập nhật trên Grafana (14/10/2026)

| # | Sửa gì | Vì sao | Làm ra sao | File |
| - | ------ | ------ | ---------- | ---- |
| 39 | **Mọi luật đỏ sau khoảng 1-2 phút** | Bản đầu dùng số trung bình 5 phút và chờ 5-10 phút: chuẩn production nhưng test rất lâu (CPU cần ~15 phút mới đỏ) | Tất cả luật đổi sang số liệu **1 phút gần nhất** (`[1m]`) và chờ **1 phút** (`for: 1m`). Ghi chú trong `alerts.yml` cách nới ra khi lên production | `prometheus/rules/alerts.yml` |
| 40 | **Bảng "Luật đang bị phạm"** ở đầu dashboard Shop Overview | Trang `/alerts` của Prometheus **không tự làm mới**, phải F5 mới thấy thay đổi. Prometheus không có tuỳ chọn bật tự làm mới cho trang này | Prometheus tự tạo một series tên `ALERTS` cho mỗi luật đang pending/firing. Bảng mới hỏi `ALERTS{alertstate=~"pending\|firing"}`; dashboard đặt tự làm mới **5 giây**. Đỏ = FIRING, cam = PENDING; trống = mọi thứ bình thường | `grafana/dashboards/shop-overview.json` |

**Lưu ý khi lên production:** đặt `for: 1m` trên số 1 phút thì một cú tăng CPU ngắn (ví dụ lúc build image) cũng đủ báo. Báo quá nhiều thì người trực sẽ quen và bỏ qua, nên trước khi gắn Alertmanager gửi tin thật thì nới lại theo ghi chú ở đầu `alerts.yml`.

**Đã kiểm tra trên bản sao:** cùng lúc tắt MySQL và chạy stress-ng. `MySQLDown` đỏ sau ~1,5 phút, `HostHighCpu` đỏ sau ~2 phút, `HostHighMemory` đỏ sau ~2,7 phút. Bảng trên Grafana hiện đúng các luật đó và tự đổi PENDING → FIRING mà không cần F5.

### 7.14. Alertmanager: gửi mail cảnh báo theo mẫu (14/10/2026)

| # | Thêm gì | Vì sao | File |
| - | ------- | ------ | ---- |
| 41 | **Alertmanager** nhận cảnh báo FIRING từ Prometheus, gom nhóm, gửi **mail** | Trước đây phải mở Grafana mới biết có sự cố. Giờ sự cố tự báo tới hộp thư | `alertmanager/alertmanager.yml`, `alertmanager/entrypoint.sh`, `docker-compose.yml`, `prometheus/prometheus.yml` |
| 42 | **Mẫu mail** tiếng Việt, cố định | Mail nào cũng cùng một khuôn để đọc nhanh: tiêu đề cho biết sự cố hay đã khắc phục, mức độ, tên luật; thân mail là bảng: sự cố, trạng thái, giờ bắt đầu/kết thúc (giờ Việt Nam), nơi bị, nên làm gì | `alertmanager/templates/email.tmpl` |
| 43 | **mailpit**: hộp thư giả để test, không cần Gmail | Thử mail mà không phải dùng tài khoản thật; xem mail trên web <http://localhost:8025> | `docker-compose.yml` (profile `mail-test`) |

**Đường đi của một cảnh báo:**

```
Prometheus (luật FIRING) ──► Alertmanager :9093 ──► gom nhóm 10s ──► SMTP (Gmail / mailpit) ──► hộp thư
                                   │
                                   └─ sự cố hết ──► gửi thêm mail "ĐÃ KHẮC PHỤC"
```

**Mẫu mail:**

- Tiêu đề: `[SỰ CỐ][CRITICAL] MySQLDown - Shop Demo` hoặc `[ĐÃ KHẮC PHỤC][CRITICAL] MySQLDown - Shop Demo`. Nhiều nơi cùng bị thì thêm `(3 nơi)`.
- Đầu mail có dải màu: **đỏ** = critical, **cam** = warning, **xanh** = đã khắc phục.
- Thân mail, mỗi nơi bị một bảng: Sự cố · Trạng thái · Bắt đầu lúc · Kết thúc lúc (chỉ khi đã hết) · Nơi bị · Nên làm gì. Nội dung lấy từ `summary` và `description` của luật trong `prometheus/rules/alerts.yml`, nên sửa chữ ở đó là mail đổi theo.
- Đổi màu, đổi bố cục: sửa `alertmanager/templates/email.tmpl` rồi `docker restart ecommerce-alertmanager`.

**Nhịp gửi** (trong `alertmanager.yml`): gom các cảnh báo cùng tên luật vào **1 mail**; chờ 10 giây để gom; có thêm nơi bị thì gửi cập nhật sau 1 phút; sự cố chưa hết thì **nhắc lại mỗi 1 tiếng** (không gửi mỗi phút). Ổ đĩa đầy (`HostDiskFull`) đang bật thì không gửi thêm mail "sắp đầy"; MySQL chết thì không gửi thêm mail "nhiều kết nối".

**Cài đặt:** mail là **tuỳ chọn** (từ mục 7.15 có thêm Telegram; phải bật ít nhất 1 trong 2 kênh, nếu không Alertmanager báo lỗi và không chạy). Muốn dùng mail thì thêm các dòng sau vào `.env` (có mẫu trong `.env.example`).

*Cách A: test bằng hộp thư giả mailpit (không cần tài khoản, nên làm trước):*

```
SMTP_HOST=mailpit:1025
SMTP_FROM=canhbao@shopdemo.local
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_REQUIRE_TLS=false
ALERT_EMAIL_TO=ban@example.com
```

```powershell
docker compose --profile mail-test up -d
docker stop ecommerce-db      # khoảng 1,5 phút sau mail SỰ CỐ tới
docker start ecommerce-db     # khoảng 1 phút sau mail ĐÃ KHẮC PHỤC tới
```

Mở <http://localhost:8025> để xem mail.

*Cách B: gửi thật qua Gmail:*

1. Tài khoản Gmail dùng để **gửi** phải bật **Xác minh 2 bước**.
2. Vào <https://myaccount.google.com/apppasswords>, tạo một **App password** (16 ký tự). Đây **không phải** mật khẩu đăng nhập Gmail; Gmail không cho Alertmanager đăng nhập bằng mật khẩu thường.
3. Trong `.env`:
   ```
   SMTP_HOST=smtp.gmail.com:587
   SMTP_FROM=ban@gmail.com
   SMTP_USERNAME=ban@gmail.com
   SMTP_PASSWORD=<app password 16 ký tự, viết liền không dấu cách>
   SMTP_REQUIRE_TLS=true
   ALERT_EMAIL_TO=ban@gmail.com,lead@congty.com
   ```
4. `docker compose up -d` (hoặc `$MON up -d --force-recreate alertmanager` nếu đã chạy). Thử bằng `docker stop ecommerce-db`.

**Gỡ lỗi khi không thấy mail:**

- `docker logs ecommerce-alertmanager --tail 30`: dòng `Chưa đặt SMTP_... trong .env` = thiếu biến; `535 ... Username and Password not accepted` = sai App password; `dial tcp ... timeout` = mạng chặn cổng 587.
- `<web>/admin/alertmanager/`: giao diện Alertmanager, xem cảnh báo nó đang giữ. Không thấy gì ở đây mà trang /alerts của Prometheus đang đỏ = Prometheus chưa nối được Alertmanager (xem `<web>/admin/prometheus/targets`, job `alertmanager`).
- Mail Gmail có thể rơi vào mục **Spam** lần đầu.
- Tạm tắt mail khi bảo trì: trên `<web>/admin/alertmanager/` bấm **New Silence**, chọn luật và thời gian.

**Bảo mật:** mật khẩu mail chỉ nằm trong `.env` (không lên git) và một file tạm trong container; không nằm trong `alertmanager.yml`. `entrypoint.sh` điền các giá trị còn lại vào khuôn mỗi lần khởi động, vì Alertmanager không tự đọc biến môi trường.

**Đã kiểm tra trên bản sao (với mailpit):** `amtool check-config` hợp lệ (cả mẫu mail); Prometheus thấy Alertmanager (target `alertmanager` UP); tắt MySQL → mail `[SỰ CỐ][CRITICAL] MySQLDown` tới sau khoảng 80 giây, gửi đúng 2 người nhận, giờ hiển thị theo giờ Việt Nam; bật lại MySQL → mail `[ĐÃ KHẮC PHỤC]` tới sau khoảng 1 phút, có cả giờ kết thúc. **Chưa** thử gửi qua Gmail thật (cần App password của bạn).

### 7.15. Báo cảnh báo qua Telegram (14/10/2026)

| # | Thêm gì | Vì sao | File |
| - | ------- | ------ | ---- |
| 44 | Kênh **Telegram** cho Alertmanager, mẫu tin nhắn giống mẫu mail | Gửi mail qua Gmail cần cấp App password của tài khoản Gmail. Telegram chỉ cần một **bot** riêng, không dính tới tài khoản cá nhân nào; tin tới điện thoại ngay | `alertmanager/alertmanager.yml`, `alertmanager/templates/telegram.tmpl`, `alertmanager/entrypoint.sh`, `docker-compose.yml`, `.env.example` |
| 45 | **Bật/tắt từng kênh bằng `.env`** | Không phải ai cũng dùng cả mail lẫn Telegram. Có `TELEGRAM_BOT_TOKEN` thì gửi Telegram; có `SMTP_HOST` thì gửi mail; có cả hai thì gửi cả hai; không có kênh nào thì Alertmanager báo lỗi rõ ràng và không chạy | `alertmanager/entrypoint.sh` |

**Mẫu tin nhắn** (cùng nội dung với mail, Telegram không có màu nên dùng biểu tượng):

```
🔴 SỰ CỐ: MySQLDown
Mức độ: CRITICAL · 1 nơi đang bị
━━━━━━━━━━━━━━
Sự cố: mysqld-exporter không kết nối được MySQL
Trạng thái: ĐANG XẢY RA
Bắt đầu: 16:30:54 14/10/2026
Nơi bị: mysqld-exporter:9104 (job: mysql)
Nên làm gì: MySQL có thể đã chết, ... Kiểm tra: docker logs ecommerce-db.
Shop Demo · Alertmanager. Chưa hết sẽ nhắc lại mỗi 1 tiếng.
```

🔴 = critical, 🟠 = warning, ✅ = đã khắc phục (kèm giờ kết thúc). Sửa mẫu ở `alertmanager/templates/telegram.tmpl` rồi `docker restart ecommerce-alertmanager`.

**Cài đặt (làm 1 lần, khoảng 5 phút).** Đây là đúng các bước đã chạy thành công trên máy Windows (Docker Desktop) ngày 14/10/2026.

Bot Telegram **không gửi theo số điện thoại**, mà gửi vào một **chat ID** (một con số). Vì vậy cần 2 thứ: **token** của bot và **chat ID** của bạn.

1. **Tạo bot.** Trên Telegram (điện thoại hoặc máy tính), gõ vào ô **tìm kiếm** chữ `BotFather`, chọn **@BotFather** có dấu tích xanh, bấm **Start**. Gửi `/newbot`, đặt tên hiển thị (ví dụ `Shop Demo Cảnh Báo`), rồi đặt username kết thúc bằng `bot` (ví dụ `shopdemo_canhbao_bot`). BotFather trả về một **token** dạng `7123456789:AAH...` (thường dài 46 ký tự). Giữ kín token này như mật khẩu, **không** chụp màn hình hay dán lên chat. Lỡ lộ thì vào BotFather gõ `/revoke` để lấy token mới.
2. **Nhắn cho bot.** Bấm vào link bot mà BotFather vừa đưa, bấm **Start** rồi gửi thêm một tin bất kỳ (ví dụ `hi`). Bước này bắt buộc: bot không được phép nhắn trước cho người chưa từng nhắn nó.
3. **Lấy chat ID.** Mở trình duyệt, vào `https://api.telegram.org/bot<TOKEN>/getUpdates` (thay `<TOKEN>` bằng token, giữ chữ `bot` dính liền phía trước). Trong kết quả tìm đoạn `"chat":{"id":123456789,"first_name":"<tên bạn>",...,"type":"private"}`: con số sau `"id":` là chat ID. Mọi tin bạn nhắn cho bot đều có **cùng** chat ID này, nên nhiều dòng cũng không sao. Nếu trang chỉ hiện `{"ok":true,"result":[]}` thì quay lại bước 2 nhắn cho bot rồi tải lại trang.
   *Mẹo:* trang này mở ra được (`"ok":true`) nghĩa là token bạn vừa dán trên thanh địa chỉ là token **đúng**; hãy copy đúng phần đó (giữa `/bot` và `/getUpdates`) vào `.env`.
4. **Điền vào `.env`** (mỗi biến đúng **một** dòng, không dấu ngoặc kép, không dấu cách, không có chữ `bot` ở đầu token):
   ```
   TELEGRAM_BOT_TOKEN=7123456789:AAH...
   TELEGRAM_CHAT_ID=123456789
   ```
   Không dùng mail thì để trống `SMTP_HOST=`.
5. **Tạo lại Alertmanager để nó đọc `.env` mới:**
   ```powershell
   $MON up -d --force-recreate alertmanager
   ```
   ⚠️ **Mỗi lần sửa `.env` đều phải chạy lệnh này.** `docker restart` **không** đọc lại `.env.*`, container vẫn giữ token cũ (đây chính là lỗi đã gặp: `.env` đúng nhưng Telegram vẫn báo `Unauthorized`).
6. **Nếu vừa thêm Alertmanager lần đầu**, khởi động lại cả Prometheus để nó đọc phần `alerting` mới trong `prometheus.yml`, đợi khoảng 20 giây (ngay sau khi khởi động, Prometheus trả `Service Unavailable` là bình thường):
   ```powershell
   docker restart ecommerce-prometheus
   ```
7. **Thử thật:** `docker stop ecommerce-db` → khoảng 1 đến 2 phút sau tin 🔴 **MySQLDown** tới Telegram. `docker start ecommerce-db` → khoảng 1 phút sau tin ✅ **ĐÃ KHẮC PHỤC**.

*Gửi vào nhóm (cả team cùng thấy):* thêm bot vào nhóm Telegram, nhắn một tin trong nhóm, làm lại bước 3: chat ID của nhóm là số **âm** (ví dụ `-1001234567890`), điền số đó (cả dấu trừ) vào `TELEGRAM_CHAT_ID`, rồi làm lại bước 5.

**Không nhận được tin? Dò theo từng chặng** (Prometheus → Alertmanager → Telegram). Các lệnh dưới đây **không in token ra màn hình**, và cố ý không dùng dấu ngoặc kép bên trong vì PowerShell tự bỏ mất chúng.

| Bước | Lệnh (PowerShell, chạy trong thư mục dự án) | Kết quả đúng | Nếu sai thì |
| ---- | -------------------------------------------- | ------------ | ----------- |
| 1. Prometheus thấy lỗi chưa | Mở http://localhost:9090/alerts | Luật (ví dụ `MySQLDown`) màu đỏ, **FIRING** | Đợi đủ 1 đến 2 phút (luật có `for: 1m`) |
| 2. Prometheus biết Alertmanager chưa | `curl.exe -s http://localhost:9090/api/v1/alertmanagers` | Có `"url":"http://alertmanager:9093/api/v2/alerts"` | Ra `"activeAlertmanagers":[]` → Prometheus đang chạy cấu hình cũ: `docker restart ecommerce-prometheus`, đợi 20 giây |
| 3. Alertmanager nhận được cảnh báo chưa | `curl.exe -s http://localhost:9093/api/v2/alerts` | Có `MySQLDown` | Ra `[]` → quay lại bước 2 |
| 4. Telegram từ chối gì | `docker logs ecommerce-alertmanager --tail 30` | Dòng đầu có `Kênh Telegram: BẬT (chat_id ..., token dài 46 ký tự)`, không có `Notify for alerts failed` | `Unauthorized (401)` → **sai token** (xem bước 5, 6). `chat not found` → sai chat ID hoặc chưa bấm Start với bot. `can't parse entities` → mẫu tin có ký tự `<` `>` lạ. Timeout → mạng chặn `api.telegram.org` |
| 5. Token trong container dài bao nhiêu | `docker exec ecommerce-alertmanager sh -c 'wc -c /tmp/telegram_token; tr -d A-Za-z0-9:_- < /tmp/telegram_token \| od -c'` | Khoảng `46`, dòng sau chỉ có `0000000` | Ngắn hơn hoặc có `\r`, `"`, dấu cách → so với bước 6, rồi `$MON up -d --force-recreate alertmanager` |
| 6. Token trong `.env` dài bao nhiêu | `(Get-Content .env)[<số dòng - 1>].Length` (tìm số dòng bằng `Select-String -Path .env -Pattern TELEGRAM`) | 19 (`TELEGRAM_BOT_TOKEN=`) + 46 = **65** | Khác bước 5 → container chưa đọc `.env` mới: `$MON up -d --force-recreate alertmanager` |
| 7. Telegram chấp nhận token chưa | `docker exec ecommerce-alertmanager sh -c 'wget -qO- https://api.telegram.org/bot$(cat /tmp/telegram_token)/getMe 2>&1 \| head -c 300'` | `"ok":true` kèm tên bot | `401 Unauthorized` → token sai hoặc đã bị `/revoke`. Dòng `TLS certificate validation not implemented` chỉ là lời nhắc của wget, bỏ qua |

**Đã kiểm tra trên bản sao:** máy cloud bị chặn không ra được `api.telegram.org`, nên tôi dựng một máy chủ **Telegram giả** để bắt đúng yêu cầu Alertmanager gửi đi (biến `TELEGRAM_API_URL`, chỉ dùng để test, bình thường không cần đặt). Tắt MySQL → Alertmanager gọi `sendMessage` với đúng token, chat ID, `parse_mode=HTML` và nội dung như mẫu trên; bật lại → tin ✅ có giờ kết thúc. Tắt mysqld-exporter → tin `TargetDown` có chữ `<tên service đó>` được escape đúng, không làm hỏng tin. `amtool check-config` hợp lệ ở cả 3 cách bật (chỉ Telegram, chỉ mail, cả hai). **Đã chạy thật** trên máy Windows (Docker Desktop) ngày 14/10/2026: nhận được tin 🔴 MySQLDown trên Telegram sau khi tắt db.

### 7.16. Rà soát lại toàn bộ thay đổi (15/10/2026)

Rà lại mọi file đã thêm/sửa từ mục 7.10 đến 7.15, chạy lại các công cụ kiểm tra: `docker compose config` (cả chế độ HTTP và HTTPS), `promtool check config` + `promtool check rules` (10 luật hợp lệ), `amtool check-config`, kiểm tra JSON của dashboard. **Không có lỗi làm hỏng hệ thống.** Sửa thêm 2 chỗ nhỏ và cập nhật các ghi chú đã cũ:

| # | Sửa gì | Vì sao | File |
| - | ------ | ------ | ---- |
| 46 | `entrypoint.sh` tự **bỏ dấu cách, dấu ngoặc kép, ký tự `\r`** lỡ dán thừa vào `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Thừa 1 ký tự là Telegram báo `Unauthorized`, rất khó nhìn ra bằng mắt | `alertmanager/entrypoint.sh` |
| 47 | Log khởi động in thêm **độ dài token** (không in token): `Kênh Telegram: BẬT (chat_id ..., token dài 46 ký tự)` | Lỗi thật đã gặp ngày 14/10: `.env` có token đủ 46 ký tự nhưng container vẫn chạy token cũ 43 ký tự (chưa `--force-recreate`). Có số này trong log là so được ngay với `.env` | `alertmanager/entrypoint.sh` |
| 48 | Sửa các ghi chú đã cũ: "chưa có Alertmanager", "để gửi mail", "5 nơi lấy số" | Từ mục 7.14–7.15 đã có Alertmanager gửi Telegram/mail, và Prometheus lấy số ở **6** nơi (thêm node-exporter, alertmanager) | `prometheus/rules/alerts.yml`, `prometheus/prometheus.yml`, `TRA-CUU-NHANH.md`, mục 7.11 README |
| 49 | `TRA-CUU-NHANH.md`: thêm luồng **Prometheus → Alertmanager → Telegram** và lệnh "sửa `.env` xong thì `--force-recreate`" | Để bảng tra cứu khớp với hệ thống hiện tại | `TRA-CUU-NHANH.md` |
| 50 | Thêm `"5s"` vào `timepicker.refresh_intervals` của Shop Overview | Dashboard đặt `"refresh": "5s"` nhưng danh sách khoảng làm mới chỉ có 15s/30s/1m/5m. Grafana 11.3 thấy 5s không nằm trong danh sách nên **tắt luôn tự làm mới** (nút ghi "Auto refresh turned off"): bảng "Luật đang bị phạm" và mọi ô không cập nhật, phải F5. Đo bằng trình duyệt headless: trước khi sửa 0 lần gọi query trong 35 giây; sau khi sửa 91 lần, bảng tự hiện `TargetDown` PENDING → FIRING và tự biến mất khi bật lại exporter, không cần F5 | `grafana/dashboards/shop-overview.json` |

Đã kiểm tra #46–#47: chạy Alertmanager với token cố tình kẹp dấu cách, dấu ngoặc kép và `\r` → file token còn đúng 46 ký tự, `amtool check-config` hợp lệ; không đặt kênh nào → vẫn báo lỗi rõ ràng như cũ.

**Giới hạn còn lại (biết rõ, chưa làm):** nếu chính **Alertmanager hoặc Prometheus chết** thì không ai nhắn được Telegram để báo việc đó. Cách thường dùng ở production là một luật "luôn luôn kêu" (Watchdog) gửi tới một dịch vụ bên ngoài, dịch vụ đó báo động khi **không** còn nghe thấy nó nữa. Để sau khi lên EC2.

### 7.17. Đăng nhập và phân quyền trang quản trị (21/10/2026)

Chuẩn bị đưa web ra internet: trước đây ai biết đường dẫn cũng thêm/sửa/xoá được sản phẩm và xem được toàn bộ đơn hàng (tên + email khách).

| # | Sửa gì | Vì sao | File |
| - | ------ | ------ | ---- |
| 51 | **Tài khoản admin tự tạo** ở lần chạy đầu từ `ADMIN_USERNAME` / `ADMIN_PASSWORD` (`.env`, bắt buộc, tối thiểu 12 ký tự). Đã có admin thì bỏ qua | Không cần thao tác tay trong DB. Mật khẩu lưu dạng hash **scrypt** có salt riêng, không bao giờ lưu mật khẩu gốc | `backend/src/db.js`, `backend/src/auth/password.js`, `docker-compose.yml`, `.env.example` |
| 52 | **Phiên đăng nhập** bằng cookie `sid`: HttpOnly, SameSite=Strict, tự bật Secure khi chạy HTTPS, hết hạn sau 8 giờ (`SESSION_TTL_HOURS`). DB chỉ lưu SHA-256 của token | JavaScript trên trang không đọc được cookie (XSS không lấy cắp được); lộ bảng `sessions` cũng không dùng lại được phiên | `backend/src/auth/session.js`, bảng `users`, `sessions` |
| 53 | **Phân quyền API**: `POST/PUT/DELETE /api/products` và `GET /api/orders*` chỉ admin (401 / 403). Xem sản phẩm và **đặt hàng vẫn công khai** | Quyền tối thiểu: khách chỉ cần xem và mua | `backend/src/auth/middleware.js`, `routes/products.js`, `routes/orders.js` |
| 54 | **Chống CSRF**: request ghi/xoá có `Origin` khác tên miền hoặc `Sec-Fetch-Site: cross-site` → 403. Bỏ `cors()` (trước đây cho mọi trang web gọi API), gỡ luôn thư viện `cors` | Trang lạ không gửi được request thay admin đang đăng nhập | `backend/src/auth/http.js`, `backend/src/server.js`, `backend/package.json` |
| 55 | **Chống dò mật khẩu**: sai 5 lần / 15 phút (theo IP và theo tên đăng nhập) → khoá 15 phút (429). Một thông báo chung "Sai tên đăng nhập hoặc mật khẩu"; tên không tồn tại cũng tốn đúng thời gian như sai mật khẩu | Không dò được mật khẩu, không đoán được tài khoản nào tồn tại | `backend/src/auth/rate-limit.js`, `backend/src/routes/auth.js` |
| 56 | **nginx `auth_request`**: chưa đăng nhập thì `/admin.html`, `admin.js`, `admin.css`, `tools.js` trả **302 → /login.html**, file không được gửi đi. Trang admin gắn `Cache-Control: no-store` | Lớp chặn thứ 2 ngay ở cửa vào; đăng xuất rồi bấm Back không xem lại được | `nginx/snippets/app-locations.conf` (dùng chung HTTP + HTTPS) |
| 57 | **Trang đăng nhập** `login.html` (giao diện Tabler); trang admin có tên tài khoản, nút Đăng xuất, mục **Đổi mật khẩu** (đổi xong đăng xuất mọi máy khác); phiên hết hạn giữa chừng tự về trang đăng nhập | | `frontend/login.html`, `login.js`, `admin.html`, `admin.js` |
| 58 | **Navbar shop**: bỏ link vào thẳng `/admin.html`. Khách thấy **Đăng nhập**; admin đã đăng nhập thấy **Quản trị** + **Đăng xuất** | `/api/auth/me` luôn trả 200 nên không làm tăng ô 4xx trên dashboard | `frontend/index.html`, `app.js`, `styles.css` |
| 59 | `absolute_redirect off` cho chuyển hướng về trang đăng nhập | Lỗi thật gặp khi test: nginx ghép `Location: http://localhost/login.html` theo cổng **trong container** (80), trình duyệt vào bằng 8080 nên bị đẩy sang trang lỗi | `nginx/snippets/app-locations.conf` |

**Log:** ghi sự kiện `login_success`, `login_failed` (kèm lý do `wrong_password` / `unknown_user`), `login_locked`, `password_changed`, `admin_seeded`. Không ghi mật khẩu, không ghi tên người dùng gõ thử.

**Đã kiểm tra trên Docker Compose local:** 16 test đơn vị (`npm test`); 48 mục kiểm tra API qua nginx (chưa đăng nhập bị chặn đúng 401/302, đăng nhập đúng/sai, CSRF 403, CRUD, đổi mật khẩu đá máy khác ra, đăng xuất, khoá sau 5 lần sai, log không chứa mật khẩu); 15 mục giao diện bằng trình duyệt headless (navbar, chuyển hướng, đăng nhập, trang admin, đăng xuất, không lỗi JavaScript). Prometheus vẫn đủ 6 target UP, `/metrics` vẫn 404.

**Lưu ý vận hành:**
- Sửa `ADMIN_PASSWORD` trong `.env` **sau** lần tạo đầu không có tác dụng: đổi trong trang admin → **Đổi mật khẩu**.
- Quên mật khẩu: xoá tài khoản (`DELETE FROM users WHERE username='admin';` bằng user root của MySQL) rồi `docker restart ecommerce-backend` để tạo lại từ `.env`.
- Bộ đếm chống dò nằm trong bộ nhớ backend: lỡ bị khoá khi đang test thì `docker restart ecommerce-backend` là mở.
- Lên internet **bắt buộc dùng HTTPS** (`docker-compose.https.yml`): HTTP thường thì mật khẩu và cookie đi qua mạng dạng chữ thường.

### 7.18. Làm lại giao diện shop, trang đăng nhập và trang quản trị (21/10/2026)

| # | Sửa gì | File |
| - | ------ | ---- |
| 60 | **Shop** chuyển sang giao diện sáng kiểu cửa hàng: thanh thông báo, navbar dính trên cùng, hero 2 cột có ảnh, 4 thẻ ưu điểm dùng icon SVG thay emoji, thẻ sản phẩm ảnh vuông (nhãn chỉ hiện khi sắp hết / hết hàng), footer nhiều cột, giỏ hàng có icon trống. Font **Be Vietnam Pro** (thiết kế cho tiếng Việt) | `frontend/index.html`, `styles.css`, `app.js` |
| 61 | **Trang quản trị** (vẫn template Tabler): sidebar tối chia nhóm, nội dung sáng, thẻ thống kê có icon (thêm tổng trị giá tồn kho), bảng có ô tìm kiếm có icon và nút Sửa/Xoá dạng icon, hộp thoại 2 cột có khung xem trước ảnh, mục Monitoring hiện cả địa chỉ của tool. Icon là SVG bộ Tabler Icons (MIT) vẽ sẵn trong `admin.js`, không tải thêm gì | `frontend/admin.html`, `admin.css`, `admin.js`, `tools.js` |
| 62 | **Trang đăng nhập** chia đôi: bên trái giới thiệu, bên phải form; nút Hiện/Ẩn mật khẩu. CSS để ngay trong trang vì `admin.css` bị nginx chặn khi chưa đăng nhập | `frontend/login.html`, `login.js` |
| 63 | Điện thoại (390px): không trang nào bị tràn ngang. Bảng sản phẩm tự cuộn ngang trong khung (`.page-wrapper { min-width: 0 }`, `.table-responsive { position: relative }`); navbar shop ẩn bớt chữ | `admin.css`, `styles.css` |

**Đã kiểm tra:** 21 bước chức năng bằng trình duyệt headless (thêm giỏ, tăng/xoá, đăng nhập, thêm/tìm/sửa/xoá sản phẩm qua giao diện, Monitoring, đăng xuất), không lỗi JavaScript; 16 + 6 test đơn vị; file admin vẫn 302 khi chưa đăng nhập, `/metrics` vẫn 404.

### 7.19. HolmesGPT + AWS Bedrock: AI điều tra sự cố (bước 7–8)

```
Bạn: $MON --profile ai run --rm holmes ask "..."
        │
        ▼
HolmesGPT (container, mạng appnet) ──PromQL──► Prometheus :9090
        │                          ──LogQL───► Loki :3100
        │                          ──API─────► Alertmanager :9093
        │  gửi câu hỏi + số liệu/log đã lấy
        ▼
AWS Bedrock (model Claude) ── suy luận ──► trả lời: nguyên nhân + nên làm gì
```

HolmesGPT tự quyết định cần tra gì, tự chạy PromQL/LogQL, rồi gửi kết quả cho model trên Bedrock suy luận. Bedrock không truy cập thẳng vào hệ thống, chỉ nhận đoạn chữ HolmesGPT gửi lên.

| Thêm gì | File |
| ------- | ---- |
| Image HolmesGPT `0.45.0` build từ PyPI (ghim phiên bản), chạy bằng user thường | `holmes/Dockerfile` |
| Script khởi động: bỏ biến AWS trống / ký tự thừa, **dừng nếu chưa có khoá** (không gọi Bedrock), in cách đăng nhập đang dùng (không in khoá) | `holmes/entrypoint.sh` |
| Toolset Prometheus, Loki, địa chỉ Alertmanager, `max_steps: 15` (giới hạn số lượt gọi Bedrock cho 1 câu hỏi) | `holmes/config.yaml` |
| Service `holmes`, **profile `ai`**: `$MON up -d` không bật nó, chỉ chạy khi gọi | `docker-compose.monitoring.yml` |
| Biến Bedrock | `.env.monitoring.example` |

**Biến trong `.env.monitoring`:**

| Biến | Ý nghĩa |
| ---- | ------- |
| `BEDROCK_REGION` | Region có model đã bật quyền, ví dụ `ap-southeast-1` |
| `BEDROCK_MODEL_ID` | Model ID hoặc inference profile ID, **không** ghi tiền tố `bedrock/` (compose tự thêm) |
| `AWS_BEARER_TOKEN_BEDROCK` | Cách 1: Bedrock API key |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Cách 2: access key IAM (session token chỉ khi là khoá tạm) |

Điền **một** trong hai cách. Có cả hai thì API key được ưu tiên trong thông báo khởi động; nên để trống cách không dùng.

**Cách chạy (trên EC2, đã khai báo `$MON` như mục 1):**

```bash
# Lần đầu, hoặc khi có image mới trên Docker Hub
$MON --profile ai pull holmes

# Hỏi tự do
$MON --profile ai run --rm holmes ask "Vì sao web đang trả lỗi 5xx?" --no-interactive

# Điều tra toàn bộ cảnh báo đang FIRING trên Alertmanager
$MON --profile ai run --rm holmes investigate alertmanager

# Chỉ 1 cảnh báo
$MON --profile ai run --rm holmes investigate alertmanager --alertmanager-alertname MySQLDown

# Kiểm tra kết nối toolset (KHÔNG gọi Bedrock, không tốn tiền)
$MON --profile ai run --rm --no-deps --entrypoint holmes holmes toolset list
```

Diễn tập gợi ý: `docker stop ecommerce-db`, đợi khoảng 1,5 phút để `MySQLDown` FIRING, chạy `investigate alertmanager --alertmanager-alertname MySQLDown`, xong thì `docker start ecommerce-db`. Tin Telegram vẫn gửi như bình thường.

**Đã kiểm tra (không gọi Bedrock):** build image thành công; `toolset list` báo `prometheus/metrics` và `grafana/loki` ✅ (kết nối được qua mạng nội bộ); chưa điền khoá thì container dừng với thông báo rõ ràng; `$MON up -d` không bật service `holmes`. **Chưa kiểm tra** lượt gọi Bedrock thật (cần khoá và tốn phí).

**Lưu ý:**
- **Chi phí:** Bedrock tính theo token. Một câu hỏi thường gồm nhiều lượt gọi (tối đa `max_steps`). Nên đặt AWS Budgets cảnh báo chi phí.
- **Dữ liệu ra ngoài:** số liệu và log (có IP, user-agent của khách trong log nginx) được gửi lên Bedrock. Bedrock không dùng dữ liệu này để huấn luyện model, nhưng dữ liệu vẫn rời khỏi máy.
- **Quyền IAM tối thiểu:** chỉ `bedrock:InvokeModel` và `bedrock:InvokeModelWithResponseStream` trên đúng inference profile + foundation model đang dùng. Không cần `AmazonBedrockFullAccess`.
- **Lỗi hay gặp:** `AccessDeniedException` = chưa bật quyền dùng model (Model access, lần đầu dùng Claude phải gửi form use case) hoặc IAM thiếu quyền; `ValidationException ... model identifier` = sai `BEDROCK_MODEL_ID` hoặc model không có ở region đó. Sửa `.env.monitoring` xong không cần build lại, chỉ chạy lại lệnh `run`.
- Bản 0.45.0 chỉ đọc cấu hình ở `~/.holmes/config.yaml` (biến `HOLMES_CONFIG_PATH` trong tài liệu của HolmesGPT không có tác dụng), nên image đóng sẵn file vào đúng chỗ đó.

### 7.20. AI tự phân tích cảnh báo và nhắn Telegram (holmes-bridge)

```
Prometheus (FIRING) ──► Alertmanager ──► Telegram: 🔴 SỰ CỐ ... (như cũ)
                              │
                              └─ webhook ──► holmes-bridge ──► HolmesGPT (Prometheus + Loki + Bedrock)
                                                   │
                                                   ├─ phân tích XONG ──► Telegram: 🔎 AI PHÂN TÍCH (vấn đề · nguyên nhân · bằng chứng · hướng xử lý)
                                                   └─ cảnh báo hết  ──► hỏi lại Prometheus ──► Telegram: 🟢 ĐÃ HOẠT ĐỘNG BÌNH THƯỜNG (trả lời ngay dưới tin phân tích)
```

| Thêm gì | File |
| ------- | ---- |
| Service cầu nối (Python thư viện chuẩn), dựng trên image `ecommerce-holmes` | `holmes-bridge/bridge.py`, `holmes-bridge/Dockerfile` |
| Alertmanager thêm `webhook_configs`, chỉ bật khi `.env.monitoring` có `AI_WEBHOOK_URL` | `alertmanager/alertmanager.yml`, `alertmanager/entrypoint.sh` |
| Service `holmes-bridge` (profile `ai`, chạy nền, không publish port) | `docker-compose.monitoring.yml` |
| Biến `AI_WEBHOOK_URL`, `AI_SEVERITIES`, `AI_MAX_PER_HOUR` | `.env.monitoring.example` |

**Quy tắc:** mỗi sự cố chỉ phân tích 1 lần (tin nhắc lại mỗi giờ của Alertmanager không làm tốn thêm Bedrock); tối đa `AI_MAX_PER_HOUR` lần/giờ; phân tích lần lượt, không song song; tin "đã hoạt động bình thường" kiểm tra lại `up` và `ALERTS` trên Prometheus (không gọi Bedrock), còn thành phần chưa trả lời hoặc còn cảnh báo khác thì báo 🟡 thay vì 🟢. AI lỗi (thiếu khoá, Bedrock từ chối, quá 5 phút) thì gửi 1 tin ⚠️ ngắn; tin cảnh báo gốc không bị ảnh hưởng. Trạng thái nằm trong bộ nhớ: restart bridge giữa lúc có sự cố thì sự cố đó không có tin xác nhận.

**Bật:** đặt `AI_WEBHOOK_URL=http://holmes-bridge:8080/alert` trong `.env.monitoring`, rồi
`$MON --profile ai up -d holmes-bridge` và `$MON up -d --force-recreate alertmanager`. Log Alertmanager (`docker logs ecommerce-alertmanager`) phải có `AI phân tích: BẬT`.

**Đã kiểm tra:** `amtool check-config` hợp lệ khi bật và khi tắt AI; khi tắt, cấu hình không có `webhook_configs`. **Chưa** chạy luồng AI thật (Bedrock + Telegram).

---

## 8. Lưu ý quan trọng & lỗi thường gặp

| Tình huống | Nguyên nhân | Cách xử lý |
| ---------- | ----------- | ---------- |
| `Bind for 0.0.0.0:80 failed: port is already allocated` | Chương trình khác trên EC2 đang dùng port 80 | Tắt chương trình đó, hoặc đổi `HTTP_PORT` trong `.env.app` |
| `set MYSQL_... in .env.app` | Chưa tạo file `.env.*` hoặc thiếu biến | `cp .env.app.example .env.app` (tương tự `.env.monitoring`) rồi điền mật khẩu |
| Đổi mật khẩu trong `.env` xong backend/exporter không đăng nhập được DB | MySQL chỉ tạo user **lần đầu**. Volume cũ vẫn giữ mật khẩu cũ | Không cần giữ dữ liệu: `$APP down -v` rồi `$APP up -d`. Cần giữ dữ liệu: vào MySQL bằng root đổi mật khẩu bằng `ALTER USER` |
| Target `mysql` **DOWN**, log exporter báo `Access denied` | User `exporter` chưa có (volume tạo trước khi có script) | Tạo tay bằng câu lệnh dưới đây |
| Target nào đó DOWN | Container đó chết/khởi động lại | `docker ps` và `docker logs ecommerce-<tên-service>` |
| Đổi `GRAFANA_ADMIN_PASSWORD` trong `.env` nhưng đăng nhập không được | Giống MySQL: mật khẩu admin Grafana chỉ được đặt **lần đầu**, sau đó lưu trong volume `grafana-data` | `docker exec ecommerce-grafana grafana cli admin reset-admin-password <mật-khẩu-mới>` |
| Ô biểu đồ hiện "No data" | Chưa có traffic, hoặc mới bật chưa đủ 1 phút | Vào web bấm vài trang, đợi khoảng 1 phút |
| `loki` và `alloy` **không** hiện `(healthy)` trong `docker ps` | **Bình thường, không phải lỗi.** Hai image này là distroless (không có `sh`/`wget`/`curl`) nên không đặt được healthcheck | Kiểm tra bằng tay: `curl http://127.0.0.1:3100/ready` và `curl http://127.0.0.1:12345/-/ready` |
| Dashboard **Shop Logs** trống hoàn toàn | Alloy không đọc được docker socket | `docker logs ecommerce-alloy`. Trên Linux cần user có quyền với `/var/run/docker.sock`; service đã đặt `user: root` sẵn |
| Shop Logs có log, nhưng **thiếu service nào đó** | Container đó đang rảnh nên không ghi log gì (ví dụ MySQL lúc không ai truy vấn), hoặc tên service không nằm trong danh sách `keep` của Alloy | Xem lại regex `keep` trong `alloy/config.remote.alloy`. Thêm service mới thì phải thêm tên vào đó |
| Log nginx bị đếm gấp đôi | `access_log` khai ở http context sẽ **cộng dồn** với bản mặc định của image | Phải khai `access_log` **trong `server` block** (xem lỗi #26 ở mục 7.6) |
| `ecommerce-alertmanager` khởi động lại liên tục | `.env` chưa bật kênh nào (Telegram hoặc mail), hoặc bật mà thiếu biến | `docker logs ecommerce-alertmanager` sẽ ghi rõ thiếu biến nào. Thêm vào `.env.monitoring` (mục 7.14 mail, 7.15 Telegram) |
| Đã cài Telegram nhưng tắt db không có tin nào | Prometheus chạy cấu hình cũ chưa có phần `alerting`, hoặc container Alertmanager vẫn giữ token cũ | Dò theo bảng "Không nhận được tin?" ở mục 7.15. Hay gặp nhất: `docker restart ecommerce-prometheus` và `$MON up -d --force-recreate alertmanager` |
| Sửa `.env` xong mà không có tác dụng | `docker restart` **không** đọc lại `.env.*` | `$APP (hoặc $MON) up -d --force-recreate <tên service>` |
| Log Loki có nhiều dòng `entry too far behind` sau khi `stop`/`start` một container | Alloy gửi lại vài dòng log cũ (hơn 1 tiếng) của container vừa bật lại; Loki từ chối vì quá cũ | **Bình thường, không mất log mới.** Bỏ qua |
| Ô CPU/RAM máy chủ khác số trên CloudWatch | node-exporter đo từ bên trong EC2 (RAM theo `MemAvailable`), CloudWatch đo từ phía hypervisor | Bình thường |
| API trả **502** ngay sau khi tạo lại container backend | Nginx nhớ IP cũ của container backend vừa bị tạo lại | Đã sửa bằng `resolver 127.0.0.11` + biến trong `proxy_pass` (lỗi #24 mục 7.6). Nếu vẫn gặp: `docker restart ecommerce-nginx` |

Tạo tay user `exporter` (chạy bằng root MySQL, thay mật khẩu đúng như trong `.env.app`):

```sql
CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '<MYSQL_EXPORTER_PASSWORD>' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
```

```bash
docker exec -it ecommerce-db mysql -uroot -p
```

Những điều **không** được làm:

- Không commit file `.env.app`, `.env.monitoring`.
- Không mở port nào khác ngoài 80 trên Security Group (MySQL, exporter, Prometheus, Grafana đều đi qua mạng Docker nội bộ hoặc `/admin/...`).
- Không đổi app sang dùng `root`.

---

## 9. Cấu trúc thư mục

Mỗi thư mục có `Dockerfile` riêng: config được đóng gói vào image `thuanperry/ecommerce-<tên>`, EC2 không cần file config nào.

```
.
├── docker-compose.app.yml        # EC2 — phần WEB: db, backend, frontend, nginx, 3 exporter, alloy
├── docker-compose.monitoring.yml # EC2 — phần GIÁM SÁT: prometheus, alertmanager, loki, grafana, holmes, holmes-bridge, mailpit
├── .env.app.example              # Mẫu biến cho phần web (file thật .env.app KHÔNG commit)
├── .env.monitoring.example       # Mẫu biến cho phần giám sát (file thật .env.monitoring KHÔNG commit)
├── backend/                      # REST API (Node.js + Express): src/ (server, metrics, logger, db, auth/, routes/), test/
├── frontend/                     # Giao diện tĩnh: shop, login, admin (tools.js = mục Monitoring), icons/, vendor/
├── nginx/                        # default.conf + snippets/ (log JSON, stub_status, /api, /admin/...), entrypoint.sh (tạo htpasswd)
├── mysql/init/                   # Tạo user 'exporter' chỉ đọc (chạy lần đầu)
├── alloy/config.remote.alloy     # Alloy: gom log container, parse từng service, đẩy sang Loki
├── prometheus/                   # prometheus.yml (6 nguồn số liệu) + rules/alerts.yml (10 luật cảnh báo)
├── alertmanager/                 # alertmanager.yml (khuôn) + entrypoint.sh + templates/ (mẫu Telegram, mail)
├── loki/loki-config.yml          # Loki: lưu log ở đâu, giữ bao lâu, giới hạn gì
├── grafana/                      # provisioning/ (datasource Prometheus + Loki) + dashboards/ (Shop Overview, Shop Logs)
├── holmes/                       # HolmesGPT CLI: config.yaml (nối Prometheus/Loki/Alertmanager) + entrypoint.sh
├── holmes-bridge/                # Cầu nối Alertmanager -> HolmesGPT -> Telegram (bridge.py)
├── loadtest/                     # Kịch bản k6: load.js (tải thường), overload.js (quá tải)
├── scripts/push-images.ps1       # Build + push toàn bộ image lên Docker Hub
├── TRA-CUU-NHANH.md              # Bảng tra cứu tool / port / link / lệnh
└── README.md
```

## 10. API

**Ai gọi được:** "Công khai" = mọi người. "Admin" = phải đăng nhập tài khoản admin (cookie `sid`), chưa đăng nhập trả **401**; request ghi/xoá đến từ trang web khác trả **403**.

| Method | Endpoint             | Ai gọi được | Mô tả                                   |
| ------ | -------------------- | ----------- | --------------------------------------- |
| GET    | `/api/products`      | Công khai | Danh sách sản phẩm                      |
| GET    | `/api/products/:id`  | Công khai | Chi tiết 1 sản phẩm                     |
| POST   | `/api/products`      | **Admin** | Thêm sản phẩm `{name, price, stock, image, description}` |
| PUT    | `/api/products/:id`  | **Admin** | Sửa sản phẩm                            |
| DELETE | `/api/products/:id`  | **Admin** | Xoá mềm (ẩn khỏi shop, đơn cũ giữ nguyên) |
| GET    | `/api/orders`        | **Admin** | Danh sách đơn hàng (có tên + email khách) |
| GET    | `/api/orders/:id`    | **Admin** | Chi tiết 1 đơn hàng                     |
| POST   | `/api/orders`        | Công khai | Đặt hàng `{customer, email, items[]}`   |
| POST   | `/api/auth/login`    | Công khai | Đăng nhập `{username, password}` → đặt cookie `sid`. Sai 5 lần / 15 phút → **429** |
| POST   | `/api/auth/logout`   | Công khai | Đăng xuất (xoá phiên)                   |
| GET    | `/api/auth/me`       | Công khai | Đang đăng nhập không. Luôn trả 200      |
| GET    | `/api/auth/check`    | Nội bộ    | nginx `auth_request` dùng để chặn `/admin.html` (204 / 401) |
| POST   | `/api/auth/password` | **Admin** | Đổi mật khẩu `{currentPassword, newPassword}`; đăng xuất mọi máy khác |
| GET    | `/health`            | Công khai | Kiểm tra sống/chết (Docker healthcheck dùng) |
| GET    | `/metrics`           | Nội bộ    | Số liệu cho Prometheus, gọi từ ngoài qua Nginx trả 404 |

```bash
curl -X POST http://localhost/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customer":"Nguyen Van A","email":"a@example.com","items":[{"productId":1,"quantity":2}]}'
```

---

## 11. Đã làm / chưa làm

Đã xong theo slide: bước 1–2 (Docker, ứng dụng), bước 3 (exporter + Prometheus), bước 4 (Alloy + Loki), bước 5 (Grafana), bước 6 (luật cảnh báo + Alertmanager gửi Telegram / mail), bước 7–8 (HolmesGPT + Bedrock, AI tự phân tích cảnh báo — mục 7.19, 7.20). Đã chạy trên EC2 (mục 12).

Còn để ngỏ:

- Luật cảnh báo đang ở chế độ demo (`[1m]`, `for: 1m`); lên production nên nới ra theo ghi chú đầu `prometheus/rules/alerts.yml`.
- Watchdog: báo động khi chính Prometheus / Alertmanager chết (mục 7.16).
- Cảnh báo dựa trên log (Loki ruler).
- HTTPS + domain riêng.

---

## 12. Triển khai trên EC2 (chi tiết)

**Cả web và giám sát chạy trên cùng 1 EC2**, tách thành 2 file `docker-compose`
độc lập để dễ quản lý / cập nhật riêng từng phần, nối nhau qua **1 Docker network dùng chung**.

### 12.1. Vì sao cần network dùng chung

Mỗi file `docker-compose` tự tạo 1 network bridge **riêng** (`appnet`,
`monitoringnet`) — 2 network đó **cô lập nhau theo mặc định**, dù chạy trên
cùng 1 máy (giống 2 VPC chưa peering, không phải 2 AZ cùng VPC). Phải tạo
thêm 1 network "phụ" rồi cho 2 bên cùng join vào thì mới gọi được nhau bằng
tên container:

```bash
docker network create ecommerce-shared   # chỉ tạo 1 lần, tồn tại độc lập
                                          # với mọi lần up/down compose
```

Service nào cần gọi chéo sang file bên kia thì join thêm `shared`:
- `backend`, `nginx-exporter`, `mysqld-exporter`, `node-exporter` (app) — để
  `prometheus` (monitoring) pull `/metrics` bằng tên container.
- `alloy` (app) — để đẩy log vào `ecommerce-loki` (monitoring) bằng tên container.
- `prometheus`, `loki` (monitoring) — phía nhận/gọi tương ứng.

Nhờ vậy **không cần publish port nào ra host** cho mục đích này — gọn và an
toàn hơn, vì traffic đi thẳng trong Docker network, Security Group không cần
quan tâm tới các port đó nữa. EC2 chỉ cần mở public đúng 1 port: **80** (nginx).

### 12.2. Mọi thứ đều đóng gói vào image, EC2 không cần file config nào

**Toàn bộ config đã đóng gói sẵn vào image** qua Dockerfile riêng từng thư
mục — không bind-mount gì từ host:

| Image | Dockerfile | Đóng gói gì |
| ----- | ---------- | ----------- |
| `thuanperry/ecommerce-prometheus` | `prometheus/Dockerfile` | `prometheus.yml` (target ghi tên container cố định) + `rules/` |
| `thuanperry/ecommerce-grafana` | `grafana/Dockerfile` | `provisioning/` + `dashboards/` |
| `thuanperry/ecommerce-loki` | `loki/Dockerfile` | `loki-config.yml` |
| `thuanperry/ecommerce-alertmanager` | `alertmanager/Dockerfile` | `alertmanager.yml` (khuôn) + `entrypoint.sh` + `templates/` |
| `thuanperry/ecommerce-holmes` | `holmes/Dockerfile` | `config.yaml` + `entrypoint.sh` |
| `thuanperry/ecommerce-holmes-bridge` | `holmes-bridge/Dockerfile` | `bridge.py` (dựng trên image holmes) |
| `thuanperry/ecommerce-nginx` | `nginx/Dockerfile` | `default.conf` + `snippets/` + `entrypoint.sh` |
| `thuanperry/ecommerce-alloy` | `alloy/Dockerfile` | `config.remote.alloy` |
| `thuanperry/ecommerce-db` | `mysql/Dockerfile` | `init/` |
| `thuanperry/ecommerce-backend`, `-frontend` | `backend/Dockerfile`, `frontend/Dockerfile` | code ứng dụng |

Secret (mật khẩu, token Telegram/SMTP, key Bedrock) **không** nằm trong image
— vẫn đọc từ `.env.app`/`.env.monitoring` lúc container chạy.

### 12.3. Các bước triển khai

**Bước 1 — Build + push toàn bộ image** (từ máy local, chỉ khi có sửa code):
```powershell
docker login
.\scripts\push-images.ps1
```

**Bước 2 — Trên EC2** (Amazon Linux 2023; cài Docker nếu chưa có: `sudo dnf install -y docker && sudo systemctl enable --now docker`, kèm docker compose plugin):
```bash
sudo mkdir -p /opt/ecommerce && cd /opt/ecommerce
docker network create ecommerce-shared

# Dán nội dung docker-compose.app.yml + .env.app (lấy từ repo, điền mật khẩu thật)
# Dán nội dung docker-compose.monitoring.yml + .env.monitoring (điền Grafana/Telegram/mail)

docker compose -f docker-compose.app.yml --env-file .env.app pull
docker compose -f docker-compose.app.yml --env-file .env.app up -d

docker compose -f docker-compose.monitoring.yml --env-file .env.monitoring pull
docker compose -f docker-compose.monitoring.yml --env-file .env.monitoring up -d

# (Tuỳ chọn) AI: điền khoá Bedrock + AI_WEBHOOK_URL trong .env.monitoring (mục 7.19-7.20)
docker compose -f docker-compose.monitoring.yml --env-file .env.monitoring --profile ai up -d holmes-bridge
```

**Bước 3 — Kiểm tra**:
```bash
docker ps                                            # 12 container (8 web + 4 giám sát), +1 nếu bật holmes-bridge
curl http://localhost/                               # trang shop, chạy qua nginx publish port 80
docker exec ecommerce-prometheus wget -qO- http://ecommerce-backend:3000/metrics | head -5
```
Muốn xem Grafana/Prometheus/Alertmanager từ máy bạn mà không mở port public:
dùng SSM port-forward (không cần SSH, không cần VPN):
```bash
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["3001"],"localPortNumber":["3001"]}'
```
rồi mở `http://localhost:3001` trên máy bạn như bình thường.

### 12.4. Security Group cho chế độ này

Chỉ cần mở public **80** (và **22** nếu không dùng SSM Session Manager thay
SSH). Không có port nào khác cần mở — toàn bộ giao tiếp app ↔ monitoring đi
qua network Docker nội bộ (`ecommerce-shared`), không chạm tới Security Group.
