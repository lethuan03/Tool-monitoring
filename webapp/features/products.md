# Sản phẩm

Files: webapp/backend/src/routes/products.js, webapp/backend/src/validate-product.js

## API
| API | Ai được dùng | Kết quả đúng |
|---|---|---|
| `GET /api/products` | Mọi người | 200, danh sách sản phẩm đang bán, xếp theo id |
| `GET /api/products/:id` | Mọi người | 200; id không tồn tại, đã xoá hoặc không phải số nguyên dương: 404 |
| `POST /api/products` | Admin | 201, trả sản phẩm vừa tạo |
| `PUT /api/products/:id` | Admin | 200, thay toàn bộ thông tin; id không có: 404 |
| `DELETE /api/products/:id` | Admin | 204; xoá lần nữa: 404 |

Chưa đăng nhập mà gọi API của admin: 401.

## Dữ liệu
- Trường trả về: `id, name, description, price, stock, image`. Không trả cột `active`.
- `name`: bắt buộc, tối đa 255 ký tự. `description`: chữ, tối đa 5000, mặc định rỗng.
- `price`: số (không phải chuỗi) từ 0 đến 9999999999.99, làm tròn 2 chữ số.
- `stock`: số nguyên từ 0, mặc định 0.
- `image`: rỗng hoặc link http/https, tối đa 512 ký tự.
- Dữ liệu sai: 400.

## Quy tắc
- Xoá là xoá mềm: sản phẩm không còn hiện và không mua được, đơn hàng cũ giữ nguyên.
