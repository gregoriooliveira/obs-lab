'use strict';
// Load generator – bate no gateway. Gera trace distribuído gateway→orders→payment.

const TARGET      = process.env.TARGET_URL        || 'http://gateway-service:8080';
const VUS         = parseInt(process.env.VIRTUAL_USERS   || '8',  10);
const THINK_TIME  = parseInt(process.env.THINK_TIME_MS   || '400', 10);
const STATS_EVERY = parseInt(process.env.STATS_INTERVAL_S || '30', 10) * 1000;

// Precos REAIS do catalogo do gateway. Mandar preco diferente daqui e
// exatamente o que o detector de price_tampering bloqueia (400) - o checkout
// legitimo tem que bater com o catalogo, senao orders/payment nunca recebem
// trafego e o trace distribuido morre no gateway.
const CATALOGUE = {
  'prod-001': 19.99,
  'prod-002': 34.99,
  'prod-003': 129.99,
  'prod-004': 12.99,
  'prod-005': 49.99,
  'prod-006': 2.99,     // item barato - alvo do cenario de card testing
};
const PRODUCTS = Object.keys(CATALOGUE);
const METHODS  = ['card','pix','boleto'];

// Populacao de usuarios legitimos: cada um com IP e customerId proprios.
// Sem isso o trafego normal inteiro vem de um IP so (o pod do load-gen) e com
// 4 customerIds, e cai nos rate limits de bot_scraping / card_testing /
// velocity_abuse - o checkout legitimo virava 429 e orders/payment ficavam sem
// trafego. Com 100 usuarios a taxa por usuario fica ~2 ordens de grandeza
// abaixo dos thresholds, com folga ate VUs=30.
const USERS = Array.from({ length: 100 }, (_, i) => ({
  ip: `198.18.${Math.floor(i / 250)}.${10 + (i % 250)}`,
  customerId: `cust-${String(i + 1).padStart(3, '0')}`,
}));
const pickUser = () => USERS[Math.floor(Math.random() * USERS.length)];
const asUser = (u) => ({ 'X-Forwarded-For': (u || pickUser()).ip });

const stats = { req: 0, ok: 0, err: 0, ms: 0, byJourney: {} };
function record(j, status, ms) {
  stats.req++; stats.ms += ms;
  if (status >= 200 && status < 500) stats.ok++; else stats.err++;
  stats.byJourney[j] = (stats.byJourney[j] || 0) + 1;
}

setInterval(() => {
  const rps = (stats.req / (STATS_EVERY / 1000)).toFixed(1);
  const avg = stats.req ? Math.round(stats.ms / stats.req) : 0;
  const errPct = stats.req ? ((stats.err / stats.req) * 100).toFixed(1) : '0.0';
  console.log(`[stats] VUs=${VUS} req=${stats.req} rps=${rps} avg=${avg}ms err=${errPct}% ` +
    Object.entries(stats.byJourney).map(([k,v]) => `${k}=${v}`).join(' '));
  stats.req = stats.ok = stats.err = stats.ms = 0; stats.byJourney = {};
}, STATS_EVERY);

const rand = arr => arr[Math.floor(Math.random() * arr.length)];
const sleep = ms => new Promise(r => setTimeout(r, ms + Math.random() * THINK_TIME * 0.5));

