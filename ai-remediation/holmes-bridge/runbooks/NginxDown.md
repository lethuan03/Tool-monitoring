# NginxDown — nginx-exporter không đọc được stub_status của nginx
Khách có thể không vào được web.

## Điều tra trước (chỉ đọc)
1. PromQL: `up{job="nginx"}` và `nginx_up` — exporter sống nhưng nginx không trả lời?
2. LogQL: `{service="nginx", level="error"}`; tìm "emergency", "bind() failed", "host not found in upstream".
3. PromQL: `up{job="backend"}` — nginx có thể lỗi vì upstream backend/frontend chết.

## Nguyên nhân thường gặp
- nginx crash sau khi đổi cấu hình, hoặc không phân giải được upstream (backend/frontend chưa lên).
- Quá tải kết nối (`nginx_connections_active` rất cao).

## Hành động có thể đề xuất (action_id)
- `restart_container` -> `ecommerce-nginx` (nếu log cho thấy nginx đã thoát hoặc treo).
- Nếu nguyên nhân là backend chết: đề xuất restart `ecommerce-backend` trước.
