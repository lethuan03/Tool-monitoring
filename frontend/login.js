// Trang đăng nhập quản trị. Đăng nhập đúng -> backend đặt cookie sid (HttpOnly)
// -> chuyển sang /admin.html. JavaScript không đọc/không lưu token ở đâu cả.

const $ = (id) => document.getElementById(id);

function showError(message) {
  const box = $('loginError');
  box.textContent = message || '';
  box.classList.toggle('d-none', !message);
}

// Đã đăng nhập admin rồi thì vào thẳng trang quản trị
fetch('/api/auth/me', { cache: 'no-store' })
  .then((r) => (r.ok ? r.json() : null))
  .then((me) => {
    if (me && me.authenticated && me.role === 'admin') location.replace('/admin.html');
  })
  .catch(() => {});

// Nút Hiện/Ẩn mật khẩu
$('pwToggle').addEventListener('click', () => {
  const input = $('password');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  $('pwToggle').textContent = show ? 'Ẩn' : 'Hiện';
  $('pwToggle').setAttribute('aria-pressed', String(show));
  input.focus();
});

$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = $('username').value.trim();
  const password = $('password').value;
  if (!username || !password) return showError('Nhập tên đăng nhập và mật khẩu');

  const btn = $('loginBtn');
  btn.disabled = true;
  btn.textContent = 'Đang đăng nhập…';
  showError('');
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Lỗi HTTP ${res.status}`);
    if (data.role !== 'admin') throw new Error('Tài khoản này không có quyền quản trị');
    location.replace('/admin.html');
  } catch (err) {
    showError(err.message);
    $('password').value = '';
    $('password').focus();
  } finally {
    btn.disabled = false;
    btn.textContent = 'Đăng nhập';
  }
});
