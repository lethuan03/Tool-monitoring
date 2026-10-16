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
    box.innerHTML = `<p class="muted">Không tải được sản phẩm: ${escapeHtml(err.message)}</p>`;
  }
}

function stockLabel(stock) {
  if (stock <= 0) return '<span class="stock out">Hết hàng</span>';
  if (stock <= 5) return `<span class="stock low">Sắp hết · còn ${stock}</span>`;
  return `<span class="stock">Còn ${stock} sản phẩm</span>`;
}

function renderProducts() {
  const box = el('products-grid');
  if (products.length === 0) {
    box.innerHTML = '<p class="muted">Chưa có sản phẩm.</p>';
    return;
  }
  box.innerHTML = products
    .map(
      (p) => `
      <article class="card">
        <div class="card-media">
          <img src="${escapeHtml(p.image)}" alt="${escapeHtml(p.name)}" loading="lazy" />
          <span class="card-tag">NEW</span>
        </div>
        <div class="card-body">
          <h3>${escapeHtml(p.name)}</h3>
          <p class="desc">${escapeHtml(p.description)}</p>
          <div class="card-row">
            <span class="price">${fmt(p.price)}</span>
            ${stockLabel(p.stock)}
          </div>
          <button class="add-btn" data-id="${p.id}" ${p.stock <= 0 ? 'disabled' : ''}>
            ${p.stock <= 0 ? 'Hết hàng' : '＋ Thêm vào giỏ'}
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
  el('cartCount').textContent = items.reduce((s, e) => s + e.qty, 0);

  const box = el('cartItems');
  if (items.length === 0) {
    box.innerHTML = '<div class="empty-cart">Giỏ hàng của bạn đang trống 🛍️</div>';
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
            <button data-dec="${e.product.id}">−</button>
            <span>${e.qty}</span>
            <button data-inc="${e.product.id}">+</button>
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

loadProducts();
renderCart();
