# Đăng nhập và quản trị

Files: webapp/backend/src/routes/auth.js, webapp/backend/src/auth/*

## API
| API | Kết quả đúng |
|---|---|
| `POST /api/auth/login` `{username, password}` | 200 `{username, role}` và cookie phiên; sai: 401 (cùng thông báo dù sai tên hay sai mật khẩu); thiếu dữ liệu: 400 |
| `POST /api/auth/logout` | 204, phiên hết hiệu lực |
| `GET /api/auth/me` | Luôn 200: `{"authenticated": false}` hoặc `{"authenticated": true, "username", "role"}` |
| `GET /api/auth/check` | 204 nếu admin đã đăng nhập, ngược lại 401 |
| `POST /api/auth/password` `{currentPassword, newPassword}` | Admin; 204; mật khẩu mới dưới 12 ký tự, trùng mật khẩu cũ hoặc sai mật khẩu hiện tại: 400 |

## Quy tắc
- Sai 5 lần trong 15 phút từ một địa chỉ thì bị khoá, trả 429.
- Đổi mật khẩu thì mọi phiên cũ bị đăng xuất.
- Request thay đổi dữ liệu đến từ trang khác (`Sec-Fetch-Site: cross-site` hoặc `Origin` khác tên miền): 403.
- Chưa đăng nhập mà mở `/admin.html`: chuyển hướng 302 về `/login.html`.
- Không bao giờ ghi hoặc trả mật khẩu.
