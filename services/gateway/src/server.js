'use strict';
// gateway-service – entrada. Catálogo + roteamento + cenários de borda.
const express = require('express');
const http = require('http');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');

const app  = express();
const PORT = parseInt(process.env.APP_PORT || '8080', 10);
const ORDERS_URL = process.env.ORDERS_URL || 'http://orders-service:8081';
const tracer = trace.getTracer('gateway-service');
const meter  = metrics.getMeter('gateway-service');

const httpReqs = meter.createCounter('http.requests.total', { description: 'Requests' });
const httpErrs = meter.createCounter('http.errors.total',   { description: 'Erros 5xx' });
const reqDur   = meter.createHistogram('http.request.duration', { description: 'Latência ms', unit: 'ms' });

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

// debug
app.get('/api/debug/error', (req, res) => res.status(500).json({ error: 'forced' }));
app.get('/api/debug/slow', async (req, res) => { await sleep(parseInt(req.query.ms || '2000')); res.json({ slept: true }); });

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[gateway-service] :${PORT} → orders: ${ORDERS_URL} | cache miss 15%`);
});
