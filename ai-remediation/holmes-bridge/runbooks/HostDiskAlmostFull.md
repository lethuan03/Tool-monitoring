# HostDiskAlmostFull / HostDiskFull — ổ đĩa `/` trên 85% / 95%
Đầy ổ thì MySQL, Loki, Prometheus không ghi được.

## Điều tra trước (chỉ đọc)
1. PromQL: `100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})` và tốc độ `deriv(node_filesystem_avail_bytes{mountpoint="/"}[30m])`.
2. LogQL: log bùng nổ? `sum by (service) (rate({job="docker"}[5m]))`.
3. `prometheus_tsdb_head_series` tăng vọt (nghi sai nhãn)?

## Nguyên nhân thường gặp
- Log container đầy (đã giới hạn 5x10MB). Image Docker cũ tích tụ. Dữ liệu Loki/Prometheus phình.

## Hành động có thể đề xuất (action_id)
- Không có action tự động phù hợp (chỉ chẩn đoán bằng số liệu node-exporter/Loki).
- KHÔNG đề xuất xoá/prune (chính sách cấm); nêu bước dọn dẹp cho người vận hành.
