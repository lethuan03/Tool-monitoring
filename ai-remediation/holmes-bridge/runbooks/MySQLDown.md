# MySQLDown — mysqld-exporter không kết nối được MySQL
Đặt hàng/đăng nhập admin sẽ lỗi; backend trả 5xx.

## Điều tra trước (chỉ đọc)
1. PromQL: `mysql_up`, `mysql_global_status_uptime` (về gần 0 = vừa khởi động lại), `mysql_global_status_threads_connected / mysql_global_variables_max_connections`.
2. LogQL: `{service="db"}` (tìm "ready for connections", "Out of memory", "InnoDB", "Access denied"); `{service="backend", level="error"} |= "ENOTFOUND"`.
3. PromQL: `node_memory_MemAvailable_bytes` và `node_filesystem_avail_bytes{mountpoint="/"}` — MySQL hay bị OOM-kill hoặc hết ổ đĩa.

## Nguyên nhân thường gặp
- OOM-kill do máy hết RAM. Hết ổ đĩa. Container db đang khởi động lại.
- Sai mật khẩu user `exporter` (mysql_up=0 nhưng web vẫn chạy được).
- Hết kết nối (`max_connections`).

## Hành động có thể đề xuất (action_id)
- KHÔNG đề xuất restart `ecommerce-db` khi nó đang chạy (dữ liệu quan trọng, chính sách cấm). Chỉ chẩn đoán và nêu bước cho người vận hành.
- DB ĐÃ DỪNG (Prometheus: `mysql_up == 0` kéo dài, exporter báo "lookup db: no such host" hoặc Loki `{service="db"}` có "Shutdown complete"/"SHUTDOWN" và không có dòng "ready for connections" mới hơn): ĐỀ XUẤT `start_container` -> `ecommerce-db` (policy chỉ cho BẬT LẠI DB, an toàn, dữ liệu nằm trong volume). Sau khi DB lên, vòng sau nếu `mysql_up` vẫn 0 mới đề xuất restart `ecommerce-mysqld-exporter`.
- Policy chặn `restart_container` và `logs_tail` trên `ecommerce-db`, đừng đề xuất. Lấy bằng chứng bằng Loki `{service="db"}`; nếu cần thêm thì `logs_tail` của `ecommerce-backend`.
- Backend đã báo lỗi kết nối DB nhưng DB đã sống lại: `restart_container` -> `ecommerce-backend` để nối lại.
- DB đang chạy bình thường (log có "ready for connections", backend vẫn kết nối được) nhưng `mysql_up` vẫn 0: lỗi nằm ở exporter, ĐỀ XUẤT `restart_container` -> `ecommerce-mysqld-exporter` (container này được phép, không ảnh hưởng khách).
- Muốn xem log exporter trước khi restart: `logs_tail` -> `ecommerce-mysqld-exporter`.

## Nếu lệnh trước không hiệu quả (vòng sau)
- Đã restart backend mà `mysql_up` vẫn 0: lỗi nằm ở DB hoặc ổ đĩa/RAM máy, ghi `không có` và nêu rõ bước cho người vận hành.
- Đã restart `ecommerce-mysqld-exporter` mà `mysql_up` vẫn 0: nghi sai mật khẩu user `exporter` hoặc DB chưa nhận kết nối; đọc `logs_tail` của exporter rồi ghi `không có` nếu không còn lệnh an toàn.
