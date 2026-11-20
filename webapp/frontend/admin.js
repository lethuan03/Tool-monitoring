// ============================================================================
// Trang admin: thêm / sửa / xoá sản phẩm + mục Monitoring (link tool) + đổi mật khẩu.
// Gọi API backend qua nginx: GET/POST /api/products, PUT/DELETE /api/products/:id
// Cần đăng nhập admin: cookie sid (HttpOnly) trình duyệt tự gửi kèm, JS không đụng tới.
// Mọi dữ liệu từ server được gắn vào trang bằng textContent (không innerHTML)
// để tên / mô tả sản phẩm không chèn được HTML hay script.
// ============================================================================

const API = '/api/products';
const LOW_STOCK = 5; // khớp với nhãn "Sắp hết" ở trang shop (app.js)

const $ = (id) => document.getElementById(id);
const fmtMoney = (n) => Number(n).toLocaleString('vi-VN') + ' ₫';
const fmtNum = (n) => Number(n).toLocaleString('vi-VN');

let products = [];
let editingId = null; // null = đang thêm mới

/* ---------- Icon SVG (theo bộ Tabler Icons, MIT) ----------
   Chuỗi path là hằng số viết sẵn ở đây, không chứa dữ liệu từ server. */
const ICONS = {
  package: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12 4 7.5M16 5.25 8 9.75"/>',
  activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M10.85 12.15 19 4M18 5l2 2M15 8l2 2"/>',
  store: '<path d="M3 21h18M3 7v1a3 3 0 0 0 6 0V7m0 1a3 3 0 0 0 6 0V7m0 1a3 3 0 0 0 6 0V7H3l2-4h14l2 4M5 21V10.85M19 21V10.85M9 21v-4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v4"/>',
  logout: '<path d="M14 8V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2-2v-2"/><path d="M9 12h12l-3-3M18 15l3-3"/>',
  external: '<path d="M12 6H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6M11 13l9-9M15 4h5v5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  boxes: '<path d="M3 7l9-4 9 4-9 4z"/><path d="M3 12l9 4 9-4M3 17l9 4 9-4"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.24 3.96 2.39 17.06A2 2 0 0 0 4.1 20h15.8a2 2 0 0 0 1.71-2.94L13.76 3.96a2 2 0 0 0-3.52 0z"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="M5.7 5.7l12.6 12.6"/>',
  search: '<circle cx="10" cy="10" r="7"/><path d="M21 21l-6-6"/>',
  photo: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  edit: '<path d="M4 20h4L18.5 9.5a2.83 2.83 0 0 0-4-4L4 16v4M13.5 6.5l4 4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/>',
  arrowUpRight: '<path d="M17 7 7 17M8 7h9v9"/>',
};

function createIcon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name] || '';
  return svg;
}

// Chèn icon vào mọi thẻ <... data-icon="tên"> trong trang (kể cả ô tool do tools.js vẽ)
function applyIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach((node) => {
    if (!node.querySelector('svg')) node.prepend(createIcon(node.dataset.icon));
  });
}

