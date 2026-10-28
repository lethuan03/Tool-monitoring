// ============================================================================
// Mục "Monitoring" của trang admin (admin.html#monitoring) — mỗi tool một ô:
// logo + tên, bấm là mở tab mới.
//
// Các tool KHÔNG còn mở trực tiếp qua localhost:port của máy chạy Docker nữa.
// nginx làm reverse-proxy trung gian (xem nginx/snippets/app-locations.conf):
// Grafana/Prometheus/Alertmanager được truy cập qua đường dẫn /admin/... trên
// CÙNG domain của web, chỉ khi đã đăng nhập admin (auth_request). Nhờ vậy xem
// được từ bất kỳ đâu qua domain public, không cần SSH tunnel/VPN/SSM nữa, và
// không expose các cổng monitoring ra ngoài.
//
// Logo trong frontend/icons/ lấy từ chính các tool (Grafana, Prometheus,
// Alertmanager) nên không phải tải từ nguồn ngoài.
// Loki không có giao diện riêng: xem log qua Grafana (dashboard Shop Logs).
// ============================================================================
const TOOL_CONFIG = {
  tools: [
    { name: 'Grafana', icon: 'icons/grafana.svg', path: '/admin/grafana/' },
    { name: 'Prometheus', icon: 'icons/prometheus.svg', path: '/admin/prometheus/query' },
    { name: 'Prometheus Targets', icon: 'icons/prometheus.svg', path: '/admin/prometheus/targets' },
    { name: 'Prometheus Alerts', icon: 'icons/prometheus.svg', path: '/admin/prometheus/alerts' },
    { name: 'Alertmanager', icon: 'icons/alertmanager.svg', path: '/admin/alertmanager/' },
  ],
};
// Tạo URL cho một tool. Giờ chỉ là đường dẫn tương đối (nginx proxy cùng domain).
// Hàm thuần (không đụng DOM) để test được bằng node:test.
function buildToolUrl(config, tool) {
  return tool.path;
}
// ---------- Render lên trang (chỉ chạy trên trình duyệt) ----------
function renderTools(config, root) {
  config.tools.forEach((tool) => {
    const card = document.createElement('a');
    card.className = 'tool-tile';
    card.href = buildToolUrl(config, tool);
    card.target = '_blank';
    card.rel = 'noopener noreferrer';
    const img = document.createElement('img');
    img.src = tool.icon;
    img.alt = ''; // trang trí, tên tool đã có ở dưới
    img.width = 48;
    img.height = 48;
    card.appendChild(img);
    // Tên + địa chỉ (host:cổng/đường dẫn) để biết tool đang chạy ở đâu
    const text = document.createElement('span');
    text.className = 'tool-text';
    const name = document.createElement('span');
    name.className = 'tool-name';
    name.textContent = tool.name;
    const url = document.createElement('span');
    url.className = 'tool-url';
    url.textContent = card.href.replace(/^https?:\/\//, '');
    text.append(name, url);
    card.appendChild(text);
    const hint = document.createElement('span');
    hint.className = 'visually-hidden'; // class ẩn-cho-screen-reader của Tabler/Bootstrap
    hint.textContent = ' (mở tab mới)';
    card.appendChild(hint);
    // Mũi tên ↗: admin.js chèn icon SVG vào thẻ có data-icon
    const arrow = document.createElement('span');
    arrow.className = 'tool-arrow';
    arrow.dataset.icon = 'arrowUpRight';
    arrow.setAttribute('aria-hidden', 'true');
    card.appendChild(arrow);
    root.appendChild(card);
  });
}
if (typeof document !== 'undefined') {
  const root = document.getElementById('tools-root');
  if (root) renderTools(TOOL_CONFIG, root);
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { TOOL_CONFIG, buildToolUrl };
}
