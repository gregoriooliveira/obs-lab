'use strict';
// orders-service – meio da cadeia. Agora com Postgres real (inventory-db).
// Slow queries reais (SELECT FOR UPDATE com lock), retries, propagação de erro.
const express = require('express');
const http = require('http');
const { Pool } = require('pg');
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
// erros do pg vem com code SQLSTATE ("57P01", "23505"...) e erros de socket com
// code string ("ECONNREFUSED"). Passar isso pro res.status() lanca
// ERR_HTTP_INVALID_STATUS_CODE e derruba o processo.
const httpStatus = (code, fallback = 500) =>
  Number.isInteger(code) && code >= 400 && code <= 599 ? code : fallback;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand  = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;

// ── Pool de conexão Postgres ──────────────────────────────────────────────
const pool = new Pool({
  host:     process.env.DB_HOST || 'inventory-db',
  port:     parseInt(process.env.DB_PORT || '5432', 10),
  user:     process.env.DB_USER || 'obslab',
  password: process.env.DB_PASSWORD || 'obslab',
  database: process.env.DB_NAME || 'inventory',
  max: 10,                       // pool de 10 conexões
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
});

pool.on('error', (err) => console.error('[pg] pool error:', err.message));

// ── Reposicao de estoque ──────────────────────────────────────────────────
// O lab roda 24/7: sem repor, o estoque semeado zera em minutos e TODO checkout
// passa a falhar com 409 permanente. A falha de estoque que interessa e a
// aleatoria (ERROR_RATE), nao o fim do inventario.
const RESTOCK_EVERY_S = parseInt(process.env.RESTOCK_INTERVAL_S || '60', 10);
const RESTOCK_BELOW   = parseInt(process.env.RESTOCK_BELOW || '50', 10);
const RESTOCK_TO      = parseInt(process.env.RESTOCK_TO || '500', 10);
setInterval(async () => {
  try {
    const r = await pool.query('UPDATE products SET stock = $1 WHERE stock < $2', [RESTOCK_TO, RESTOCK_BELOW]);
    if (r.rowCount) console.log(`[orders-service] restock: ${r.rowCount} produto(s) -> ${RESTOCK_TO}`);
  } catch (e) { /* db fora do ar: tenta de novo no proximo ciclo */ }
}, RESTOCK_EVERY_S * 1000);

app.use(express.json());
// Alem da checagem real do Postgres, uma fracao das chamadas falha de
// proposito: sem isso o teste sintetico so veria erro se o banco caisse, e o
// lab passaria dias sem um unico alerta pra demonstrar.
const HEALTH_ERROR_RATE = () => parseFloat(process.env.HEALTH_ERROR_RATE || '0.03');
app.get('/health', async (req, res) => {
  if (Math.random() < HEALTH_ERROR_RATE()) {
    return res.status(503).json({ status: 'degraded', service: 'orders-service',
                                  db: 'up', reason: 'connection_pool_exhausted' });
  }
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'ok', service: 'orders-service', db: 'up' });
  } catch (e) {
    res.status(503).json({ status: 'degraded', service: 'orders-service', db: 'down' });
  }
});

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
      resp.on('end', () => {
        // resposta nao-JSON nao pode estourar dentro do handler (crasha o processo)
        let body = {};
        try { body = JSON.parse(chunks || '{}'); } catch (_) { body = { raw: chunks }; }
        resolve({ status: resp.statusCode, body });
      });
    });
    req.on('timeout', () => { req.destroy(); reject({ code: 504, msg: 'payment timeout' }); });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

// ── Reserva de estoque no Postgres (queries reais) ─────────────────────────
// A instrumentação pg do OTel gera spans automáticos para cada query.
async function reserveStock(items, amount) {
  const t0 = Date.now();
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    // 8% das transações pegam um caminho lento (simula lock contention):
    // faz um pg_sleep no servidor, gerando latência de query REAL.
    const slow = Math.random() < 0.08;
    if (slow) {
      const lockMs = rand(800, 2000);
      await client.query('SELECT pg_sleep($1)', [lockMs / 1000.0]);
    }

    // Para cada item: SELECT FOR UPDATE (lock de linha) + UPDATE do estoque
    for (const it of items) {
      const pid = it.productId;
      const qty = it.qty || 1;

      const sel = await client.query(
        'SELECT id, stock FROM products WHERE id = $1 FOR UPDATE',
        [pid]
      );

      if (sel.rows.length === 0) {
        await client.query('ROLLBACK');
        throw { code: 404, msg: 'product not found' };
      }

      const stock = sel.rows[0].stock;
      // Falha de estoque correlacionada com produto caro
      let stockFail = ERROR_RATE() * 0.5;
      if (amount > 250) stockFail += 0.08;

      if (stock < qty || Math.random() < stockFail) {
        await client.query('ROLLBACK');
        throw { code: 409, msg: 'Insufficient stock' };
      }

      await client.query(
        'UPDATE products SET stock = stock - $1 WHERE id = $2',
        [qty, pid]
      );
    }

    await client.query('COMMIT');
    const lat = Date.now() - t0;
    dbDuration.record(lat, { operation: 'reserve', slow: String(slow) });
    return { latencyMs: lat, slow };
  } catch (e) {
    try { await client.query('ROLLBACK'); } catch (_) {}
    const lat = Date.now() - t0;
    dbDuration.record(lat, { operation: 'reserve', error: 'true' });
    throw e;
  } finally {
    client.release();
  }
}

