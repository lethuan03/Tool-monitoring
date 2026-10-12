const client = require('prom-client');
const { routeTemplate } = require('./http-route');

// Process / Node.js runtime metrics: CPU, memory, event loop lag, GC, handles...
const register = client.register;
client.collectDefaultMetrics({ register });

const httpRequestsTotal = new client.Counter({
  name: 'http_requests_total',
  help: 'Total HTTP requests handled',
  labelNames: ['method', 'route', 'status_code']
});

const httpRequestDuration = new client.Histogram({
  name: 'http_request_duration_seconds',
  help: 'HTTP request latency in seconds',
  labelNames: ['method', 'route', 'status_code'],
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
});

function httpMetricsMiddleware(req, res, next) {
  const end = httpRequestDuration.startTimer();
  res.on('finish', () => {
    // routeTemplate dùng chung với log (xem src/http-route.js) để nhãn route
    // trên Prometheus và trên Loki luôn khớp nhau
    const labels = { method: req.method, route: routeTemplate(req), status_code: String(res.statusCode) };
    httpRequestsTotal.inc(labels);
    end(labels);
  });
  next();
}

async function metricsHandler(req, res, next) {
  try {
    res.set('Content-Type', register.contentType);
    res.end(await register.metrics());
  } catch (err) {
    next(err);
  }
}

module.exports = { httpMetricsMiddleware, metricsHandler };
