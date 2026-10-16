const express = require('express');
const { pool } = require('../db');
const { validateProduct, parseId } = require('../validate-product');

const router = express.Router();

// Small wrapper so async errors reach Express' error handler
const asyncHandler = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// Cột trả về cho client. Không trả `active`: sản phẩm đã xoá (active = 0)
// không bao giờ xuất hiện ở đây.
const COLS = 'id, name, description, price, stock, image';

async function findActive(id) {
  const [rows] = await pool.query(`SELECT ${COLS} FROM products WHERE id = ? AND active = 1`, [id]);
  return rows[0] || null;
}

// LƯU Ý BẢO MẬT: POST/PUT/DELETE chưa có đăng nhập / phân quyền (trang admin
// đang để mở theo yêu cầu). Chạy trên EC2 public thì ai cũng sửa/xoá được
// sản phẩm. Phải thêm xác thực trước khi dùng thật.

// GET /api/products - list all (active) products
router.get(
  '/',
  asyncHandler(async (req, res) => {
    const [rows] = await pool.query(`SELECT ${COLS} FROM products WHERE active = 1 ORDER BY id`);
    res.json(rows);
  })
);

// GET /api/products/:id - single product
router.get(
  '/:id',
  asyncHandler(async (req, res) => {
    const id = parseId(req.params.id);
    const product = id && (await findActive(id));
    if (!product) return res.status(404).json({ error: 'Product not found' });
    res.json(product);
  })
);

// POST /api/products - create a product (trang admin)
router.post(
  '/',
  asyncHandler(async (req, res) => {
    const { value, error } = validateProduct(req.body);
    if (error) return res.status(400).json({ error });

    const [result] = await pool.query(
      'INSERT INTO products (name, description, price, stock, image) VALUES (?, ?, ?, ?, ?)',
      [value.name, value.description, value.price, value.stock, value.image]
    );
    res.status(201).json(await findActive(result.insertId));
  })
);

// PUT /api/products/:id - sửa toàn bộ thông tin sản phẩm (trang admin)
router.put(
  '/:id',
  asyncHandler(async (req, res) => {
    const id = parseId(req.params.id);
    if (!id) return res.status(404).json({ error: 'Product not found' });

    const { value, error } = validateProduct(req.body);
    if (error) return res.status(400).json({ error });

    const [result] = await pool.query(
      'UPDATE products SET name = ?, description = ?, price = ?, stock = ?, image = ? WHERE id = ? AND active = 1',
      [value.name, value.description, value.price, value.stock, value.image, id]
    );
    if (result.affectedRows === 0) return res.status(404).json({ error: 'Product not found' });
    res.json(await findActive(id));
  })
);

// DELETE /api/products/:id - "xoá" sản phẩm (trang admin)
// Xoá mềm: active = 0. Lý do xem migrate() trong db.js.
router.delete(
  '/:id',
  asyncHandler(async (req, res) => {
    const id = parseId(req.params.id);
    if (!id) return res.status(404).json({ error: 'Product not found' });

    const [result] = await pool.query('UPDATE products SET active = 0 WHERE id = ? AND active = 1', [id]);
    if (result.affectedRows === 0) return res.status(404).json({ error: 'Product not found' });
    res.status(204).end();
  })
);

module.exports = router;