async function persistOrder(orderId, customerId, amount, status, paymentId, items) {
  try {
    await pool.query(
      'INSERT INTO orders (id, customer_id, amount, status, payment_id) VALUES ($1,$2,$3,$4,$5)',
      [orderId, customerId, amount, status, paymentId || null]
    );
    for (const it of items) {
      await pool.query(
        'INSERT INTO order_items (order_id, product_id, qty, price) VALUES ($1,$2,$3,$4)',
        [orderId, it.productId, it.qty || 1, it.price || 0]
      );
    }
  } catch (e) {
    console.error('[pg] persist order failed:', e.message);
  }
}

app.post('/orders', async (req, res) => {
  await tracer.startActiveSpan('order.create', async (span) => {
    // sufixo aleatorio: com 2 replicas dois pedidos caem no mesmo milissegundo
    // e o INSERT colide na PK (o pedido some do banco)
    const orderId = `ORD-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const { items = [], customerId = 'guest', paymentMethod = 'card' } = req.body;
    const amount = items.reduce((s, i) => s + (i.price || 0) * (i.qty || 1), 0) || rand(20, 350);

    span.setAttribute('order.id', orderId);
    span.setAttribute('order.value', amount);
    span.setAttribute('customer.id', customerId);
    span.setAttribute('payment.method', paymentMethod);

    try {
      // ── 1. Reserva de estoque no Postgres (spans de query automáticos) ──
      const stockResult = await reserveStock(items, amount);
      span.setAttribute('db.slow_query', stockResult.slow);
      span.setAttribute('db.latency_ms', stockResult.latencyMs);

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
          break;
        } catch (e) {
          if (e.code === 504 && attempt < maxAttempts) {
            span.setAttribute(`retry.${attempt}.reason`, 'payment_timeout');
            continue;
          }
          throw e;
        }
      }

      if (pay.status === 402) {
        ordersFailed.add(1, { reason: 'payment_declined' });
        await persistOrder(orderId, customerId, amount, 'payment_declined', null, items);
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'Payment declined' });
        span.setAttribute('order.status', 'payment_declined');
        span.setAttribute('payment.decline_reason', pay.body.reason);
        span.end();
        return res.status(402).json({ orderId, status: 'declined', reason: pay.body.reason });
      }
      if (pay.status !== 201) {
        ordersFailed.add(1, { reason: 'payment_error' });
        await persistOrder(orderId, customerId, amount, 'payment_error', null, items);
        span.setStatus({ code: SpanStatusCode.ERROR, message: 'Payment gateway error' });
        span.setAttribute('order.status', 'payment_error');
        span.end();
        return res.status(502).json({ orderId, status: 'failed', reason: pay.body.reason });
      }

      // sucesso: persiste pedido confirmado
      await persistOrder(orderId, customerId, amount, 'confirmed', pay.body.paymentId, items);
      ordersTotal.add(1, { payment_method: paymentMethod });
      span.setAttribute('payment.id', pay.body.paymentId);
      span.setAttribute('order.status', 'confirmed');
      span.end();
      res.status(201).json({ orderId, status: 'confirmed', amount, paymentId: pay.body.paymentId });

    } catch (err) {
      const reason = err.msg || err.message || 'error';
      ordersFailed.add(1, { reason });
      span.setStatus({ code: SpanStatusCode.ERROR, message: reason });
      span.setAttribute('order.status', 'failed');
      span.setAttribute('error.type',
        err.code === 504 ? 'payment_timeout' :
        err.code === 409 ? 'out_of_stock' :
        err.code === 404 ? 'product_not_found' :
        typeof err.code === 'string' ? `db_${err.code}` : 'order_error');
      span.end();
      res.status(httpStatus(err.code)).json({ orderId, status: 'failed', error: reason });
    }
  });
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[orders-service] :${PORT} → payment: ${PAYMENT_URL} | Postgres: ${process.env.DB_HOST || 'inventory-db'} | slow queries 8%`);
});
