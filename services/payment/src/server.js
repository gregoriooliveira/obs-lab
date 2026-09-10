'use strict';
// payment-service – ponta da cadeia. Cenários realistas de erro e latência.
const express = require('express');
const { trace, metrics, SpanStatusCode } = require('@opentelemetry/api');

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
  console.log('[payment-service] *** INÍCIO janela de degradação (45s) ***');
  setTimeout(() => {
    degraded = false;
    console.log('[payment-service] --- fim janela de degradação ---');
  }, 45000);
}, 180000);

app.use(express.json());
// Durante a janela de degradacao o health responde 503. Sem isso os testes
// sinteticos do ThousandEyes nunca falham - eles batem em /health, que
// respondia 200 mesmo com o adquirente fora - e nenhum alerta dispara na demo.
// HEALTH_503_WHEN_DEGRADED=false devolve o comportamento antigo.
const HEALTH_FAILS = (process.env.HEALTH_503_WHEN_DEGRADED || 'true') !== 'false';
app.get('/health', (req, res) => {
  if (degraded && HEALTH_FAILS) {
    return res.status(503).json({ status: 'degraded', service: 'payment-service',
                                  reason: 'acquirer_unavailable', degraded });
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
  console.log(`[payment-service] :${PORT} | error_rate=${process.env.ERROR_RATE || '0.10'} | degradação a cada 3min`);
});
