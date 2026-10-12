// Local runs: load the project-root .env. In containers the file isn't present and env comes from compose.
require('dotenv').config({ path: require('path').resolve(__dirname, '../../.env') });

const express = require('express');
const cors = require('cors');

const { pool, initDb } = require('./db');
const { httpMetricsMiddleware, metricsHandler } = require('./metrics');
const productsRouter = require('./routes/products');
const ordersRouter = require('./routes/orders');

const app = express();
app.disable('x-powered-by'); // don't advertise the framework in response headers
const PORT = process.env.PORT || 3000;
const startedAt = Date.now();

// --- Middleware ---
app.use(httpMetricsMiddleware);
app.use(cors());
app.use(express.json());

// --- API routes ---
app.use('/api/products', productsRouter);
app.use('/api/orders', ordersRouter);

// --- Health check (for container orchestration / monitoring) ---
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptimeSeconds: Math.floor((Date.now() - startedAt) / 1000) });
});

// --- Prometheus metrics (scraped by Prometheus on the internal network only) ---
app.get('/metrics', metricsHandler);

// --- 404 for unknown API routes ---
app.use('/api', (req, res) => res.status(404).json({ error: 'Not found' }));

// --- Error handler ---
app.use((err, req, res, next) => {
  console.error('[error]', err);
  res.status(500).json({ error: 'Internal server error' });
});

// --- Bootstrap: wait for the database before accepting traffic ---
async function start() {
  try {
    await initDb();
    const server = app.listen(PORT, () => {
      console.log(`[server] e-commerce API running on http://localhost:${PORT}`);
      console.log(`[server] health:  http://localhost:${PORT}/health`);
      console.log(`[server] metrics: http://localhost:${PORT}/metrics`);
    });

    // Graceful shutdown: `docker stop` sends SIGTERM
    const shutdown = (signal) => {
      console.log(`[server] ${signal} received, shutting down`);
      server.close(async () => {
        await pool.end().catch(() => {});
        process.exit(0);
      });
      setTimeout(() => process.exit(1), 10000).unref();
    };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
  } catch (err) {
    console.error('[server] failed to start:', err.message);
    process.exit(1);
  }
}

start();
