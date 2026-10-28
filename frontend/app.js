const API = '/api';

// Cart stored in localStorage: { [productId]: { product, qty } }
let cart = loadCart();
let products = [];

const el = (id) => document.getElementById(id);
const fmt = (n) => n.toLocaleString('vi-VN') + ' ₫';

function loadCart() {
  try {
    return JSON.parse(localStorage.getItem('cart')) || {};
  } catch {
    return {};
  }
}
function saveCart() {
  localStorage.setItem('cart', JSON.stringify(cart));
}

/* ---------- Toast ---------- */
let toastTimer;
function toast(message, type = 'ok') {
  const t = el('toast');
  t.textContent = message;
  t.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = 'toast'), 2600);
}

/* ---------- Load products ---------- */
async function loadProducts() {
  const box = el('products-grid');
  try {
    const res = await fetch(`${API}/products`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    products = await res.json();
    renderProducts();
  } catch (err) {
    box.innerHTML = `<p class="empty-state">Không tải được sản phẩm: ${escapeHtml(err.message)}</p>`;
    el('productCount').textContent = '';
  }
}

// Biểu tượng giỏ hàng nhỏ trên nút "Thêm vào giỏ" (SVG tĩnh, không chứa dữ liệu)
const CART_ICON =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6h15l-1.5 9h-12z"/><path d="M6 6 5 3H2"/><circle cx="9" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/></svg>';

function stockLabel(stock) {
  if (stock <= 0) return '<span class="stock out">Hết hàng</span>';
  if (stock <= 5) return `<span class="stock low">Còn ${stock}</span>`;
  return `<span class="stock">Còn hàng</span>`;
}

// Nhãn nổi trên ảnh: chỉ hiện khi sắp hết / hết hàng
function stockTag(stock) {
  if (stock <= 0) return '<span class="card-tag out">Hết hàng</span>';
  if (stock <= 5) return '<span class="card-tag low">Sắp hết</span>';
  return '';
}

function renderProducts() {
  const box = el('products-grid');
  el('productCount').textContent = products.length ? `${products.length} sản phẩm` : '';
  if (products.length === 0) {
    box.innerHTML = '<p class="empty-state">Chưa có sản phẩm.</p>';
    return;
  }
  box.innerHTML = products
    .map(
      (p) => `
      <article class="card">
        <div class="card-media">
          <img src="${escapeHtml(p.image)}" alt="${escapeHtml(p.name)}" loading="lazy" />
          ${stockTag(p.stock)}
        </div>
        <div class="card-body">
          <h3>${escapeHtml(p.name)}</h3>
          <p class="desc">${escapeHtml(p.description)}</p>
          <div class="card-row">
            <span class="price">${fmt(p.price)}</span>
            ${stockLabel(p.stock)}
          </div>
          <button class="add-btn" data-id="${p.id}" ${p.stock <= 0 ? 'disabled' : ''}
                  aria-label="Thêm ${escapeHtml(p.name)} vào giỏ">
            ${p.stock <= 0 ? 'Hết hàng' : `${CART_ICON} Thêm vào giỏ`}
          </button>
        </div>
      </article>`
    )
    .join('');

  box.querySelectorAll('.add-btn').forEach((btn) => {
    btn.addEventListener('click', () => addToCart(Number(btn.dataset.id)));
  });
}

/* ---------- Cart ops ---------- */
function addToCart(id) {
  const product = products.find((p) => p.id === id);
  if (!product) return;
  const current = cart[id]?.qty || 0;
  if (current + 1 > product.stock) {
    toast('Đã đạt số lượng tồn kho', 'err');
    return;
  }
  cart[id] = { product, qty: current + 1 };
  saveCart();
  renderCart();
  toast(`Đã thêm "${product.name}"`);
}

function changeQty(id, delta) {
  const entry = cart[id];
  if (!entry) return;
  const next = entry.qty + delta;
  if (next <= 0) {
    delete cart[id];
  } else if (next <= entry.product.stock) {
    entry.qty = next;
  } else {
    toast('Vượt quá tồn kho', 'err');
    return;
  }
  saveCart();
  renderCart();
}

function removeFromCart(id) {
  delete cart[id];
  saveCart();
  renderCart();
}

function cartTotal() {
  return Object.values(cart).reduce((s, e) => s + e.product.price * e.qty, 0);
}

function renderCart() {
  const items = Object.values(cart);
  const count = items.reduce((s, e) => s + e.qty, 0);
  const badge = el('cartCount');
  if (badge.textContent !== String(count)) {
    badge.textContent = count;
    // Rung nhẹ con số khi giỏ thay đổi
    badge.classList.remove('bump');
    void badge.offsetWidth;
    badge.classList.add('bump');
  }

  const box = el('cartItems');
  if (items.length === 0) {
    box.innerHTML = `<div class="empty-cart">
      <svg viewBox="0 0 24 24" width="56" height="56" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6h15l-1.5 9h-12z"/><path d="M6 6 5 3H2"/><circle cx="9" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/></svg>
      <p>Giỏ hàng của bạn đang trống</p>
    </div>`;
  } else {
    box.innerHTML = items
      .map(
        (e) => `
        <div class="cart-row">
          <img src="${escapeHtml(e.product.image)}" alt="${escapeHtml(e.product.name)}" />
          <div class="info">
            <div class="n">${escapeHtml(e.product.name)}</div>
            <div class="p">${fmt(e.product.price)}</div>
            <button class="remove" data-id="${e.product.id}">Xoá</button>
          </div>
          <div class="qty">
            <button data-dec="${e.product.id}" aria-label="Giảm số lượng">−</button>
            <span>${e.qty}</span>
            <button data-inc="${e.product.id}" aria-label="Tăng số lượng">+</button>
          </div>
        </div>`
      )
      .join('');

    box.querySelectorAll('[data-inc]').forEach((b) =>
      b.addEventListener('click', () => changeQty(Number(b.dataset.inc), 1))
    );
    box.querySelectorAll('[data-dec]').forEach((b) =>
      b.addEventListener('click', () => changeQty(Number(b.dataset.dec), -1))
    );
    box.querySelectorAll('.remove').forEach((b) =>
      b.addEventListener('click', () => removeFromCart(Number(b.dataset.id)))
    );
  }
  el('cartTotal').textContent = fmt(cartTotal());
}

/* ---------- Checkout ---------- */
async function checkout(e) {
  e.preventDefault();
  const items = Object.values(cart).map((x) => ({ productId: x.product.id, quantity: x.qty }));
  if (items.length === 0) {
    toast('Giỏ hàng đang trống', 'err');
    return;
  }

  const payload = {
    customer: el('customer').value.trim(),
    email: el('email').value.trim(),
    items
  };

  const btn = e.submitter;
  if (btn) { btn.disabled = true; btn.textContent = 'Đang xử lý...'; }

  try {
    const res = await fetch(`${API}/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Đặt hàng thất bại');

    toast(`Đặt hàng thành công! Đơn #${data.id} · ${fmt(data.total)}`);
    cart = {};
    saveCart();
    renderCart();
    el('checkoutForm').reset();
    closeCart();
    loadProducts();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Đặt hàng ngay'; }
  }
}

/* ---------- Drawer ---------- */
function openCart() {
  el('cartDrawer').classList.add('open');
  el('cartDrawer').setAttribute('aria-hidden', 'false');
  el('overlay').classList.remove('hidden');
}
function closeCart() {
  el('cartDrawer').classList.remove('open');
  el('cartDrawer').setAttribute('aria-hidden', 'true');
  el('overlay').classList.add('hidden');
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* ---------- Init ---------- */
el('cartBtn').addEventListener('click', openCart);
el('closeCart').addEventListener('click', closeCart);
el('overlay').addEventListener('click', closeCart);
el('checkoutForm').addEventListener('submit', checkout);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeCart(); });

/* ---------- Nút đăng nhập trên navbar ---------- */
// Khách: "Đăng nhập". Admin đã đăng nhập: "Quản trị" + "Đăng xuất".
// /api/auth/me luôn trả 200 nên không làm tăng số lỗi 4xx trên dashboard.
async function updateAuthNav() {
  try {
    const res = await fetch('/api/auth/me', { cache: 'no-store' });
    const me = res.ok ? await res.json() : null;
    if (!me || !me.authenticated || me.role !== 'admin') return;
    const link = el('navAuth');
    link.textContent = 'Quản trị';
    link.href = '/admin.html';
    el('navLogout').hidden = false;
  } catch {
    // Lỗi mạng: giữ nguyên nút "Đăng nhập"
  }
}

el('navLogout').addEventListener('click', async () => {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
  } finally {
    location.reload();
  }
});

loadProducts();
renderCart();
updateAuthNav();
