'use strict';
// gateway-service – entrada. Catálogo + roteamento + cenários de borda + seguranca.
const express = require('express');
const http = require('http');
const { Pool } = require('pg');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');
const sec = require('./security');

const app  = express();
const PORT = parseInt(process.env.APP_PORT || '8080', 10);
const ORDERS_URL = process.env.ORDERS_URL || 'http://orders-service:8081';
const tracer = trace.getTracer('gateway-service');
const meter  = metrics.getMeter('gateway-service');

const httpReqs = meter.createCounter('http.requests.total', { description: 'Requests' });
const httpErrs = meter.createCounter('http.errors.total',   { description: 'Erros 5xx' });
const reqDur   = meter.createHistogram('http.request.duration', { description: 'Latência ms', unit: 'ms' });
const fraudTotal = meter.createCounter('security.fraud.total', { description: 'Eventos de fraude detectados' });
const authTotal  = meter.createCounter('security.auth.total',  { description: 'Tentativas de login' });

// Pool Postgres (auth + security_events). DB_* vem do secret obs-lab-db.
const pool = new Pool({
  host: process.env.DB_HOST || 'inventory-db',
  port: parseInt(process.env.DB_PORT || '5432', 10),
  user: process.env.DB_USER || 'obslab',
  password: process.env.DB_PASSWORD || 'obslab',
  database: process.env.DB_NAME || 'inventory',
  max: 5,
});
pool.on('error', () => {});

// cria users demo com hash real (senhas fracas propositais: alice/bob/admin)
// Lockout temporario: sem TTL a conta travava pra sempre no primeiro
// brute-force e os cenarios de login legitimo / ATO paravam de gerar dado.
const LOCK_TTL_MS = parseInt(process.env.LOCK_TTL_MS || '180000', 10);

async function bootstrapUsers() {
  // idempotente: o initdb so roda em volume vazio, entao a coluna e garantida aqui
  try { await pool.query('ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_at TIMESTAMP'); }
  catch (e) { /* db pode nao estar pronto; retry no proximo boot */ }
  const seed = [['alice','alice123'],['bob','hunter2'],['admin','admin']];
  for (const [u, p] of seed) {
    try {
      await pool.query(
        `INSERT INTO users (username, password_hash) VALUES ($1,$2)
         ON CONFLICT (username) DO NOTHING`, [u, sec.sha256(p)]);
    } catch (e) { /* db pode nao estar pronto; ok */ }
  }
}

// registra evento: log JSON + banco + metrica + atributo no span
function reportFraud(evt) {
  sec.logSecurity(evt);
  sec.persistEvent(pool, evt);
  fraudTotal.add(1, { threat: evt.threat, blocked: String(!!evt.blocked) });
  const span = trace.getActiveSpan();
  if (span) {
    span.setAttribute('security.threat', evt.threat);
    span.setAttribute('security.risk_score', evt.risk_score);
    span.setAttribute('security.blocked', !!evt.blocked);
  }
}

const JITTER = () => parseInt(process.env.LATENCY_JITTER_MS || '150', 10);
// erros de socket trazem code string ("ECONNREFUSED"); res.status() com string
// lanca ERR_HTTP_INVALID_STATUS_CODE e derruba o processo.
const httpStatus = (code, fallback = 502) =>
  Number.isInteger(code) && code >= 400 && code <= 599 ? code : fallback;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand  = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;

app.use(express.json());
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    const labels = { method: req.method, route: req.path, status: String(res.statusCode) };
    httpReqs.add(1, labels);
    reqDur.record(Date.now() - start, labels);
    if (res.statusCode >= 500) httpErrs.add(1, { route: req.path });
  });
  next();
});

const CATALOGUE = [
  { id: 'prod-001', name: 'Pokémon TCG Booster',   price: 19.99 },
  { id: 'prod-002', name: 'Yu-Gi-Oh! Deck',         price: 34.99 },
  { id: 'prod-003', name: 'MTG Draft Set',          price: 129.99 },
  { id: 'prod-004', name: 'Deck Sleeves 100pk',     price: 12.99 },
  { id: 'prod-005', name: 'Playmat Premium',        price: 49.99 },
  // item barato: e o alvo do cenario de card testing (amount < 5)
  { id: 'prod-006', name: 'Single Card Common',     price: 2.99 },
];

