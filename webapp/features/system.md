# Chung toàn hệ thống

Files: *

Bản mô tả chức năng đúng của shop. merge-gate đưa cho AI làm chuẩn chấm (bản trên nhánh `main`).
Dòng `Files:` cho biết chức năng nằm ở file nào; `*` = luôn đưa vào.

## Đường đi
- Mọi request đi qua nginx. `/api/*` và `/health` tới backend; còn lại là trang tĩnh của frontend.
- `GET /health` trả 200. `/metrics` gọi từ ngoài trả 404.
- API không tồn tại dưới `/api` trả 404 `{"error": "Not found"}`.
- Lỗi do dữ liệu người gửi trả mã 4xx kèm `{"error": "..."}`, không được trả 500.

## Lưu ý khi viết kịch bản kiểm thử
- Database mới, có sẵn 6 sản phẩm mẫu và 1 tài khoản admin. Chưa có đơn hàng nào.
- Các kịch bản dùng chung database và chạy lần lượt: chỉ sửa hoặc xoá dữ liệu do chính kịch bản tạo ra.
- Không dựng được trạng thái "không còn sản phẩm nào" (không có vòng lặp, và không được xoá dữ liệu mẫu).
- Đăng nhập sai nhiều lần sẽ bị khoá: cả bộ kịch bản chỉ đăng nhập sai tối đa 2 lần.
