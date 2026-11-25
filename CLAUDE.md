# CLAUDE.md — Quy tắc làm việc trên project này

## QUY TẮC BẮT BUỘC (không có ngoại lệ nếu người dùng chưa cho phép rõ ràng)

### 1. Không tạo / thay đổi tài nguyên trên AWS
- Không tạo, sửa, xoá bất kỳ tài nguyên AWS nào (EC2, ALB, EBS, S3, IAM, Security Group, VPC, SSM, Bedrock...).
- Không chạy `aws ...` (CLI), `terraform`, `cdk`, `sam`, `pulumi`, `eksctl`, CloudFormation hay công cụ IaC nào trỏ tới AWS.
- Không gọi API AWS dưới mọi hình thức (kể cả SDK `boto3`, `@aws-sdk/*`, `curl` tới endpoint AWS).
- Không SSH / SSM vào EC2 để chạy lệnh.

### 2. Không đọc file `.env`
- Không đọc, in, grep, cat, copy, hay mở các file chứa bí mật: `.env`, `.env.app`, `.env.monitoring`, `.env.*` (trừ file mẫu bên dưới).
- Không dùng `docker compose config` hay lệnh khác làm lộ giá trị biến môi trường thật.
- Được phép đọc **file mẫu** không chứa bí mật: `.env.app.example`, `.env.monitoring.example`.
- Không bao giờ commit file `.env*` thật (đã chặn trong `.gitignore`).
- Cần biết tên biến → đọc README mục 3 hoặc file `.example`. Cần giá trị → hỏi người dùng.

### 3. Không chạy lệnh có thể tốn tiền
Cấm (hoặc phải hỏi và được đồng ý trước khi chạy):
- Mọi thứ gọi **AWS Bedrock** / HolmesGPT: `holmes ask`, `$MON --profile ai run ...`, bật `holmes-bridge`, gọi `AI_WEBHOOK_URL`. Các lệnh này tính phí theo token.
- Mọi lệnh gọi LLM/API trả phí bên ngoài (Anthropic, OpenAI, ...) từ code của project.
- `docker push`, `.\scripts\push-images.ps1` (đẩy image lên Docker Hub; ảnh hưởng tài khoản bên ngoài).
- Chạy load test (`webapp/loadtest/load.js`, `webapp/loadtest/overload.js`) nhắm vào máy chủ thật / EC2 / ALB — tốn băng thông, CPU, có thể kích hoạt cảnh báo Telegram/mail và AI.
- Bất kỳ lệnh nào gửi dữ liệu ra dịch vụ trả phí hoặc dịch vụ bên thứ ba (Telegram bot, SMTP...) .

Nếu không chắc một lệnh có tốn tiền hay không → **coi là có và hỏi người dùng trước**.

- Chạy thật `remediator` ở chế độ `execute`, hoặc bất kỳ lệnh nào restart/start/stop container thật (kể cả trên máy local nếu đang chạy stack của project). **Ngoại lệ duy nhất:** `ai-remediation/sandbox/` (`python ai-remediation/sandbox/run_sandbox.py`), chỉ đụng container `ecommerce-sbx-*`/`sbx-*` của project compose `ai-sandbox`, dùng AI/Telegram giả. Không bao giờ trỏ remediator vào container ngoài sandbox, và luôn `down` sandbox sau khi test.

## Được phép (an toàn, không tốn tiền)
- Đọc / sửa code, tài liệu, cấu hình trong repo (trừ file `.env*` thật).
- Chạy test cục bộ: `npm test` trong `webapp/backend/`; `node --test webapp/frontend/tests/tools.test.js`; `python -m unittest discover -s ai-remediation/<remediator|holmes-bridge|docker-proxy>/tests`.
- Kiểm tra cú pháp tĩnh, lint, đọc `git status`/`git diff`/`git log`.
- Build image Docker **cục bộ** (`docker build`) khi cần, không push.
- Chạy sandbox test cục bộ: `python ai-remediation/sandbox/run_sandbox.py` rồi `python ai-remediation/sandbox/run_sandbox.py down` (xem ngoại lệ ở trên).

## Tổng quan project (để làm việc nhanh)
- **Shop demo** thương mại điện tử 3 tầng: `webapp/frontend/` (HTML/JS tĩnh) · `webapp/backend/` (Node.js REST API, `prom-client`, session admin) · `webapp/mysql/` (MySQL 8), đứng sau `webapp/nginx/`.
- **Monitoring**: Prometheus (số liệu) + Alloy → Loki (log) + Grafana (dashboard) + Alertmanager (Telegram/mail) + HolmesGPT (AI điều tra, gọi AWS Bedrock — tốn phí).
- Triển khai: 1 EC2, 2 file compose (`webapp/docker-compose.app.yml`, `monitoring/docker-compose.monitoring.yml`, mỗi file cạnh `.env` của nó), image trên Docker Hub `thuanperry/ecommerce-*`, nối nhau qua network `ecommerce-shared`.
- Tài liệu: `README.md` (đầy đủ), `TRA-CUU-NHANH.md` (port/link/lệnh), `ai-remediation/README2.md` (phần mở rộng: runbook cho AI + remediator đề xuất/duyệt/audit lệnh sửa lỗi). Mỗi thay đổi thuộc phần mở rộng này phải cập nhật `README2.md`.
- Luật lệnh/container AI được đề xuất: `ai-remediation/remediator/policy.json` (không nới luật nếu người dùng chưa bảo).

## Quy ước khi sửa code
- Mật khẩu trong `.env` chỉ dùng chữ và số (ký tự `$`, `'` làm hỏng compose / SQL).
- Dashboard Grafana quản lý bằng file JSON trong `monitoring/grafana/dashboards/`; sửa file rồi mới build lại image.
- Label Loki chỉ dùng giá trị hữu hạn (không đặt `path`, `ip`, `order_id`... làm label).
- Route trong metrics ghi theo mẫu (`/api/orders/:id`), không ghi id thật.
- Chỉ sửa file khi được yêu cầu; không commit/push nếu người dùng chưa bảo.
