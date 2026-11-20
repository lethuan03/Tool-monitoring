# HostHighCpu — CPU máy chủ trên 90%

## Điều tra trước (chỉ đọc)
1. PromQL: `100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m])))`.
2. CPU backend: `rate(process_cpu_seconds_total{job="backend"}[1m])`; lưu lượng `sum(rate(http_requests_total[1m]))`.
3. LogQL: có vòng lặp lỗi/restart liên tục? `{job="docker", level="error"}`.

## Nguyên nhân thường gặp
- Lưu lượng cao hoặc đang chạy load test. Backend kẹt vòng lặp. Alloy/Loki xử lý log bùng nổ do lỗi lặp.

## Hành động có thể đề xuất (action_id)
- Một container kẹt CPU cao không có lưu lượng tương ứng: `restart_container` cho container đó (nếu được phép).
- Do lưu lượng thật: chỉ chẩn đoán (cần tăng cấu hình máy).
