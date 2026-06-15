'use strict';
// orders-service – meio da cadeia. Slow queries, retries, propagação de erro.
const express = require('express');
const http = require('http');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');

const app  = express();
const PORT = parseInt(process.env.APP_PORT || '8081', 10);
const PAYMENT_URL = process.env.PAYMENT_URL || 'http://payment-service:8082';
const tracer = trace.getTracer('orders-service');
const meter  = metrics.getMeter('orders-service');

const ordersTotal  = meter.createCounter('orders.created.total', { description: 'Pedidos criados' });
const ordersFailed = meter.createCounter('orders.failed.total',  { description: 'Pedidos com falha' });
const dbDuration   = meter.createHistogram('orders.db.duration', { description: 'Latência DB ms', unit: 'ms' });

const ERROR_RATE = () => parseFloat(process.env.ERROR_RATE || '0.06');
const JITTER     = () => parseInt(process.env.LATENCY_JITTER_MS || '150', 10);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand  = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;

app.use(express.json());
app.get('/health', (req, res) => res.json({ status: 'ok', service: 'orders-service' }));

function postJSON(url, body, timeoutMs = 12000) {
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
      resp.on('end', () => resolve({ status: resp.statusCode, body: JSON.parse(chunks || '{}') }));
    });
    req.on('timeout', () => { req.destroy(); reject({ code: 504, msg: 'payment timeout' }); });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

app.post('/orders', async (req, res) => {
  await tracer.startActiveSpan('order.create', async (span) => {
    const orderId = `ORD-${Date.now()}`;
    const { items = [], customerId = 'guest', paymentMethod = 'card' } = req.body;
    const amount = items.reduce((s, i) => s + (i.price || 0) * (i.qty || 1), 0) || rand(20, 350);

    span.setAttribute('order.id', orderId);
    span.setAttribute('order.value', amount);
    span.setAttribute('customer.id', customerId);
    span.setAttribute('payment.method', paymentMethod);

    try {
      // ── 1. Reserva de estoque (DB) com slow query intermitente ──────────
      await tracer.startActiveSpan('inventory.reserve', async (inv) => {
        inv.setAttribute('peer.service', 'inventory-db');
        inv.setAttribute('db.system', 'postgresql');
        inv.setAttribute('db.operation', 'UPDATE');

        let dbLat = rand(40, 100 + JITTER() * 0.3);
        // 8% das queries são lentas (lock contention, full scan)
        if (Math.random() < 0.08) {
          dbLat += rand(800, 2000);
          inv.setAttribute('db.slow_query', true);
          inv.setAttribute('db.statement', 'SELECT ... FOR UPDATE (lock wait)');
        }
        inv.setAttribute('db.latency_ms', dbLat);
        dbDuration.record(dbLat, { operation: 'reserve' });
        await sleep(dbLat);

        // Falha de estoque correlacionada com produto caro
        let stockFail = ERROR_RATE() * 0.5;
        if (amount > 250) stockFail += 0.08;
        if (Math.random() < stockFail) {
          inv.setStatus({ code: SpanStatusCode.ERROR, message: 'Out of stock' });
          inv.setAttribute('error', true);
          inv.setAttribute('error.type', 'out_of_stock');
          inv.end();
          throw { code: 409, msg: 'Insufficient stock' };
        }
        inv.end();
      });

      // ── 2. Chama payment-service (com retry em timeout) ─────────────────
      let pay;
      let attempt = 0;
      const maxAttempts = 2;
      while (attempt < maxAttempts) {
        attempt++;
        try {
          pay = await tracer.startActiveSpan('payment.call', async (pc) => {
            pc.setAttribute('peer.service', 'payment-service');
            pc.setAttribute('retry.attempt', attempt);
            const r = await postJSON(`${PAYMENT_URL}/charge`, { amount, method: paymentMethod, orderId });
            pc.setAttribute('payment.response_status', r.status);
            pc.end();
            return r;
          });
          break;  // sucesso na chamada (mesmo que recusado)
        } catch (e) {
          if (e.code === 504 && attempt < maxAttempts) {
            span.setAttribute(`retry.${attempt}.reason`, 'payment_timeout');
            continue;  // tenta de novo
          }
          throw e;
        }
      }

      // payment recusou (402) ou erro de gateway (503)
      if (pay.status === 402) {
        ordersFailed.add(1, { reason: 'payment_declined' });
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'Payment declined' });
        span.setAttribute('order.status', 'payment_declined');
        span.setAttribute('payment.decline_reason', pay.body.reason);
        span.end();
        return res.status(402).json({ orderId, status: 'declined', reason: pay.body.reason });
      }
      if (pay.status !== 201) {
        ordersFailed.add(1, { reason: 'payment_error' });
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'Payment gateway error' });
        span.setAttribute('order.status', 'payment_error');
        span.end();
        return res.status(502).json({ orderId, status: 'failed', reason: pay.body.reason });
      }

      ordersTotal.add(1, { payment_method: paymentMethod });
      span.setAttribute('payment.id', pay.body.paymentId);
      span.setAttribute('order.status', 'confirmed');
      span.end();
      res.status(201).json({ orderId, status: 'confirmed', amount, paymentId: pay.body.paymentId });

    } catch (err) {
      ordersFailed.add(1, { reason: err.msg || 'error' });
      span.setStatus({ code: SpanStatusCode.ERROR, message: err.msg || 'error' });
      span.setAttribute('order.status', 'failed');
      span.setAttribute('error.type', err.code === 504 ? 'payment_timeout' : 'order_error');
      span.end();
      res.status(err.code || 500).json({ orderId, status: 'failed', error: err.msg });
    }
  });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[orders-service] :${PORT} → payment: ${PAYMENT_URL} | slow queries 8%, retry em timeout`);
});
