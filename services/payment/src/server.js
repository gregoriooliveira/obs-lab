'use strict';
// payment-service – ponta da cadeia. Cenários realistas de erro e latência.
const express = require('express');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');
const log = require('./log');

const app  = express();
const PORT = parseInt(process.env.APP_PORT || '8082', 10);
const tracer = trace.getTracer('payment-service');
const meter  = metrics.getMeter('payment-service');

const paymentsTotal  = meter.createCounter('payment.processed.total', { description: 'Pagamentos processados' });
const paymentsFailed = meter.createCounter('payment.failed.total',    { description: 'Pagamentos recusados' });
const revenue        = meter.createCounter('payment.revenue.total',   { description: 'Receita USD', unit: 'USD' });

const ERROR_RATE = () => parseFloat(process.env.ERROR_RATE || '0.10');
const JITTER     = () => parseInt(process.env.LATENCY_JITTER_MS || '150', 10);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rand  = (a, b) => Math.floor(Math.random() * (b - a + 1)) + a;

// ── Janela de degradação: a cada ~3min entra num "incidente" de 45s ──────────
// Durante o incidente: latência do adquirente sobe muito e recusas aumentam.
let degraded = false;
setInterval(() => {
  degraded = true;
  log.warn('[payment-service] *** INÍCIO janela de degradação (45s) ***', { logger: 'degradation', degraded: true });
  setTimeout(() => {
    degraded = false;
    log.info('[payment-service] --- fim janela de degradação ---', { logger: 'degradation', degraded: false });
  }, 45000);
}, 180000);

app.use(express.json());
// Access log estruturado: uma linha por request, com trace_id/span_id.
// E o que da o vinculo log <-> trace no o11y (Related Content).
app.use(log.httpMiddleware());
// O /health falha por PROBABILIDADE, do mesmo jeito que o ERROR_RATE do
// /charge - nao por janela fixa. Antes ele respondia 200 sempre, e os testes
// sinteticos do ThousandEyes nunca viam falha nenhuma; travar 503 durante toda
// a janela de degradacao ia pro outro extremo (falha previsivel de 45s).
// Aqui a checagem cai as vezes, e cai bem mais durante a degradacao.
const HEALTH_ERROR_RATE      = () => parseFloat(process.env.HEALTH_ERROR_RATE || '0.04');
const HEALTH_ERROR_RATE_DEGR = () => parseFloat(process.env.HEALTH_ERROR_RATE_DEGRADED || '0.35');
app.get('/health', (req, res) => {
  const rate = degraded ? HEALTH_ERROR_RATE_DEGR() : HEALTH_ERROR_RATE();
  if (Math.random() < rate) {
    return res.status(503).json({ status: 'degraded', service: 'payment-service',
                                  reason: degraded ? 'acquirer_unavailable' : 'acquirer_timeout',
                                  degraded });
  }
  res.json({ status: 'ok', service: 'payment-service', degraded });
});

app.post('/charge', async (req, res) => {
  await tracer.startActiveSpan('payment.charge', async (span) => {
    const { amount = 0, method = 'card', orderId } = req.body;
    span.setAttribute('payment.amount', amount);
    span.setAttribute('payment.method', method);
    span.setAttribute('order.id', orderId || 'unknown');
    span.setAttribute('payment.degraded_window', degraded);

    // ── Autorização no adquirente ────────────────────────────────────────
    let acquirerError = false;
    await tracer.startActiveSpan('acquirer.authorize', async (acq) => {
      acq.setAttribute('peer.service', 'acquirer-gateway');
      acq.setAttribute('payment.method', method);

      // Latência base + cenários:
      let latency = rand(120, 300 + JITTER());
      // boleto é mais lento por natureza
      if (method === 'boleto') latency += rand(200, 500);
      // 5% das chamadas têm um pico de latência (p99)
      if (Math.random() < 0.05) { latency += rand(1500, 3500); acq.setAttribute('latency.spike', true); }
      // durante incidente, adquirente fica MUITO lento
      if (degraded) latency += rand(2000, 5000);

      acq.setAttribute('acquirer.latency_ms', latency);
      await sleep(latency);

      // Timeout do adquirente (> 5s) vira erro de gateway
      if (latency > 5000) {
        acquirerError = true;
        acq.setStatus({ code: SpanStatusCode.ERROR, message: 'Acquirer timeout' });
        acq.setAttribute('error', true);
        acq.setAttribute('error.type', 'acquirer_timeout');
      }
      acq.end();
    });

    // Erro de gateway do adquirente → 503
    if (acquirerError) {
      paymentsFailed.add(1, { method, reason: 'acquirer_timeout' });
      span.setStatus({ code: SpanStatusCode.ERROR, message: 'Acquirer timeout' });
      span.setAttribute('error.type', 'acquirer_timeout');
      span.end();
      return res.status(503).json({ status: 'error', reason: 'acquirer_timeout', orderId });
    }

    // ── Taxa de recusa: base + fatores correlacionados ───────────────────
    let declineChance = ERROR_RATE();
    if (method === 'boleto')  declineChance += 0.08;   // boleto recusa mais
    if (amount > 200)         declineChance += 0.10;   // valores altos recusam mais
    if (degraded)             declineChance += 0.20;   // incidente: mais recusas

    if (Math.random() < declineChance) {
      const reasons = ['insufficient_funds', 'card_declined', 'fraud_suspected', 'expired_card'];
      const reason = reasons[Math.floor(Math.random() * reasons.length)];
      paymentsFailed.add(1, { method, reason });
      span.setStatus({ code: SpanStatusCode.ERROR, message: `Declined: ${reason}` });
      span.setAttribute('payment.decline_reason', reason);
      span.end();
      return res.status(402).json({ status: 'declined', reason, orderId });
    }

    const paymentId = `PAY-${Date.now()}`;
    paymentsTotal.add(1, { method });
    revenue.add(amount, { method });
    span.setAttribute('payment.id', paymentId);
    span.setAttribute('payment.status', 'approved');
    span.end();
    res.status(201).json({ status: 'approved', paymentId, amount, method, orderId });
  });
});

app.listen(PORT, '0.0.0.0', () => {
  log.info(`[payment-service] :${PORT} | error_rate=${process.env.ERROR_RATE || '0.10'} | degradação a cada 3min | health_error_rate=${process.env.HEALTH_ERROR_RATE || '0.04'}`, { logger: 'startup', port: PORT });
});
