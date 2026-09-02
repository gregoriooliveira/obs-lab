'use strict';
// Modulo de seguranca do gateway.
// Detecta 4 fluxos de fraude + auth (brute-force / ATO).
// Loga JSON estruturado em stdout (-> agent DaemonSet -> Splunk) e grava em
// security_events no Postgres (-> visivel via consultas / DBM).
const crypto = require('crypto');
const { trace } = require('@opentelemetry/api');

const tracer = trace.getTracer('gateway-security');
const sha256 = s => crypto.createHash('sha256').update(s).digest('hex');

// ── Log estruturado (uma linha JSON por evento) ────────────────────────────
function logSecurity(evt) {
  console.log(JSON.stringify({
    ts: new Date().toISOString(),
    logger: 'security',
    service: 'gateway-service',
    ...evt,
  }));
}

function clientIp(req) {
  return (req.headers['x-forwarded-for'] || req.socket.remoteAddress || 'unknown')
    .split(',')[0].trim();
}

// ── Estado em memoria para velocity / card testing ─────────────────────────
const windows = new Map();
function hitWindow(key, windowMs = 60000) {
  const now = Date.now();
  const arr = (windows.get(key) || []).filter(t => now - t < windowMs);
  arr.push(now);
  windows.set(key, arr);
  return arr.length;
}

async function persistEvent(pool, evt) {
  try {
    await pool.query(
      `INSERT INTO security_events (event_type, threat, username, client_ip, risk_score, detail)
       VALUES ($1,$2,$3,$4,$5,$6)`,
      [evt.event_type, evt.threat || null, evt.username || null,
       evt.client_ip || null, evt.risk_score || 0, evt.detail || null]
    );
  } catch (e) { /* audit nao pode derrubar request */ }
}

// ── Detectores ─────────────────────────────────────────────────────────────

// 1. BOT SCRAPING
// Threshold alto de proposito: o trafego legitimo do load-gen sai de poucos IPs
// (um por pod), entao um limite baixo marcava usuario normal como bot e
// devolvia 429 no catalogo inteiro. Quem dispara e o User-Agent de ferramenta.
const SCRAPE_RATE = parseInt(process.env.SCRAPE_RATE_10S || '30', 10);
function detectScraping(req, ip) {
  const ua = (req.headers['user-agent'] || '').toLowerCase();
  const suspToken = ['curl', 'python', 'scrapy', 'bot', 'wget', 'go-http', 'axios'].find(t => ua.includes(t));
  const rate = hitWindow(`scrape:${ip}`, 10000);
  if (rate > SCRAPE_RATE || suspToken) {
    const risk = Math.min(100, rate * 3 + (suspToken ? 40 : 0));
    const blocked = risk > 80;
    return {
      blocked,
      // blocked tambem dentro do evt: e o que vai pro log JSON e pro label da
      // metrica security.fraud.total (sem isso, tudo aparecia como blocked=false)
      evt: { event_type: 'catalogue_access', threat: 'bot_scraping', client_ip: ip,
             risk_score: risk, blocked, user_agent: ua || 'none',
             detail: `rate=${rate}/10s ua_flag=${suspToken || 'none'}` },
    };
  }
  return null;
}

// 2. CHECKOUT TAMPERING
function detectTampering(items, catalogue, ip) {
  for (const it of items || []) {
    const real = catalogue.find(p => p.id === it.productId);
    if (real && typeof it.price === 'number' && Math.abs(it.price - real.price) > 0.01) {
      const diff = (real.price - it.price).toFixed(2);
      return {
        blocked: true,
        evt: { event_type: 'checkout', threat: 'price_tampering', client_ip: ip,
               risk_score: 95, blocked: true, product_id: it.productId,
               detail: `sent=${it.price} real=${real.price} diff=${diff}` },
      };
    }
  }
  return null;
}

// 3. CARD TESTING
// Exige valor baixo E rajada: sem o `lowValue` obrigatorio este detector
// disparava antes do velocity_abuse (as duas janelas sao por IP) e o
// velocity_abuse nunca era alcancado.
function detectCardTesting(ip, amount) {
  const rate = hitWindow(`pay:${ip}`, 60000);
  const lowValue = amount < 5;
  if (rate > 8 && lowValue) {
    const risk = Math.min(100, rate * 8 + 20);
    const blocked = risk > 85;
    return {
      blocked,
      evt: { event_type: 'payment_attempt', threat: 'card_testing', client_ip: ip,
             risk_score: risk, blocked, amount,
             detail: `attempts=${rate}/60s low_value=${lowValue}` },
    };
  }
  return null;
}

// 4. VELOCITY ABUSE
function detectVelocity(customerId, ip) {
  const rate = hitWindow(`vel:${customerId}`, 60000);
  if (rate > 10) {
    const risk = Math.min(100, rate * 7);
    const blocked = risk > 90;
    return {
      blocked,
      evt: { event_type: 'order', threat: 'velocity_abuse', client_ip: ip,
             username: customerId, risk_score: risk, blocked,
             detail: `orders=${rate}/60s customer=${customerId}` },
    };
  }
  return null;
}

module.exports = {
  sha256, logSecurity, clientIp, hitWindow, persistEvent,
  detectScraping, detectTampering, detectCardTesting, detectVelocity, tracer,
};
