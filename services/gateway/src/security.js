'use strict';
// Modulo de seguranca do gateway.
// Detecta 4 fluxos de fraude + auth (brute-force / ATO).
// Loga JSON estruturado em stdout (-> agent DaemonSet -> Splunk) e grava em
// security_events no Postgres (-> visivel via consultas / DBM).
const crypto = require('crypto');
const { trace } = require('@opentelemetry/api');
const log = require('./log');

const tracer = trace.getTracer('gateway-security');
const sha256 = s => crypto.createHash('sha256').update(s).digest('hex');

// ── Log estruturado (uma linha JSON por evento) ────────────────────────────
// Passa pelo log.js para sair com severity e trace_id/span_id - e o que faz o
// evento de fraude aparecer ligado ao trace no o11y, e nao como "unknown".
function logSecurity(evt) {
  const sev = evt.blocked || (evt.risk_score || 0) >= 70 ? 'WARN' : 'INFO';
  const msg = `${evt.threat || evt.event_type} ${evt.blocked ? 'blocked' : 'observed'}`;
  log.emit(sev, msg, { logger: 'security', ...evt });
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

// Le a janela sem registrar hit. O login conta FALHAS, nao tentativas: quem
// so registra acontece no caminho de falha.
function peekWindow(key, windowMs = 60000) {
  const now = Date.now();
  return (windows.get(key) || []).filter(t => now - t < windowMs).length;
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

// 5. CREDENTIAL STUFFING
// Lista vazada testada contra a base: MUITOS usuarios distintos pelo mesmo IP.
// O brute force e o contrario (um usuario, muitas senhas). Sem este detector
// o `ipRate > 5` do login classificava stuffing como brute_force e travava a
// conta da vitima - a tecnica (T1110.004 vs T1110.001) ficava errada.
// Bloqueio de proposito acima do aviso: entre os dois cabe o "acerto" da lista,
// que e o que alimenta o ATO e fecha a cadeia no Fusion Center.
const STUFF_WARN  = parseInt(process.env.STUFFING_WARN_USERS  || '4', 10);
const STUFF_BLOCK = parseInt(process.env.STUFFING_BLOCK_USERS || '7', 10);
const stuffSeen = new Map();   // ip -> Map(username -> ts)
function detectStuffing(ip, username) {
  const now = Date.now();
  const seen = stuffSeen.get(ip) || new Map();
  for (const [u, t] of seen) if (now - t > 60000) seen.delete(u);
  seen.set(username, now);
  stuffSeen.set(ip, seen);
  const users = seen.size;
  if (users < STUFF_WARN) return null;
  const blocked = users >= STUFF_BLOCK;
  return {
    blocked,
    evt: { event_type: 'login', threat: 'credential_stuffing', client_ip: ip, username,
           risk_score: Math.min(100, users * 12), blocked,
           detail: `distinct_users=${users}/60s` },
  };
}

// 6. MOTOR DE RISCO DO CHECKOUT
// Diferente dos detectores acima (uma regra = um alerta), aqui cada sinal SOMA
// pontos e a soma decide: aprova, pede desafio (3DS/step-up) ou bloqueia. O
// caso que ele pega e o "card cycling": uma mesma maquina testando varios
// cartoes em varias contas em poucos minutos. A chave e o dispositivo
// (X-Device-Id, o fingerprint que o front manda); sem ele, cai no IP.
const RISK_WINDOW_MS = 10 * 60 * 1000;
const RISK_CHALLENGE = parseInt(process.env.RISK_CHALLENGE_SCORE || '40', 10);
const RISK_BLOCK     = parseInt(process.env.RISK_BLOCK_SCORE || '70', 10);
const devices = new Map();          // deviceId -> { hits: [{t, card, customer}], blocks }
const knownPairs = new Set();       // "device|customer" ja vistos
function riskEngine({ deviceId, cardFp, customerId, amount }) {
  const now = Date.now();
  const d = devices.get(deviceId) || { hits: [], blocks: 0 };
  d.hits = d.hits.filter(h => now - h.t < RISK_WINDOW_MS);
  d.hits.push({ t: now, card: cardFp, customer: customerId });
  devices.set(deviceId, d);

  const cards     = new Set(d.hits.map(h => h.card).filter(Boolean)).size;
  const customers = new Set(d.hits.map(h => h.customer)).size;
  const last2min  = d.hits.filter(h => now - h.t < 120000).length;
  const pair = `${deviceId}|${customerId}`;
  const newPair = !knownPairs.has(pair);

  const rules = [];
  const add = (name, pts) => rules.push({ name, pts });
  if (cards >= 5)          add(`cartoes_distintos=${cards}`, 50);
  else if (cards >= 3)     add(`cartoes_distintos=${cards}`, 25);
  if (customers >= 3)      add(`contas_distintas=${customers}`, 20);
  if (last2min > 5)        add(`tentativas_2min=${last2min}`, 15);
  if (newPair)             add('dispositivo_novo_na_conta', 10);
  if (amount > 250)        add(`valor_alto=${amount.toFixed(2)}`, 10);
  if (d.blocks > 0)        add(`bloqueios_anteriores=${d.blocks}`, 20);

  const score = Math.min(100, rules.reduce((s, r) => s + r.pts, 0));
  const decision = score >= RISK_BLOCK ? 'block' : score >= RISK_CHALLENGE ? 'challenge' : 'approve';
  if (decision === 'block') d.blocks++;
  // so aprova "aprende" o par: conta atacada nao vira conhecida do dispositivo
  if (decision === 'approve') knownPairs.add(pair);
  return { score, decision, cards, customers, rules: rules.map(r => `${r.name}:+${r.pts}`) };
}

module.exports = {
  sha256, logSecurity, clientIp, hitWindow, peekWindow, persistEvent,
  detectScraping, detectTampering, detectCardTesting, detectVelocity, detectStuffing,
  riskEngine, tracer,
};
