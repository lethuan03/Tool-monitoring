const path = require('path');
const fs = require('fs');
const Database = require('better-sqlite3');

// Database file location (can be overridden for Docker volume mounts)
const DB_DIR = process.env.DB_DIR || path.join(__dirname, '..', 'data');
if (!fs.existsSync(DB_DIR)) {
  fs.mkdirSync(DB_DIR, { recursive: true });
}

const db = new Database(path.join(DB_DIR, 'ecommerce.db'));
db.pragma('journal_mode = WAL');

// --- Schema ---
db.exec(`
  CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    price       REAL    NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    image       TEXT    NOT NULL DEFAULT ''
  );

  CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer    TEXT    NOT NULL,
    email       TEXT    NOT NULL,
    total       REAL    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    quantity    INTEGER NOT NULL,
    FOREIGN KEY (order_id)   REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
  );
`);

// --- Seed sample products on first run ---
function seed() {
  const count = db.prepare('SELECT COUNT(*) AS c FROM products').get().c;
  if (count > 0) return;

  const insert = db.prepare(
    'INSERT INTO products (name, description, price, stock, image) VALUES (?, ?, ?, ?, ?)'
  );
  const sample = [
    ['Wireless Headphones', 'Noise-cancelling over-ear headphones', 1290000, 25, 'https://picsum.photos/seed/headphones/400/300'],
    ['Mechanical Keyboard', 'RGB hot-swappable mechanical keyboard', 1890000, 15, 'https://picsum.photos/seed/keyboard/400/300'],
    ['USB-C Hub', '7-in-1 USB-C multiport adapter', 650000, 40, 'https://picsum.photos/seed/usbhub/400/300'],
    ['Webcam 1080p', 'Full HD webcam with microphone', 790000, 30, 'https://picsum.photos/seed/webcam/400/300'],
    ['Laptop Stand', 'Aluminium adjustable laptop stand', 450000, 50, 'https://picsum.photos/seed/stand/400/300'],
    ['Wireless Mouse', 'Ergonomic silent wireless mouse', 390000, 60, 'https://picsum.photos/seed/mouse/400/300']
  ];
  const tx = db.transaction((rows) => {
    for (const r of rows) insert.run(...r);
  });
  tx(sample);
  console.log(`[db] seeded ${sample.length} sample products`);
}

seed();

module.exports = db;