async function req(method, path, body, extraHeaders) {
  const start = Date.now();
  try {
    const r = await fetch(`${TARGET}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json', ...(extraHeaders || {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(10000),
    });
    return { status: r.status, ms: Date.now() - start };
  } catch (e) {
    return { status: 0, ms: Date.now() - start };
  }
}

async function browse(hdrs = asUser()) {
  let r = await req('GET', '/api/products', null, hdrs); record('browse', r.status, r.ms);
  await sleep(THINK_TIME);
  r = await req('GET', `/api/products/${rand(PRODUCTS)}`, null, hdrs); record('browse', r.status, r.ms);
}

async function checkout() {
  const user = pickUser();
  const hdrs = asUser(user);
  await browse(hdrs);
  await sleep(THINK_TIME);
  // qty ate 2: com prod-003 (129.99) o pedido passa de $250 e ativa o caminho
  // de falha correlacionada com valor alto no orders/payment.
  const items = Array.from({ length: rand([1,2,3]) }, () => {
    const productId = rand(PRODUCTS);
    return { productId, qty: rand([1,1,2]), price: CATALOGUE[productId] };
  });
  const r = await req('POST', '/api/checkout', {
    items, customerId: user.customerId, paymentMethod: rand(METHODS),
  }, hdrs);
  record('checkout', r.status, r.ms);
}

async function errorProbe() {
  const r = await req('GET', rand(['/api/debug/error', '/api/debug/slow?ms=1500']), null, asUser());
  record('error_probe', r.status, r.ms);
}

const ATTACKER_UAS = ['curl/8.4.0', 'python-requests/2.31', 'Scrapy/2.11'];

async function fraudScraping() {
  for (let i = 0; i < 25; i++) {
    const start = Date.now();
    try {
      const r = await fetch(`${TARGET}/api/products`, {
        headers: { 'User-Agent': rand(ATTACKER_UAS), 'X-Forwarded-For': '203.0.113.66' },
        signal: AbortSignal.timeout(8000),
      });
      record('fraud_scraping', r.status, Date.now() - start);
    } catch (e) {}
  }
}
async function fraudTampering() {
  const r = await req('POST', '/api/checkout', {
    items: [{ productId: 'prod-003', qty: 1, price: 1.99 }],
    customerId: 'cust-fraud', paymentMethod: 'card',
  }, { 'X-Forwarded-For': '203.0.113.77' });
  record('fraud_tampering', r.status, r.ms);
}
async function fraudCardTesting() {
  // preco REAL do item mais barato (2.99 < 5 = low value): com preco falso o
  // gateway classificaria como price_tampering e o card_testing nunca apareceria
  for (let i = 0; i < 12; i++) {
    const r = await req('POST', '/api/checkout', {
      items: [{ productId: 'prod-006', qty: 1, price: CATALOGUE['prod-006'] }],
      customerId: `cust-ct-${i}`, paymentMethod: 'card',
    }, { 'X-Forwarded-For': '203.0.113.88' });
    record('fraud_card_testing', r.status, r.ms);
  }
}
async function fraudVelocity() {
  for (let i = 0; i < 15; i++) {
    const r = await req('POST', '/api/checkout', {
      items: [{ productId: 'prod-001', qty: 1, price: CATALOGUE['prod-001'] }],
      customerId: 'cust-velocity', paymentMethod: 'pix',
    }, { 'X-Forwarded-For': '203.0.113.99' });
    record('fraud_velocity', r.status, r.ms);
  }
}
async function fraudBruteForce() {
  const user = rand(['alice', 'admin', 'bob']);
  for (let i = 0; i < 8; i++) {
    const r = await req('POST', '/api/login', { username: user, password: `wrong${i}` },
      { 'X-Forwarded-For': '198.51.100.42' });
    record('fraud_bruteforce', r.status, r.ms);
  }
}
async function fraudATO() {
  await req('POST', '/api/login', { username: 'bob', password: 'hunter2' }, { 'X-Forwarded-For': '192.0.2.10' });
  await sleep(500);
  const r = await req('POST', '/api/login', { username: 'bob', password: 'hunter2' }, { 'X-Forwarded-For': '203.0.113.200' });
  record('fraud_ato', r.status, r.ms);
}
async function fraudRun() {
  const attacks = [fraudScraping, fraudTampering, fraudCardTesting, fraudVelocity, fraudBruteForce, fraudATO];
  await rand(attacks)();
}

const FRAUD_ENABLED = (process.env.FRAUD_ENABLED || 'true') === 'true';
const JOURNEYS = [
  { fn: browse,     w: 40 },
  { fn: checkout,   w: 40 },
  { fn: errorProbe, w: 10 },
  ...(FRAUD_ENABLED ? [{ fn: fraudRun, w: 10 }] : []),
];

function pick() {
  const total = JOURNEYS.reduce((s, j) => s + j.w, 0);
  let r = Math.random() * total;
  for (const j of JOURNEYS) { r -= j.w; if (r <= 0) return j.fn; }
  return JOURNEYS[0].fn;
}

async function vu(id) {
  await sleep(id * (THINK_TIME / VUS));
  while (true) {
    try { await pick()(); } catch (e) {}
    await sleep(THINK_TIME);
  }
}

async function main() {
  console.log(`[load-gen] target=${TARGET} VUs=${VUS}`);
  await new Promise(r => setTimeout(r, 8000));  // espera serviços subirem
  for (let i = 0; i < 30; i++) {
    const r = await req('GET', '/health');
    if (r.status === 200) break;
    console.log(`[load-gen] aguardando gateway (${i+1}/30)`);
    await new Promise(r => setTimeout(r, 2000));
  }
  console.log(`[load-gen] iniciando ${VUS} VUs`);
  for (let i = 0; i < VUS; i++) vu(i);
}

main().catch(console.error);