function postJSON(url, body, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const u = new URL(url);
    const req = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
      timeout: timeoutMs,
    }, (resp) => {
      let chunks = '';
      resp.on('data', c => chunks += c);
      resp.on('end', () => {
        // resposta nao-JSON nao pode estourar dentro do handler (crasha o processo)
        let body = {};
        try { body = JSON.parse(chunks || '{}'); } catch (_) { body = { raw: chunks }; }
        resolve({ status: resp.statusCode, body });
      });
    });
    req.on('timeout', () => { req.destroy(); reject({ code: 504, msg: 'orders timeout' }); });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

app.get('/health', (req, res) => res.json({ status: 'ok', service: 'gateway-service' }));

app.get('/api/products', async (req, res) => {
  await tracer.startActiveSpan('catalogue.list', async (span) => {
    const ip = sec.clientIp(req);
    const scrape = sec.detectScraping(req, ip);
    if (scrape) {
      reportFraud(scrape.evt);
      if (scrape.blocked) { span.setAttribute('security.blocked', true); span.end(); return res.status(429).json({ error: 'rate limited' }); }
    }
    await tracer.startActiveSpan('cache.get', async (c) => {
      c.setAttribute('db.system', 'redis');
      // cache miss ocasional → mais lento
      const hit = Math.random() > 0.15;
      c.setAttribute('cache.hit', hit);
      await sleep(hit ? rand(5, 25) : rand(60, 150));
      c.end();
    });
    span.setAttribute('result.count', CATALOGUE.length);
    span.end();
    res.json({ products: CATALOGUE, total: CATALOGUE.length });
  });
});

app.get('/api/products/:id', async (req, res) => {
  await tracer.startActiveSpan('catalogue.get', async (span) => {
    span.setAttribute('product.id', req.params.id);
    await sleep(rand(20, 30 + JITTER()));
    const p = CATALOGUE.find(x => x.id === req.params.id);
    span.end();
    if (!p) return res.status(404).json({ error: 'not found' });
    res.json(p);
  });
});

app.post('/api/checkout', async (req, res) => {
  await tracer.startActiveSpan('checkout', async (span) => {
    span.setAttribute('gateway.route', 'checkout');
    const ip = sec.clientIp(req);
    const { items = [], customerId = 'guest' } = req.body || {};
    const amount = items.reduce((s, i) => s + (i.price || 0) * (i.qty || 1), 0);

    const tamper = sec.detectTampering(items, CATALOGUE, ip);
    if (tamper) {
      reportFraud(tamper.evt);
      span.setAttribute('security.blocked', true); span.end();
      return res.status(400).json({ error: 'invalid item price', threat: 'price_tampering' });
    }
    const card = sec.detectCardTesting(ip, amount);
    if (card) { reportFraud(card.evt); if (card.blocked) { span.end(); return res.status(429).json({ error: 'payment rate limited' }); } }
    const vel = sec.detectVelocity(customerId, ip);
    if (vel) { reportFraud(vel.evt); if (vel.blocked) { span.end(); return res.status(429).json({ error: 'order velocity exceeded' }); } }

    try {
      const r = await postJSON(`${ORDERS_URL}/orders`, req.body);
      span.setAttribute('order.status', r.body.status || 'unknown');
      if (r.status >= 400) span.setStatus({ code: SpanStatusCode.ERROR, message: `downstream ${r.status}` });
      span.end();
      res.status(r.status).json(r.body);
    } catch (err) {
      span.recordException(err);
      span.setStatus({ code: SpanStatusCode.ERROR, message: err.msg || 'error' });
      span.setAttribute('error.type', err.code === 504 ? 'orders_timeout' : 'gateway_error');
      span.end();
      res.status(httpStatus(err.code)).json({ error: err.msg || err.message || 'orders-service unreachable' });
    }
  });
});

