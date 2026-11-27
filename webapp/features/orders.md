# Đơn hàng

Files: webapp/backend/src/routes/orders.js

## API
| API | Ai được dùng | Kết quả đúng |
|---|---|---|
| `POST /api/orders` | Mọi người | 201, trả đơn vừa tạo kèm `items` |
| `GET /api/orders` | Admin | 200, danh sách đơn kèm `items`, mới nhất trước |
| `GET /api/orders/:id` | Admin | 200; không có: 404 |

Chưa đăng nhập mà xem đơn: 401 (đơn có tên và email khách).

## Đặt hàng
- Body: `{"customer": "...", "email": "...", "items": [{"productId": 1, "quantity": 2}]}`.
- Thiếu `customer` hoặc `email`, `items` rỗng: 400.
- Sản phẩm không tồn tại hoặc đã xoá: 400. Số lượng không phải số nguyên dương: 400.
- Số lượng vượt tồn kho: 409.
- `total` = tổng (giá hiện tại × số lượng). Tồn kho giảm đúng số lượng đã đặt.
- Một dòng hàng lỗi thì cả đơn không được tạo và tồn kho không đổi.
