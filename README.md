# Shop Demo — Web thương mại điện tử + Monitoring

Web bán hàng demo, tách **3 tầng**: Frontend (tĩnh) · Backend (REST API) · Database (MySQL), đứng sau **Nginx**.
Đã gắn phần **giám sát số liệu (metrics)**: Exporter + Prometheus (**bước 3/8**) và Grafana dashboard tự cập nhật (**bước 5/8**) trong slide "Tool Monitor".

> **Không nhớ port/link?** Xem bảng tra cứu: **[TRA-CUU-NHANH.md](TRA-CUU-NHANH.md)**. Gồm mọi tool, port, link, cách đăng nhập và lệnh hay dùng.

Mục lục:

1. [Chạy nhanh (người mới clone về đọc mục này)](#1-chạy-nhanh-người-mới-clone-về-đọc-mục-này)
2. [Cổng (port) và khi nào phải đổi](#2-cổng-port-và-khi-nào-phải-đổi)
3. [Biến môi trường (.env)](#3-biến-môi-trường-env)
4. [Monitoring giải thích đơn giản](#4-monitoring-giải-thích-đơn-giản)
5. [Các tài khoản (user) MySQL — vì sao có 3 user](#5-các-tài-khoản-user-mysql--vì-sao-có-3-user)
6. [Kiểm tra hệ thống & kịch bản demo sự cố](#6-kiểm-tra-hệ-thống--kịch-bản-demo-sự-cố)
7. [Nhật ký thay đổi: lỗi phát hiện → sửa thành gì → vì sao](#7-nhật-ký-thay-đổi-lỗi-phát-hiện--sửa-thành-gì--vì-sao)
8. [Lưu ý quan trọng & lỗi thường gặp](#8-lưu-ý-quan-trọng--lỗi-thường-gặp)
9. [Cấu trúc thư mục](#9-cấu-trúc-thư-mục)
10. [API](#10-api)
11. [Chưa làm (các bước sau)](#11-chưa-làm-các-bước-sau)

---

## 1. Chạy nhanh (người mới clone về đọc mục này)

Yêu cầu: Docker Desktop đang chạy.

```bash
git clone <repo> && cd Tool-monitoring

# 1) Tạo file .env từ file mẫu (file .env KHÔNG có trên git — mỗi máy tự tạo)
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env

# 2) Mở .env, thay toàn bộ giá trị change_me_... bằng mật khẩu của bạn (chỉ chữ + số)
#    Nếu port 8080 trên máy đã bị app khác dùng -> đổi HTTP_PORT (xem mục 2)

# 3) Bật toàn bộ hệ thống
docker compose up -d --build
```

Sau khoảng 30–60 giây:

| Mở gì       | Địa chỉ                         |
| ----------- | ------------------------------- |
| Trang web   | <http://localhost:8080> (hoặc port bạn đặt ở `HTTP_PORT`) |
| **Grafana (màn hình theo dõi chính)** | <http://localhost:3001> — đăng nhập bằng `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` trong `.env`, mở ra là dashboard **Shop Overview** |
| Prometheus (dành cho kỹ thuật, tra cứu/gỡ lỗi) | <http://localhost:9090>         |

Tắt: `docker compose down` (giữ dữ liệu) · Xoá sạch cả dữ liệu: `docker compose down -v`.

---

## 2. Cổng (port) và khi nào phải đổi

**Port** giống "số cửa" trên máy tính. Mỗi cửa chỉ một chương trình được đứng. Nếu máy đã có app khác đứng ở cửa 8080 thì Docker báo lỗi `port is already allocated`.

| Port   | Của ai               | Mở ra ngoài máy? | Ghi chú |
| ------ | -------------------- | ---------------- | ------- |
| `8080` | Nginx (cửa vào web)  | Có               | Đổi được qua `HTTP_PORT` trong `.env` |
| `3001` | Grafana              | Chỉ `localhost`  | Đổi được qua `GRAFANA_PORT` trong `.env` |
| `9090` | Prometheus           | Chỉ `localhost`  | Chỉ người ngồi trên máy đó mở được |
| `3000` | Backend              | Không            | Chỉ các container nói chuyện nội bộ |
| `3306` | MySQL                | Không            | 〃 |
| `8081` | Nginx stub_status    | Không            | 〃 (cho nginx-exporter đọc) |
| `9113` | nginx-exporter       | Không            | 〃 |
| `9104` | mysqld-exporter      | Không            | 〃 |

**Bạn của bạn `git pull` về có cần chỉnh port không?**

- `HTTP_PORT` nằm trong **`.env`**, mà `.env` **không được đưa lên git**. Mỗi người tự có file `.env` riêng, copy từ `.env.example`.
- Trong `.env.example` mặc định là `HTTP_PORT=8080`.
  - Máy bạn ấy **trống port 8080** → không cần làm gì.
  - Máy bạn ấy **đã có app chiếm 8080** → mở `.env` của bạn ấy, đổi ví dụ `HTTP_PORT=8088`, rồi vào web ở <http://localhost:8088>.
- Việc đổi port chỉ ảnh hưởng **máy đó**, không ảnh hưởng người khác, không cần commit gì.
- Các port nội bộ (3000, 3306, 8081, 9113, 9104) **không bao giờ cần đổi**, vì chúng không chiếm cửa của máy thật.
- Nếu port `9090` bị trùng: sửa dòng `"127.0.0.1:9090:9090"` trong `docker-compose.yml` thành ví dụ `"127.0.0.1:9091:9090"` (chỉ đổi số **bên trái**).

---

## 3. Biến môi trường (.env)

`.env` chứa mật khẩu nên **không bao giờ commit**. Nó đã nằm trong `.gitignore` và `.dockerignore`, không lọt vào git hay image. `.env.example` là file mẫu không chứa mật khẩu thật, được commit để người khác biết cần những biến gì.

| Biến                        | Dùng cho                                              |
| --------------------------- | ----------------------------------------------------- |
| `MYSQL_ROOT_PASSWORD`       | Tài khoản **root** của MySQL — chỉ để quản trị, app không dùng |
| `MYSQL_DATABASE`            | Tên database (`ecommerce`)                            |
| `MYSQL_USER` / `MYSQL_PASSWORD` | Tài khoản **riêng của web** (`ecommerce_app`)     |
| `MYSQL_EXPORTER_PASSWORD`   | Tài khoản **exporter** (chỉ đọc số liệu) cho mysqld-exporter |
| `PROMETHEUS_RETENTION_TIME` | Prometheus giữ số liệu bao lâu (mặc định `15d` = 15 ngày) |
| `PROMETHEUS_RETENTION_SIZE` | Prometheus dùng tối đa bao nhiêu ổ đĩa (mặc định `5GB`) |
| `HTTP_PORT`                 | Port vào web trên máy bạn (mặc định `8080`)           |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | Tài khoản đăng nhập Grafana          |
| `GRAFANA_PORT`              | Port mở Grafana trên máy bạn (mặc định `3001`)        |
| `DB_*`                      | Chỉ dùng khi chạy backend bằng `npm start`, không qua Docker |

Mật khẩu **chỉ dùng chữ và số**. Ký tự `$` bị docker-compose hiểu là biến, còn `'` làm hỏng câu lệnh SQL tạo user.

Nếu thiếu biến bắt buộc, `docker compose up` dừng ngay và báo rõ, ví dụ `set MYSQL_ROOT_PASSWORD in .env`. Cách này để không bao giờ chạy với mật khẩu rỗng.

---

## 4. Monitoring giải thích đơn giản

### 4.1. Bức tranh chung

Monitoring (giám sát) = **luôn biết hệ thống đang khoẻ hay ốm, và ốm ở đâu**, mà không phải đợi khách báo lỗi.

Trong bài này có 2 vai trò:

| Vai trò        | Ví von                | Làm gì |
| -------------- | --------------------- | ------ |
| **Exporter**   | Người phiên dịch      | Đứng cạnh một ứng dụng (Nginx, MySQL…), đọc tình trạng của nó và đọc ra thành **con số** theo định dạng Prometheus hiểu |
| **Prometheus** | Người đi ghi số điện nước | Cứ **15 giây** đi một vòng tới từng exporter, **tự kéo số về** (gọi là *pull / scrape*) và ghi vào sổ theo thời gian |

```
  ỨNG DỤNG              PHIÊN DỊCH (exporter)            GHI SỔ
  ─────────             ─────────────────────            ──────
  Backend  ── tự đọc ra số ở /metrics ──────────────┐
  Nginx    ── stub_status ──► nginx-exporter ───────┼──► Prometheus (kéo mỗi 15s, lưu 15 ngày)
  MySQL    ── user exporter ─► mysqld-exporter ─────┘
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

1. Mở **<http://localhost:9090/query>**.
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

**Thử lỗi:** chạy `docker compose stop db`, vào web vài lần.

- Query 7 thành `0`, query 4 tăng lên.
- Query 1 với `mysql` vẫn là `1`, vì exporter còn sống, chỉ MySQL chết.

Khôi phục bằng `docker compose start db`.

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
4. Commit file đó. Grafana tự nạp lại sau khoảng 30 giây.

**"Realtime" chậm bao nhiêu?** Prometheus lấy số mỗi 15 giây, Grafana làm mới mỗi 15 giây. Vì vậy biểu đồ chậm hơn thực tế tối đa khoảng **15–30 giây**. Đây là mức chuẩn của production. Các biểu đồ "mỗi giây" tính trung bình trên khoảng **1 phút** gần nhất, nên một sự kiện cần khoảng 1 phút để hiện rõ.

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

1. `docker compose ps` → mọi container `Up`. `db`, `backend`, `prometheus`, `grafana` hiện thêm `(healthy)`.
2. Mở Grafana <http://localhost:3001> → hàng **Tổng quan** có 6 ô **UP** xanh.
3. Vào web, xem sản phẩm, đặt vài đơn → khoảng 1 phút sau các biểu đồ lưu lượng và đơn/phút nhảy lên.
4. Kiểm tra bảo mật: <http://localhost:8080/metrics> phải ra **404**. Số liệu không bị lộ ra ngoài, chỉ Prometheus đọc được trong mạng nội bộ.
5. (Kỹ thuật) <http://localhost:9090/targets> → 4 mục **UP**.

### 6.2. Kịch bản demo: giả lập sự cố và xem dashboard phản ứng

Mở Grafana ở một cửa sổ, chạy lệnh ở cửa sổ khác. Đợi khoảng **30–60 giây** sau mỗi lệnh để dashboard cập nhật.

| # | Giả lập | Lệnh | Dashboard sẽ thấy | Khôi phục |
| - | ------- | ---- | ----------------- | --------- |
| 1 | **Database chết** | `docker compose stop db` | "MySQL đăng nhập được" → **DOWN** đỏ. Vào web bị lỗi → "Tỉ lệ lỗi 5xx" **đỏ**, cột `500` xuất hiện ở "Request theo mã trạng thái". Đơn/phút tụt về 0 | `docker compose start db`. Web **tự kết nối lại**, không cần khởi động lại backend |
| 2 | **Backend chết** | `docker compose stop backend` | "Backend (web app)" → **DOWN** đỏ. Biểu đồ backend ngừng vẽ | `docker compose start backend` |
| 3 | **Mất giám sát** (exporter chết, app vẫn chạy) | `docker compose stop mysqld-exporter` | "MySQL exporter" **DOWN**, nhưng web vẫn chạy bình thường. Đây là cách phân biệt "app chết" với "công cụ đo bị chết" | `docker compose start mysqld-exporter` |
| 4 | **Khách gửi yêu cầu sai** (lỗi 4xx) | Mở `http://localhost:8080/api/products/9999` vài lần (sản phẩm không tồn tại → 404) | Cột `404` xuất hiện ở "Request theo mã trạng thái", nhưng tỉ lệ **5xx vẫn xanh**. Lỗi phía khách không phải sự cố server | — |
| 5 | **Lưu lượng tăng đột biến** | Chạy vòng lặp gọi API (dưới đây) | Request/giây, query MySQL/giây, CPU backend tăng vọt | Bấm `Ctrl+C` |

Tạo tải cho kịch bản 5 (Git Bash; đổi `8080` nếu bạn dùng `HTTP_PORT` khác):

```bash
while true; do curl -s -o /dev/null http://localhost:8080/api/products; done
```

**Hiện tại hệ thống "phát hiện và hiển thị" sự cố, chưa "tự báo" sự cố.** Muốn có tin nhắn hoặc email tự gửi tới khi có ô chuyển đỏ thì cần **bước 6: luật cảnh báo + Alertmanager** (chưa làm). Việc "xử lý" là con người đọc dashboard để biết hỏng ở đâu rồi khôi phục như cột cuối. Tự động điều tra nguyên nhân là bước 7–8 (HolmesGPT).

---

## 7. Nhật ký thay đổi: lỗi phát hiện → sửa thành gì → vì sao

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

### 7.6. Đã biết nhưng **chưa** xử lý (ngoài phạm vi monitoring)

| Vấn đề | Rủi ro | Hướng xử lý sau |
| ------ | ------ | --------------- |
| `POST /api/products` (thêm sản phẩm) **không cần đăng nhập** | Ai vào được web cũng thêm được sản phẩm | Thêm xác thực admin, hoặc chặn route này ở nginx khi lên production |
| Không giới hạn tần suất gọi API (rate limit) | Có thể bị spam đặt đơn | `limit_req` ở nginx |
| `cors()` cho phép mọi domain | Không cần thiết vì frontend và API cùng domain qua nginx | Bỏ hoặc giới hạn domain khi có domain thật |
| Chưa có HTTPS | Dữ liệu đi dạng rõ | Làm khi lên server có domain |

---

## 8. Lưu ý quan trọng & lỗi thường gặp

| Tình huống | Nguyên nhân | Cách xử lý |
| ---------- | ----------- | ---------- |
| `Bind for 0.0.0.0:8080 failed: port is already allocated` | App khác trên máy đang dùng 8080 | Đổi `HTTP_PORT` trong `.env` (mục 2) |
| `set MYSQL_... in .env` | Chưa tạo `.env` hoặc thiếu biến | `cp .env.example .env` rồi điền mật khẩu |
| Đổi mật khẩu trong `.env` xong backend/exporter không đăng nhập được DB | MySQL chỉ tạo user **lần đầu**. Volume cũ vẫn giữ mật khẩu cũ | Máy dev không cần giữ dữ liệu: `docker compose down -v` rồi `up` lại. Cần giữ dữ liệu: vào MySQL bằng root đổi mật khẩu bằng `ALTER USER` |
| Target `mysql` **DOWN**, log exporter báo `Access denied` | User `exporter` chưa có (volume tạo trước khi có script) | Tạo tay bằng câu lệnh dưới đây |
| Target nào đó DOWN | Container đó chết/khởi động lại | `docker compose ps` và `docker compose logs <tên-service>` |
| Đổi `GRAFANA_ADMIN_PASSWORD` trong `.env` nhưng đăng nhập không được | Giống MySQL: mật khẩu admin Grafana chỉ được đặt **lần đầu**, sau đó lưu trong volume `grafana-data` | `docker compose exec grafana grafana cli admin reset-admin-password <mật-khẩu-mới>` |
| Ô biểu đồ hiện "No data" | Chưa có traffic, hoặc mới bật chưa đủ 1 phút | Vào web bấm vài trang, đợi khoảng 1 phút |

Tạo tay user `exporter` (chạy bằng root MySQL, thay mật khẩu đúng như trong `.env`):

```sql
CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '<MYSQL_EXPORTER_PASSWORD>' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
```

```bash
docker exec -it ecommerce-db mysql -uroot -p
```

Những điều **không** được làm:

- Không commit file `.env`.
- Không mở port MySQL (3306), exporter, Prometheus hay Grafana ra ngoài.
- Không đổi app sang dùng `root`.

---

## 9. Cấu trúc thư mục

```
.
├── backend/                      # REST API (Node.js + Express)
│   ├── src/
│   │   ├── server.js             # Khởi tạo app, /health, /metrics, tắt êm (graceful shutdown)
│   │   ├── metrics.js            # prom-client: số liệu Node.js + HTTP (đếm, thời gian phản hồi)
│   │   ├── db.js                 # MySQL (mysql2): pool, chờ DB, tạo bảng, seed dữ liệu mẫu
│   │   └── routes/
│   │       ├── products.js
│   │       └── orders.js
│   ├── Dockerfile
│   └── package.json
├── frontend/                     # Giao diện tĩnh (HTML/CSS/JS), phục vụ bởi nginx
├── nginx/default.conf            # Reverse proxy (cửa vào) + stub_status nội bộ :8081
├── mysql/init/01-exporter-user.sh# Tạo user 'exporter' chỉ đọc (chạy lần đầu)
├── prometheus/prometheus.yml     # Prometheus đi lấy số ở đâu, bao lâu một lần
├── grafana/
│   ├── provisioning/             # Tự cấu hình khi Grafana khởi động
│   │   ├── datasources/prometheus.yml   # Kết nối tới Prometheus
│   │   ├── dashboards/shop.yml          # Nạp dashboard từ thư mục grafana/dashboards
│   │   └── alerting/                    # (để trống — dành cho bước 6)
│   └── dashboards/shop-overview.json    # Dashboard "Shop Overview" (toàn bộ query nằm ở đây)
├── TRA-CUU-NHANH.md              # Bảng tra cứu tool / port / link / lệnh
├── .env.example                  # File mẫu biến môi trường (commit)
├── .env                          # Biến thật + mật khẩu (KHÔNG commit)
├── .gitattributes                # Ép xuống dòng kiểu Linux cho script/config
├── docker-compose.yml            # Bật cả hệ thống bằng 1 lệnh
└── README.md
```

Chạy riêng backend không qua Docker (để debug API). Cần MySQL có sẵn database `ecommerce` và user `ecommerce_app`:

```bash
cd backend && npm install && npm start   # đọc DB_* từ .env ở thư mục gốc
```

Cách này **chỉ có API** tại <http://localhost:3000/api/products>. Giao diện web do container `frontend` + `nginx` phục vụ, nên muốn có đầy đủ web thì chạy bằng Docker.

---

## 10. API

| Method | Endpoint             | Mô tả                                   |
| ------ | -------------------- | --------------------------------------- |
| GET    | `/api/products`      | Danh sách sản phẩm                      |
| GET    | `/api/products/:id`  | Chi tiết 1 sản phẩm                     |
| POST   | `/api/products`      | Thêm sản phẩm `{name, price, ...}`      |
| GET    | `/api/orders`        | Danh sách đơn hàng                      |
| GET    | `/api/orders/:id`    | Chi tiết 1 đơn hàng                     |
| POST   | `/api/orders`        | Đặt hàng `{customer, email, items[]}`   |
| GET    | `/health`            | Kiểm tra sống/chết (Docker healthcheck dùng) |
| GET    | `/metrics`           | Số liệu cho Prometheus — **chỉ nội bộ**, gọi từ ngoài qua Nginx trả 404 |

```bash
curl -X POST http://localhost:8080/api/orders \
  -H "Content-Type: application/json" \
  -d '{"customer":"Nguyen Van A","email":"a@example.com","items":[{"productId":1,"quantity":2}]}'
```

---

## 11. Chưa làm (các bước sau)

Theo slide, các bước tiếp theo **chưa** có trong bài này:

- Bước 4: Alloy + Loki (gom log)
- Bước 6: luật cảnh báo + Alertmanager
- Bước 7–8: HolmesGPT + Bedrock
- Đưa lên server (EC2), gắn domain + HTTPS: làm sau khi web hoàn thiện
