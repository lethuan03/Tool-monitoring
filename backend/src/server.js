// Local runs: load the project-root .env. In containers the file isn't present and env comes from compose.
require('dotenv').config({ path: require('path').resolve(__dirname, '../../.env') });

const express = require('express');
const cors = require('cors');

const { pool, initDb } = require('./db');
const { httpMetricsMiddleware, metricsHandler } = require('./metrics');
const { log, requestLogger } = require('./logger');
const { stampMount } = require('./http-route');
const productsRouter = require('./routes/products');
const ordersRouter = require('./routes/orders');

const app = express();
app.disable('x-powered-by'); // don't advertise the framework in response headers
const PORT = process.env.PORT || 3000;
const startedAt = Date.now();

// --- Middleware ---
// Đặt trước mọi thứ: metrics đếm số, requestLogger ghi log cho CÙNG một request
app.use(httpMetricsMiddleware);
app.use(requestLogger);
app.use(cors());
app.use(express.json());

// --- API routes ---
// stampMount: ghi sẵn đường dẫn mount vào req để nhãn `route` của metrics/log
// vẫn đúng khi request bị lỗi (xem giải thích trong src/http-route.js)
app.use('/api/products', stampMount('/api/products'), productsRouter);
app.use('/api/orders', stampMount('/api/orders'), ordersRouter);

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
  // Log kèm ngữ cảnh để tra được trên Grafana: lỗi ở route nào, mã lỗi gì
  log.error('unhandled_error', {
    method: req.method,
    path: req.originalUrl,
    err_message: err.message,
    err_code: err.code || '',
    stack: err.stack
  });
  res.status(500).json({ error: 'Internal server error' });
});

// --- Bootstrap: wait for the database before accepting traffic ---
async function start() {
  try {
    await initDb();
    const server = app.listen(PORT, () => {
      log.info('server_started', { port: Number(PORT), health: '/health', metrics: '/metrics' });
    });

    // Graceful shutdown: `docker stop` sends SIGTERM
    const shutdown = (signal) => {
      log.info('shutdown_started', { signal });
      server.close(async () => {
        await pool.end().catch(() => {});
        log.info('shutdown_complete', { signal });
        process.exit(0);
      });
      setTimeout(() => process.exit(1), 10000).unref();
    };
    process.on('SIGTERM', shutdown);
    process.on('SIGINT', shutdown);
  } catch (err) {
    log.error('server_start_failed', { err_message: err.message, stack: err.stack });
    process.exit(1);
  }
}

start();
