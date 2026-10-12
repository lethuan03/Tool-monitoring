// ============================================================================
// Xác định "mẫu đường dẫn" (route template) của một request — dùng CHUNG cho
// cả metrics (Prometheus) và log (Loki) để hai bên luôn gắn nhãn giống nhau.
//
// Vì sao cần file này?
//   Cách tự nhiên là `req.baseUrl + req.route.path`. Nó đúng với request
//   THÀNH CÔNG, nhưng SAI khi request bị lỗi: khi lỗi được ném ra khỏi router,
//   Express phục hồi `req.baseUrl` về chuỗi rỗng trước khi tới error handler.
//   Lúc middleware đọc nhãn (sự kiện res 'finish') thì baseUrl đã mất, nên mọi
//   lỗi 500 đều bị gắn nhãn route="/" thay vì "/api/products/".
//   Hậu quả: biểu đồ "lỗi 5xx theo trang" vô dụng — không biết trang nào vỡ.
//
//   Cách sửa: khi request vừa đi vào một router, đóng dấu (stamp) sẵn đường dẫn
//   mount vào req. Giá trị này không bị Express xoá.
// ============================================================================

// Middleware đóng dấu đường dẫn mount, đặt ngay trước router tương ứng.
function stampMount(mountPath) {
  return function stamp(req, res, next) {
    req.mountPath = mountPath;
    next();
  };
}

// Trả về mẫu đường dẫn, ví dụ "/api/orders/:id" — KHÔNG bao giờ là id thật
// ("/api/orders/42"), vì mỗi id thật sẽ sinh thêm một chuỗi số liệu / stream
// log mới và làm Prometheus + Loki phình to.
function routeTemplate(req) {
  const base = req.mountPath !== undefined ? req.mountPath : req.baseUrl || '';

  if (req.route && req.route.path) return `${base}${req.route.path}`;

  // Request không khớp route nào (404 lạ, hoặc lỗi trước khi routing xong).
  // Nếu đã biết router nào nhận thì ghi kèm để còn truy được.
  return base ? `${base}/unmatched` : 'unmatched';
}

module.exports = { stampMount, routeTemplate };
