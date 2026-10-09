const express = require('express');
const { pool } = require('../db');

const router = express.Router();

// Small wrapper so async errors reach Express' error handler
const asyncHandler = (fn) => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

// GET /api/products - list all products
router.get(
  '/',
  asyncHandler(async (req, res) => {
    const [rows] = await pool.query('SELECT * FROM products ORDER BY id');
    res.json(rows);
  })
);

// GET /api/products/:id - single product
router.get(
  '/:id',
  asyncHandler(async (req, res) => {
    const [rows] = await pool.query('SELECT * FROM products WHERE id = ?', [req.params.id]);
    if (rows.length === 0) return res.status(404).json({ error: 'Product not found' });
    res.json(rows[0]);
  })
);

// POST /api/products - create a product (simple admin helper)
router.post(
  '/',
  asyncHandler(async (req, res) => {
    const { name, description = '', price, stock = 0, image = '' } = req.body || {};
    if (!name || typeof price !== 'number') {
      return res.status(400).json({ error: 'name and numeric price are required' });
    }
    const [result] = await pool.query(
      'INSERT INTO products (name, description, price, stock, image) VALUES (?, ?, ?, ?, ?)',
      [name, description, price, stock, image]
    );
    const [rows] = await pool.query('SELECT * FROM products WHERE id = ?', [result.insertId]);
    res.status(201).json(rows[0]);
  })
);

module.exports = router;