/* ---------- Gọi API ---------- */
async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  // Phiên hết hạn / bị đăng xuất ở máy khác -> quay về trang đăng nhập
  if (res.status === 401) {
    goToLogin();
    throw new Error('Phiên đăng nhập đã hết hạn');
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Lỗi HTTP ${res.status}`);
  return data;
}

function goToLogin() {
  location.replace('/login.html');
}

/* ---------- Tạo phần tử nhanh ---------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------- Đăng nhập / đăng xuất ---------- */
// Lớp kiểm tra thứ 2 (lớp 1 là nginx auth_request): không phải admin thì về login
async function requireLogin() {
  try {
    const res = await fetch('/api/auth/me', { cache: 'no-store' });
    const me = res.ok ? await res.json() : null;
    if (!me || !me.authenticated || me.role !== 'admin') {
      goToLogin();
      return false;
    }
    $('adminName').textContent = me.username;
    $('adminAvatar').textContent = me.username.slice(0, 1).toUpperCase();
    return true;
  } catch {
    goToLogin();
    return false;
  }
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } finally {
    goToLogin();
  }
}

/* ---------- Thông báo ---------- */
let noticeTimer;
function notify(message, type = 'success') {
  const box = $('notice');
  box.replaceChildren();
  box.appendChild(el('div', `alert alert-${type} alert-dismissible`, message));
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => box.replaceChildren(), 4000);
}

/* ---------- Danh sách sản phẩm ---------- */
function messageRow(text, className = 'text-secondary', iconName) {
  const tr = el('tr');
  const td = el('td', `admin-empty ${className}`);
  td.colSpan = 4;
  if (iconName) {
    const ic = el('div');
    ic.dataset.icon = iconName;
    ic.appendChild(createIcon(iconName));
    td.appendChild(ic);
  }
  td.appendChild(el('div', null, text));
  tr.appendChild(td);
  return tr;
}

async function loadProducts() {
  try {
    products = await api('GET', API);
    renderStats();
    renderRows();
  } catch (err) {
    $('productRows').replaceChildren(messageRow(`Không tải được sản phẩm: ${err.message}`, 'text-danger', 'alert'));
  }
}

function renderStats() {
  $('statCount').textContent = fmtNum(products.length);
  $('statStock').textContent = fmtNum(products.reduce((s, p) => s + p.stock, 0));
  $('statValue').textContent = 'Trị giá ' + fmtMoney(products.reduce((s, p) => s + p.price * p.stock, 0));
  $('statLow').textContent = fmtNum(products.filter((p) => p.stock > 0 && p.stock <= LOW_STOCK).length);
  $('statOut').textContent = fmtNum(products.filter((p) => p.stock <= 0).length);
}

function stockBadge(stock) {
  if (stock <= 0) return el('span', 'badge bg-red-lt', 'Hết hàng');
  if (stock <= LOW_STOCK) return el('span', 'badge bg-yellow-lt', `Sắp hết · ${fmtNum(stock)}`);
  return el('span', 'badge bg-green-lt', fmtNum(stock));
}

function iconButton(iconName, className, label, onClick) {
  const btn = el('button', `btn btn-icon ${className}`);
  btn.type = 'button';
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.appendChild(createIcon(iconName));
  btn.addEventListener('click', onClick);
  return btn;
}

function renderRows() {
  const tbody = $('productRows');
  const q = $('search').value.trim().toLowerCase();
  const list = q ? products.filter((p) => p.name.toLowerCase().includes(q)) : products;

  $('tableInfo').textContent = q
    ? `Tìm thấy ${fmtNum(list.length)} / ${fmtNum(products.length)} sản phẩm`
    : `${fmtNum(products.length)} sản phẩm`;

  tbody.replaceChildren();
  if (list.length === 0) {
    tbody.appendChild(messageRow(q ? 'Không có sản phẩm khớp.' : 'Chưa có sản phẩm. Bấm "Thêm sản phẩm".', 'text-secondary', 'package'));
    return;
  }

  for (const p of list) {
    const tr = el('tr');

    // Ảnh + tên + mã + mô tả
    const info = el('td');
    const wrap = el('div', 'admin-product');
    if (p.image) {
      const img = el('img', 'admin-thumb');
      img.src = p.image;
      img.alt = '';
      img.loading = 'lazy';
      wrap.appendChild(img);
    } else {
      const ph = el('span', 'admin-thumb admin-thumb-empty');
      ph.appendChild(createIcon('photo'));
      wrap.appendChild(ph);
    }
    const texts = el('div');
    texts.style.minWidth = '0';
    texts.appendChild(el('div', 'admin-product-name', p.name));
    texts.appendChild(el('div', 'admin-product-meta', `#${p.id}${p.description ? ' · ' + p.description : ''}`));
    wrap.appendChild(texts);
    info.appendChild(wrap);
    tr.appendChild(info);

    tr.appendChild(el('td', 'text-end admin-price', fmtMoney(p.price)));

    const stockTd = el('td', 'text-end');
    stockTd.appendChild(stockBadge(p.stock));
    tr.appendChild(stockTd);

    const actions = el('td');
    const box = el('div', 'admin-actions');
    box.append(
      iconButton('edit', 'btn-ghost-primary', `Sửa ${p.name}`, () => openDialog(p)),
      iconButton('trash', 'btn-ghost-danger', `Xoá ${p.name}`, () => removeProduct(p))
    );
    actions.appendChild(box);
    tr.appendChild(actions);

    tbody.appendChild(tr);
  }
}

/* ---------- Hộp thoại thêm / sửa ---------- */
function showFormError(message) {
  const box = $('formError');
  box.textContent = message || '';
  box.classList.toggle('d-none', !message);
}

function updatePreview() {
  const url = $('fImage').value.trim();
  const img = $('fImagePreview');
  const ok = /^https?:\/\//i.test(url);
  if (ok) img.src = url;
  else img.removeAttribute('src');
  img.hidden = !ok;
  $('fImageEmpty').hidden = ok;
}

function openDialog(product) {
  editingId = product ? product.id : null;
  $('dialogTitle').textContent = product ? `Sửa sản phẩm #${product.id}` : 'Thêm sản phẩm';
  $('fName').value = product ? product.name : '';
  $('fPrice').value = product ? product.price : '';
  $('fStock').value = product ? product.stock : '';
  $('fImage').value = product ? product.image : '';
  $('fDesc').value = product ? product.description : '';
  showFormError('');
  updatePreview();
  $('productDialog').showModal();
  $('fName').focus();
}

function closeDialog() {
  $('productDialog').close();
}