// ── AUTH: login com brute-force e account takeover (ATO) ──────────────────
app.post('/api/login', async (req, res) => {
  await tracer.startActiveSpan('auth.login', async (span) => {
    const ip = sec.clientIp(req);
    const { username = '', password = '' } = req.body || {};
    span.setAttribute('auth.username', username);
    span.setAttribute('client.ip', ip);
    authTotal.add(1, { username });

    const ipRate   = sec.hitWindow(`login-ip:${ip}`, 60000);
    const userRate = sec.hitWindow(`login-user:${username}`, 60000);

    try {
      const r = await pool.query('SELECT * FROM users WHERE username = $1', [username]);

      if (ipRate > 5 || userRate > 5) {
        const risk = Math.min(100, Math.max(ipRate, userRate) * 12);
        const evt = { event_type: 'login', threat: 'brute_force', username, client_ip: ip,
                      risk_score: risk, blocked: risk > 70,
                      detail: `ip_attempts=${ipRate} user_attempts=${userRate}` };
        reportFraud(evt);
        if (evt.blocked) {
          await pool.query('UPDATE users SET locked = TRUE, locked_at = NOW() WHERE username = $1', [username]).catch(()=>{});
          span.setAttribute('auth.result', 'blocked_bruteforce'); span.end();
          return res.status(429).json({ error: 'too many attempts, account locked' });
        }
      }

      let user = r.rows[0];

      // lockout expirado -> destrava (senao o brute-force mata a conta pra sempre)
      if (user && user.locked) {
        const since = user.locked_at ? Date.now() - new Date(user.locked_at).getTime() : Infinity;
        if (since > LOCK_TTL_MS) {
          await pool.query(
            'UPDATE users SET locked = FALSE, locked_at = NULL, failed_logins = 0 WHERE username = $1',
            [username]).catch(()=>{});
          user = { ...user, locked: false, locked_at: null, failed_logins: 0 };
          sec.logSecurity({ event_type: 'login', outcome: 'unlocked', username, client_ip: ip,
                            risk_score: 0, detail: `lock_ttl_ms=${LOCK_TTL_MS}` });
          span.setAttribute('auth.lock_expired', true);
        }
      }

      const ok = user && !user.locked && user.password_hash === sec.sha256(password);

      if (!ok) {
        if (user) await pool.query('UPDATE users SET failed_logins = failed_logins + 1 WHERE username = $1', [username]).catch(()=>{});
        sec.logSecurity({ event_type: 'login', outcome: 'fail', username, client_ip: ip, risk_score: 20,
                          detail: user ? (user.locked ? 'account_locked' : 'bad_password') : 'unknown_user' });
        span.setAttribute('auth.result', 'fail'); span.end();
        return res.status(401).json({ error: 'invalid credentials' });
      }

      if (user.last_login_ip && user.last_login_ip !== ip) {
        reportFraud({ event_type: 'login', threat: 'account_takeover', username, client_ip: ip,
                      risk_score: 75, blocked: false,
                      detail: `new_ip=${ip} last_ip=${user.last_login_ip}` });
        span.setAttribute('auth.ato_suspected', true);
      }

      await pool.query('UPDATE users SET last_login_ip = $1, last_login_at = NOW(), failed_logins = 0 WHERE username = $2', [ip, username]).catch(()=>{});
      sec.logSecurity({ event_type: 'login', outcome: 'success', username, client_ip: ip, risk_score: 0 });
      span.setAttribute('auth.result', 'success'); span.end();
      res.json({ status: 'ok', user: username, token: sec.sha256(username + Date.now()) });

    } catch (err) {
      span.recordException(err); span.end();
      res.status(503).json({ error: 'auth unavailable' });
    }
  });
});

// debug
app.get('/api/debug/error', (req, res) => res.status(500).json({ error: 'forced' }));
app.get('/api/debug/slow', async (req, res) => { await sleep(parseInt(req.query.ms || '2000')); res.json({ slept: true }); });

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[gateway-service] :${PORT} → orders: ${ORDERS_URL} | cache miss 15% | security ON`);
  bootstrapUsers().then(() => console.log('[gateway-service] users demo prontos (alice/bob/admin)'));
});
