// ============================================================================
// Mục "Monitoring" của trang admin (admin.html#monitoring) — mỗi tool một ô:
// logo + tên, bấm là mở tab mới.
//
// Các tool chỉ mở trên 127.0.0.1 của máy chạy Docker (xem docker-compose.yml),
// nên link luôn trỏ về `localhost`. Trên EC2 thì bật SSH tunnel trước
// (README mục 7.10).
//
// Logo trong frontend/icons/ lấy từ chính các tool (Grafana, Prometheus,
// Alloy, mailpit tự phục vụ) nên không phải tải từ nguồn ngoài.
// Loki không có giao diện riêng: xem log qua Grafana (dashboard Shop Logs).
//
// ĐỔI CỔNG TRONG .env THÌ PHẢI SỬA Ở ĐÂY RỒI BUILD LẠI FRONTEND:
//   docker compose up -d --build frontend
// ============================================================================

const TOOL_CONFIG = {
  host: 'localhost',
  ports: {
    grafana: 3001,      // .env: GRAFANA_PORT
    prometheus: 9090,   // cố định trong docker-compose.yml (127.0.0.1:9090)
    alertmanager: 9093, // cố định trong docker-compose.yml (127.0.0.1:9093)
    alloy: 12345,       // .env: ALLOY_PORT
    mailpit: 8025,      // cố định, chỉ có khi bật: docker compose --profile mail-test up -d
  },
  tools: [
    { name: 'Grafana', icon: 'icons/grafana.svg', port: 'grafana', path: '/' },
    { name: 'Prometheus', icon: 'icons/prometheus.svg', port: 'prometheus', path: '/query' },
    { name: 'Prometheus Targets', icon: 'icons/prometheus.svg', port: 'prometheus', path: '/targets' },
    { name: 'Prometheus Alerts', icon: 'icons/prometheus.svg', port: 'prometheus', path: '/alerts' },
    { name: 'Alertmanager', icon: 'icons/alertmanager.svg', port: 'alertmanager', path: '/' },
    { name: 'Alloy', icon: 'icons/alloy.ico', port: 'alloy', path: '/' },
    { name: 'Mailpit', icon: 'icons/mailpit.svg', port: 'mailpit', path: '/' },
  ],
};

// Tạo URL cho một tool. Hàm thuần (không đụng DOM) để test được bằng node:test.
function buildToolUrl(config, tool) {
  const rawPath = tool.path || '/';
  const path = rawPath.startsWith('/') ? rawPath : `/${rawPath}`;

  const port = config.ports[tool.port];
  if (!port) throw new Error(`Chưa khai báo cổng cho tool "${tool.name}" (ports.${tool.port})`);
  return `http://${config.host}:${port}${path}`;
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
