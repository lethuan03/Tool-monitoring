const mysql = require('mysql2/promise');
const { log } = require('./logger');
const { hashPassword, checkNewPassword } = require('./auth/password');

// Connection settings come from env (.env locally, docker-compose in containers)
const config = {
  host: process.env.DB_HOST || 'localhost',
  port: Number(process.env.DB_PORT) || 3306,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME || 'ecommerce',
  waitForConnections: true,
  connectionLimit: Number(process.env.DB_POOL_SIZE) || 10,
  // Return DECIMAL columns as JS numbers (prices are used in arithmetic)
  decimalNumbers: true,
  timezone: 'Z'
};

const pool = mysql.createPool(config);

// --- Schema ---
const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS products (
    id          INT UNSIGNED  NOT NULL AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255)  NOT NULL,
    description TEXT          NOT NULL,
    price       DECIMAL(12,2) NOT NULL,
    stock       INT UNSIGNED  NOT NULL DEFAULT 0,
    image       VARCHAR(512)  NOT NULL DEFAULT ''
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,

  `CREATE TABLE IF NOT EXISTS orders (
    id          INT UNSIGNED  NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer    VARCHAR(255)  NOT NULL,
    email       VARCHAR(255)  NOT NULL,
    total       DECIMAL(14,2) NOT NULL,
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,

  `CREATE TABLE IF NOT EXISTS order_items (
    id          INT UNSIGNED  NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id    INT UNSIGNED  NOT NULL,
    product_id  INT UNSIGNED  NOT NULL,
    name        VARCHAR(255)  NOT NULL,
    price       DECIMAL(12,2) NOT NULL,
    quantity    INT UNSIGNED  NOT NULL,
    CONSTRAINT fk_items_order   FOREIGN KEY (order_id)   REFERENCES orders(id),
    CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products(id)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,

  // Tài khoản đăng nhập trang quản trị. Chỉ lưu hash mật khẩu (xem auth/password.js).
  // role để sẵn cho việc thêm vai trò sau này; hiện chỉ có 'admin'.
  `CREATE TABLE IF NOT EXISTS users (
    id            INT UNSIGNED  NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)   NOT NULL,
    password_hash VARCHAR(255)  NOT NULL,
    role          VARCHAR(20)   NOT NULL DEFAULT 'admin',
    created_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_users_username (username)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,

  // Phiên đăng nhập: chỉ lưu SHA-256 của token trong cookie (xem auth/session.js).
  // Xoá user thì phiên của user đó tự xoá theo (ON DELETE CASCADE).
  `CREATE TABLE IF NOT EXISTS sessions (
    token_hash  CHAR(64)      NOT NULL PRIMARY KEY,
    user_id     INT UNSIGNED  NOT NULL,
    expires_at  DATETIME      NOT NULL,
    created_at  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_sessions_user (user_id),
    KEY idx_sessions_expires (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`
];

// --- Migration: cột `active` cho chức năng XOÁ sản phẩm ở trang admin ---
// Không xoá hẳn dòng (DELETE) được, vì order_items có khoá ngoại trỏ tới
// products: sản phẩm đã từng bán sẽ bị MySQL chặn xoá, và nếu xoá được thì
// lịch sử đơn hàng mất tham chiếu. Nên "xoá" = đặt active = 0 (ẩn khỏi cửa
// hàng và admin), đơn hàng cũ vẫn nguyên.
// MySQL 8.0 không có "ADD COLUMN IF NOT EXISTS", nên hỏi information_schema
// trước. DB tạo mới hay DB cũ đã có dữ liệu đều chạy được, chạy lại nhiều lần
// cũng không sao.
async function migrate() {
  const [[{ c }]] = await pool.query(
    `SELECT COUNT(*) AS c FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products' AND COLUMN_NAME = 'active'`
  );
  if (c > 0) return;
  await pool.query('ALTER TABLE products ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1');
  log.info('db_migrated', { change: 'products.active' });
}

// --- Seed sample products on first run ---
async function seed() {
  const [[{ c }]] = await pool.query('SELECT COUNT(*) AS c FROM products');
  if (c > 0) return;

  const sample = [
    ['Wireless Headphones', 'Noise-cancelling over-ear headphones', 1290000, 25, 'https://picsum.photos/seed/headphones/400/300'],
    ['Mechanical Keyboard', 'RGB hot-swappable mechanical keyboard', 1890000, 15, 'https://picsum.photos/seed/keyboard/400/300'],
    ['USB-C Hub', '7-in-1 USB-C multiport adapter', 650000, 40, 'https://picsum.photos/seed/usbhub/400/300'],
    ['Webcam 1080p', 'Full HD webcam with microphone', 790000, 30, 'https://picsum.photos/seed/webcam/400/300'],
    ['Laptop Stand', 'Aluminium adjustable laptop stand', 450000, 50, 'https://picsum.photos/seed/stand/400/300'],
    ['Wireless Mouse', 'Ergonomic silent wireless mouse', 390000, 60, 'https://picsum.photos/seed/mouse/400/300']
  ];
  await pool.query('INSERT INTO products (name, description, price, stock, image) VALUES ?', [sample]);
  log.info('db_seeded', { products: sample.length });
}

// --- Tạo sẵn 1 tài khoản admin nếu DB chưa có admin nào ---
// Lấy từ ADMIN_USERNAME / ADMIN_PASSWORD (.env). CHỈ dùng ở lần tạo đầu:
// đã có admin thì bỏ qua, nên đổi ADMIN_PASSWORD trong .env sau đó KHÔNG đổi
// được mật khẩu — đổi trong trang quản trị (mục "Đổi mật khẩu").
async function seedAdmin() {
  const [[{ c }]] = await pool.query("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'");
  if (c > 0) return;

  const username = String(process.env.ADMIN_USERNAME || 'admin').trim();
  const password = process.env.ADMIN_PASSWORD || '';
  if (!/^[A-Za-z0-9_.-]{3,50}$/.test(username)) {
    throw new Error('ADMIN_USERNAME chỉ gồm chữ, số, _ . - và dài 3-50 ký tự');
  }
  const problem = checkNewPassword(password);
  if (problem) throw new Error(`ADMIN_PASSWORD không hợp lệ: ${problem}`);

  await pool.query("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')", [
    username,
    await hashPassword(password),
  ]);
  // Không ghi mật khẩu ra log
  log.info('admin_seeded', { username });
}

// --- Wait for MySQL to accept connections (container start order, restarts) ---
async function waitForDb(retries = Number(process.env.DB_CONNECT_RETRIES) || 30, delayMs = 2000) {
  for (let attempt = 1; ; attempt++) {
    try {
      await pool.query('SELECT 1');
      return;
    } catch (err) {
      if (attempt >= retries) throw err;
      log.warn('db_not_ready', {
        err_code: err.code || '',
        err_message: err.message,
        attempt,
        retries
      });
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
}

async function initDb() {
  await waitForDb();
  for (const stmt of SCHEMA) await pool.query(stmt);
  await migrate();
  await seed();
  await seedAdmin();
  log.info('db_connected', { host: config.host, port: config.port, database: config.database });
}

module.exports = { pool, initDb };
