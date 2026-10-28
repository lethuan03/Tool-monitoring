// ============================================================================
// Log có cấu trúc (structured logging): mỗi bản ghi là MỘT dòng JSON.
//
// Vì sao không dùng console.log thường?
//   console.log('user 5 đặt đơn 42 mất 120ms')   -> máy không đọc được, muốn
//     thống kê "đơn nào chậm" phải viết regex mò mẫm.
//   {"level":"info","msg":"http_request","route":"/api/orders/","duration_ms":120}
//     -> Alloy parse 1 phát ra field, Loki lọc được bằng LogQL:
//        {service="backend"} | json | duration_ms > 500
//
// Log ra stdout/stderr, KHÔNG ghi ra file: Docker đã thu stdout thành
// json-file và Alloy đọc từ đó. Ghi file trong container là phản pattern
// (mất khi container bị xoá, lại phải lo xoay vòng file).
// ============================================================================

const { routeTemplate } = require('./http-route');

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const MIN_LEVEL = LEVELS[String(process.env.LOG_LEVEL || 'info').toLowerCase()] || LEVELS.info;

const SERVICE = process.env.SERVICE_NAME || 'backend';

function emit(level, msg, fields) {
  if (LEVELS[level] < MIN_LEVEL) return;

  const record = {
    time: new Date().toISOString(),
    level,
    service: SERVICE,
    msg,
    ...fields
  };

  let line;
  try {
    line = JSON.stringify(record);
  } catch {
    // Vòng tham chiếu hoặc giá trị không serialize được: vẫn phải ghi được log
    line = JSON.stringify({ time: record.time, level, service: SERVICE, msg, log_error: 'unserializable fields' });
  }

  // error/warn -> stderr, còn lại -> stdout (đúng quy ước unix)
  if (level === 'error' || level === 'warn') process.stderr.write(line + '\n');
  else process.stdout.write(line + '\n');
}

const log = {
  debug: (msg, fields) => emit('debug', msg, fields),
  info: (msg, fields) => emit('info', msg, fields),
  warn: (msg, fields) => emit('warn', msg, fields),
  error: (msg, fields) => emit('error', msg, fields)
};

// Middleware: ghi đúng MỘT dòng log cho mỗi request đã hoàn tất.
function requestLogger(req, res, next) {
  const startedAt = process.hrtime.bigint();

  res.on('finish', () => {
    const durationMs = Number(process.hrtime.bigint() - startedAt) / 1e6;
    const route = routeTemplate(req);

    // /health và /metrics là request của MÁY (healthcheck Docker, Prometheus
    // scrape 15s/lần) — luôn thành công và rất đều. Để mức debug nên mặc định
    // không ghi, tránh nhấn chìm log thật của khách.
    const isMachineTraffic = route === '/health' || route === '/metrics';

    let level = 'info';
    if (res.statusCode >= 500) level = 'error';
    else if (res.statusCode >= 400) level = 'warn';
    else if (isMachineTraffic) level = 'debug';

    log[level]('http_request', {
      method: req.method,
      route,
      // Bỏ query string (?email=...&token=...) để log không chứa dữ liệu nhạy
      // cảm — giống nginx dùng $uri thay vì $request_uri. Xem README #30.
      path: req.originalUrl.split('?')[0],
      status: res.statusCode,
      duration_ms: Math.round(durationMs * 1000) / 1000,
      ip: req.ip,
      user_agent: req.get('user-agent') || ''
    });
  });

  next();
}

module.exports = { log, requestLogger };