function readForm() {
  const priceRaw = $('fPrice').value.trim();
  const stockRaw = $('fStock').value.trim();
  return {
    name: $('fName').value.trim(),
    price: priceRaw === '' ? NaN : Number(priceRaw),
    stock: stockRaw === '' ? NaN : Number(stockRaw),
    image: $('fImage').value.trim(),
    description: $('fDesc').value.trim(),
  };
}

// Kiểm tra sơ bộ trên trình duyệt cho nhanh; backend vẫn kiểm tra lại (validate-product.js)
function checkForm(data) {
  if (!data.name) return 'Tên sản phẩm không được để trống';
  if (!Number.isFinite(data.price) || data.price < 0) return 'Giá phải là số từ 0 trở lên';
  if (!Number.isInteger(data.stock) || data.stock < 0) return 'Tồn kho phải là số nguyên từ 0 trở lên';
  if (data.image && !/^https?:\/\//i.test(data.image)) return 'Link ảnh phải bắt đầu bằng http:// hoặc https://';
  return '';
}

async function saveProduct(e) {
  e.preventDefault();
  const data = readForm();
  const problem = checkForm(data);
  if (problem) return showFormError(problem);

  const btn = $('saveBtn');
  btn.disabled = true;
  btn.textContent = 'Đang lưu…';
  try {
    if (editingId === null) {
      const created = await api('POST', API, data);
      notify(`Đã thêm "${created.name}"`);
    } else {
      const updated = await api('PUT', `${API}/${editingId}`, data);
      notify(`Đã lưu "${updated.name}"`);
    }
    closeDialog();
    await loadProducts();
  } catch (err) {
    showFormError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Lưu';
  }
}

async function removeProduct(p) {
  if (!window.confirm(`Xoá "${p.name}"?\nSản phẩm sẽ biến mất khỏi cửa hàng. Đơn hàng cũ vẫn giữ nguyên.`)) return;
  try {
    await api('DELETE', `${API}/${p.id}`);
    notify(`Đã xoá "${p.name}"`);
    await loadProducts();
  } catch (err) {
    notify(`Không xoá được "${p.name}": ${err.message}`, 'danger');
  }
}

/* ---------- Đổi mật khẩu ---------- */
function showPasswordMsg(message, type) {
  const box = $('passwordMsg');
  box.replaceChildren();
  if (!message) return;
  box.appendChild(el('div', `alert alert-${type}`, message));
}

async function changePassword(e) {
  e.preventDefault();
  const currentPassword = $('pCurrent').value;
  const newPassword = $('pNew').value;
  if (!currentPassword) return showPasswordMsg('Nhập mật khẩu hiện tại', 'danger');
  if (newPassword.length < 12) return showPasswordMsg('Mật khẩu mới tối thiểu 12 ký tự', 'danger');
  if (newPassword !== $('pConfirm').value) return showPasswordMsg('Hai lần nhập mật khẩu mới không khớp', 'danger');

  const btn = $('passwordBtn');
  btn.disabled = true;
  try {
    await api('POST', '/api/auth/password', { currentPassword, newPassword });
    $('passwordForm').reset();
    showPasswordMsg('Đã đổi mật khẩu. Các máy khác đã bị đăng xuất.', 'success');
  } catch (err) {
    showPasswordMsg(err.message, 'danger');
  } finally {
    btn.disabled = false;
  }
}

/* ---------- Chuyển mục theo #hash (Sản phẩm / Monitoring / Đổi mật khẩu) ---------- */
const SECTIONS = { products: 'Sản phẩm', monitoring: 'Monitoring', account: 'Đổi mật khẩu' };
function showSection() {
  const fromHash = location.hash.slice(1);
  const name = fromHash in SECTIONS ? fromHash : 'products';
  document.querySelectorAll('[data-section-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.sectionPanel !== name;
  });
  document.querySelectorAll('[data-section]').forEach((link) => {
    const active = link.dataset.section === name;
    link.parentElement.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.title = `${SECTIONS[name]} — Quản trị Nova Store`;
}

/* ---------- Init ---------- */
applyIcons();
$('addBtn').addEventListener('click', () => openDialog(null));
$('productForm').addEventListener('submit', saveProduct);
$('fImage').addEventListener('input', updatePreview);
$('fImagePreview').addEventListener('error', () => {
  $('fImagePreview').hidden = true;
  $('fImageEmpty').hidden = false;
});
$('search').addEventListener('input', renderRows);
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeDialog));
// Bấm ra vùng tối bên ngoài hộp thoại cũng đóng
$('productDialog').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeDialog(); });
window.addEventListener('hashchange', showSection);
$('logoutBtn').addEventListener('click', logout);
$('passwordForm').addEventListener('submit', changePassword);

showSection();
requireLogin().then((ok) => { if (ok) loadProducts(); });
