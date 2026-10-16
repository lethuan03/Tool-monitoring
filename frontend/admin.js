// ============================================================================
// Trang admin: thêm / sửa / xoá sản phẩm + mục Monitoring (link tool).
// Gọi API backend qua nginx: GET/POST /api/products, PUT/DELETE /api/products/:id
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

/* ---------- Gọi API ---------- */
async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Lỗi HTTP ${res.status}`);
  return data;
}

/* ---------- Thông báo ---------- */
let noticeTimer;
function notify(message, type = 'success') {
  const box = $('notice');
  box.replaceChildren();
  const alert = document.createElement('div');
  alert.className = `alert alert-${type}`;
  alert.textContent = message;
  box.appendChild(alert);
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => box.replaceChildren(), 4000);
}

/* ---------- Tạo phần tử nhanh ---------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------- Danh sách sản phẩm ---------- */
async function loadProducts() {
  try {
    products = await api('GET', API);
    renderStats();
    renderRows();
  } catch (err) {
    const tbody = $('productRows');
    tbody.replaceChildren();
    const tr = el('tr');
    const td = el('td', 'text-danger', `Không tải được sản phẩm: ${err.message}`);
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

function renderStats() {
  $('statCount').textContent = fmtNum(products.length);
  $('statStock').textContent = fmtNum(products.reduce((s, p) => s + p.stock, 0));
  $('statLow').textContent = fmtNum(products.filter((p) => p.stock > 0 && p.stock <= LOW_STOCK).length);
  $('statOut').textContent = fmtNum(products.filter((p) => p.stock <= 0).length);
}

function stockBadge(stock) {
  if (stock <= 0) return el('span', 'badge bg-red-lt', 'Hết hàng');
  if (stock <= LOW_STOCK) return el('span', 'badge bg-yellow-lt', `Sắp hết · ${fmtNum(stock)}`);
  return el('span', 'badge bg-green-lt', fmtNum(stock));
}

function renderRows() {
  const tbody = $('productRows');
  const q = $('search').value.trim().toLowerCase();
  const list = q ? products.filter((p) => p.name.toLowerCase().includes(q)) : products;

  tbody.replaceChildren();
  if (list.length === 0) {
    const tr = el('tr');
    const td = el('td', 'text-secondary', q ? 'Không có sản phẩm khớp.' : 'Chưa có sản phẩm. Bấm "Thêm sản phẩm".');
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const p of list) {
    const tr = el('tr');

    tr.appendChild(el('td', 'text-secondary', String(p.id)));

    // Ảnh + tên + mô tả
    const info = el('td');
    const wrap = el('div', 'd-flex align-items-center gap-3');
    if (p.image) {
      const img = el('img', 'admin-thumb');
      img.src = p.image;
      img.alt = '';
      img.loading = 'lazy';
      wrap.appendChild(img);
    } else {
      wrap.appendChild(el('span', 'admin-thumb admin-thumb-empty', '–'));
    }
    const texts = el('div');
    texts.appendChild(el('div', 'fw-semibold', p.name));
    if (p.description) texts.appendChild(el('div', 'text-secondary small', p.description));
    wrap.appendChild(texts);
    info.appendChild(wrap);
    tr.appendChild(info);

    tr.appendChild(el('td', 'text-end text-nowrap', fmtMoney(p.price)));

    const stockTd = el('td', 'text-end');
    stockTd.appendChild(stockBadge(p.stock));
    tr.appendChild(stockTd);

    // Nút thao tác
    const actions = el('td', 'text-nowrap');
    const editBtn = el('button', 'btn btn-sm btn-outline-primary me-1', 'Sửa');
    editBtn.type = 'button';
    editBtn.setAttribute('aria-label', `Sửa ${p.name}`);
    editBtn.addEventListener('click', () => openDialog(p));
    const delBtn = el('button', 'btn btn-sm btn-outline-danger', 'Xoá');
    delBtn.type = 'button';
    delBtn.setAttribute('aria-label', `Xoá ${p.name}`);
    delBtn.addEventListener('click', () => removeProduct(p));
    actions.append(editBtn, delBtn);
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
  if (/^https?:\/\//i.test(url)) {
    img.src = url;
    img.hidden = false;
  } else {
    img.removeAttribute('src');
    img.hidden = true;
  }
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

/* ---------- Chuyển mục theo #hash (Sản phẩm / Monitoring) ---------- */
function showSection() {
  const name = location.hash === '#monitoring' ? 'monitoring' : 'products';
  document.querySelectorAll('[data-section-panel]').forEach((panel) => {
    panel.hidden = panel.dataset.sectionPanel !== name;
  });
  document.querySelectorAll('[data-section]').forEach((link) => {
    const active = link.dataset.section === name;
    link.parentElement.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

/* ---------- Init ---------- */
$('addBtn').addEventListener('click', () => openDialog(null));
$('productForm').addEventListener('submit', saveProduct);
$('fImage').addEventListener('input', updatePreview);
$('fImagePreview').addEventListener('error', () => { $('fImagePreview').hidden = true; });
$('search').addEventListener('input', renderRows);
document.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', closeDialog));
// Bấm ra vùng tối bên ngoài hộp thoại cũng đóng
$('productDialog').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeDialog(); });
window.addEventListener('hashchange', showSection);

showSection();
loadProducts();
