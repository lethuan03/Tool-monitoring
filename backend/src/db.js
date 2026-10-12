const path = require('path');
// Load DB config from the project-root .env for local (non-Docker) runs.
// In Docker this file is absent and env vars come from docker-compose, so
// dotenv simply does nothing (and never overrides already-set env vars).
require('dotenv').config({ path: path.join(__dirname, '..', '..', '.env') });

const mysql = require('mysql2/promise');

const CONFIG = {
  host: process.env.DB_HOST || 'localhost',
  port: Number(process.env.DB_PORT) || 3306,
  user: process.env.DB_USER || 'root',
  password: process.env.DB_PASSWORD || 'root',
  database: process.env.DB_NAME || 'ecommerce'
};

// Connection pool reused across the whole app.
// decimalNumbers: true -> return DECIMAL columns (price, total) as JS numbers
// instead of strings, so the frontend/maths work directly.
const pool = mysql.createPool({
  ...CONFIG,
  waitForConnections: true,
  connectionLimit: 10,
  queueLimit: 0,
  decimalNumbers: true
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Best-effort: make sure the target database exists (useful for local dev).
// In Docker the MySQL image already creates it via MYSQL_DATABASE, and an
// app user may lack CREATE privilege -> ignore errors in that case.
async function ensureDatabase() {
  try {
    const conn = await mysql.createConnection({
      host: CONFIG.host,
      port: CONFIG.port,
      user: CONFIG.user,
      password: CONFIG.password
    });
    await conn.query(`CREATE DATABASE IF NOT EXISTS \`${CONFIG.database}\``);
    await conn.end();
  } catch (_) {
    // ignore - database probably already exists / no privilege
  }
}

async function createSchema() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS products (
      id          INT AUTO_INCREMENT PRIMARY KEY,
      name        VARCHAR(255)  NOT NULL,
      description VARCHAR(1000) NOT NULL DEFAULT '',
      price       DECIMAL(12,2) NOT NULL,
      stock       INT           NOT NULL DEFAULT 0,
      image       VARCHAR(500)  NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS orders (
      id          INT AUTO_INCREMENT PRIMARY KEY,
      customer    VARCHAR(255)  NOT NULL,
      email       VARCHAR(255)  NOT NULL,
      total       DECIMAL(12,2) NOT NULL,
      created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
  `);

  await pool.query(`
    CREATE TABLE IF NOT EXISTS order_items (
      id          INT AUTO_INCREMENT PRIMARY KEY,
      order_id    INT           NOT NULL,
      product_id  INT           NOT NULL,
      name        VARCHAR(255)  NOT NULL,
      price       DECIMAL(12,2) NOT NULL,
      quantity    INT           NOT NULL,
      FOREIGN KEY (order_id)   REFERENCES orders(id),
      FOREIGN KEY (product_id) REFERENCES products(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
  `);
}

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

  await pool.query(
    'INSERT INTO products (name, description, price, stock, image) VALUES ?',
    [sample]
  );
  console.log(`[db] seeded ${sample.length} sample products`);
}

// Initialise DB with retry so the app can wait for MySQL to be ready
// (MySQL containers take a few seconds to accept connections).
async function initDb() {
  const maxAttempts = 30;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await ensureDatabase();
      await pool.query('SELECT 1');
      await createSchema();
      await seed();
      console.log(`[db] connected to MySQL at ${CONFIG.host}:${CONFIG.port}/${CONFIG.database}`);
      return;
    } catch (err) {
      if (attempt === maxAttempts) {
        console.error('[db] could not connect to MySQL:', err.message);
        throw err;
      }
      console.log(`[db] waiting for MySQL... (${attempt}/${maxAttempts}) ${err.code || err.message}`);
      await sleep(3000);
    }
  }
}

module.exports = { pool, initDb };
