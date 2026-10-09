const express = require('express');
const cors = require('cors');

const { initDb } = require('./db');
const productsRouter = require('./routes/products');
const ordersRouter = require('./routes/orders');

const app = express();
const PORT = process.env.PORT || 3000;

// --- Middleware ---
app.use(cors());
app.use(express.json());

// --- Lightweight in-memory metrics (ready for a monitoring tool to scrape) ---
const metrics = {
  startedAt: Date.now(),
  requests: 0,
  byStatus: {}
};

app.use((req, res, next) => {
  metrics.requests += 1;
  res.on('finish', () => {
    metrics.byStatus[res.statusCode] = (metrics.byStatus[res.statusCode] || 0) + 1;
  });
  next();
});

// --- API routes ---
app.use('/api/products', productsRouter);
app.use('/api/orders', ordersRouter);

// --- Health check (for container orchestration / monitoring) ---
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptimeSeconds: Math.floor((Date.now() - metrics.startedAt) / 1000) });
});

// --- Prometheus-style metrics endpoint (plain text) ---
app.get('/metrics', (req, res) => {
  const uptime = Math.floor((Date.now() - metrics.startedAt) / 1000);
  let body = '';
  body += '# HELP app_uptime_seconds Process uptime in seconds\n';
  body += '# TYPE app_uptime_seconds gauge\n';
  body += `app_uptime_seconds ${uptime}\n`;
  body += '# HELP app_http_requests_total Total HTTP requests received\n';
  body += '# TYPE app_http_requests_total counter\n';
  body += `app_http_requests_total ${metrics.requests}\n`;
  body += '# HELP app_http_responses_total HTTP responses by status code\n';
  body += '# TYPE app_http_responses_total counter\n';
  for (const [code, count] of Object.entries(metrics.byStatus)) {
    body += `app_http_responses_total{status="${code}"} ${count}\n`;
  }
  res.set('Content-Type', 'text/plain; version=0.0.4').send(body);
});

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
    app.listen(PORT, () => {
      console.log(`[server] e-commerce API running on http://localhost:${PORT}`);
      console.log(`[server] health:  http://localhost:${PORT}/health`);
      console.log(`[server] metrics: http://localhost:${PORT}/metrics`);
    });
  } catch (err) {
    console.error('[server] failed to start:', err.message);
    process.exit(1);
  }
}

start();
