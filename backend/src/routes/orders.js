const express = require('express');
const { pool } = require('../db');

const router = express.Router();

const asyncHandler = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

async function fetchOrderWithItems(executor, orderId) {
  const [[order]] = await executor.query('SELECT * FROM orders WHERE id = ?', [orderId]);
  if (!order) return null;
  const [items] = await executor.query(
    'SELECT product_id, name, price, quantity FROM order_items WHERE order_id = ?',
    [orderId]
  );
  order.items = items;
  return order;
}

// GET /api/orders - list orders with their items
router.get(
  '/',
  asyncHandler(async (req, res) => {
    const [orders] = await pool.query('SELECT * FROM orders ORDER BY id DESC');
    const result = [];
    for (const o of orders) {
      const [items] = await pool.query(
        'SELECT product_id, name, price, quantity FROM order_items WHERE order_id = ?',
        [o.id]
      );
      result.push({ ...o, items });
    }
    res.json(result);
  })
);

// GET /api/orders/:id - single order
router.get(
  '/:id',
  asyncHandler(async (req, res) => {
    const order = await fetchOrderWithItems(pool, req.params.id);
    if (!order) return res.status(404).json({ error: 'Order not found' });
    res.json(order);
  })
);

// POST /api/orders - place an order
// body: { customer, email, items: [{ productId, quantity }] }
router.post(
  '/',
  asyncHandler(async (req, res) => {
    const { customer, email, items } = req.body || {};

    if (!customer || !email) {
      return res.status(400).json({ error: 'customer and email are required' });
    }
    if (!Array.isArray(items) || items.length === 0) {
      return res.status(400).json({ error: 'items must be a non-empty array' });
    }

    const conn = await pool.getConnection();
    try {
      await conn.beginTransaction();

      // Validate stock + compute total, locking each product row (FOR UPDATE)
      const lineItems = [];
      let total = 0;
      for (const it of items) {
        // active = 1: sản phẩm đã bị xoá ở trang admin thì không mua được nữa
        // (giỏ hàng cũ trong localStorage của khách vẫn có thể còn nó)
        const [[product]] = await conn.query(
          'SELECT * FROM products WHERE id = ? AND active = 1 FOR UPDATE',
          [it.productId]
        );
        if (!product) {
          await conn.rollback();
          return res.status(400).json({ error: `Product ${it.productId} not found` });
        }
        const qty = Number(it.quantity);
        if (!Number.isInteger(qty) || qty <= 0) {
          await conn.rollback();
          return res.status(400).json({ error: `Invalid quantity for product ${it.productId}` });
        }
        if (product.stock < qty) {
          await conn.rollback();
          return res.status(409).json({ error: `Not enough stock for "${product.name}"` });
        }
        total += product.price * qty;
        lineItems.push({ product, qty });
      }

      const [orderResult] = await conn.query(
        'INSERT INTO orders (customer, email, total) VALUES (?, ?, ?)',
        [customer, email, total]
      );
      const orderId = orderResult.insertId;

      for (const { product, qty } of lineItems) {
        await conn.query(
          'INSERT INTO order_items (order_id, product_id, name, price, quantity) VALUES (?, ?, ?, ?, ?)',
          [orderId, product.id, product.name, product.price, qty]
        );
        await conn.query('UPDATE products SET stock = stock - ? WHERE id = ?', [qty, product.id]);
      }

      await conn.commit();

      const order = await fetchOrderWithItems(conn, orderId);
      res.status(201).json(order);
    } catch (err) {
      await conn.rollback();
      throw err;
    } finally {
      conn.release();
    }
  })
);

module.exports = router;
