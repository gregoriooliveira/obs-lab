'use strict';
// Load generator – bate no gateway. Gera trace distribuído gateway→orders→payment.

const TARGET      = process.env.TARGET_URL        || 'http://gateway-service:8080';
const VUS         = parseInt(process.env.VIRTUAL_USERS   || '8',  10);
const THINK_TIME  = parseInt(process.env.THINK_TIME_MS   || '400', 10);
const STATS_EVERY = parseInt(process.env.STATS_INTERVAL_S || '30', 10) * 1000;

const PRODUCTS = ['prod-001','prod-002','prod-003','prod-004','prod-005'];
const METHODS  = ['card','pix','boleto'];

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

async function req(method, path, body) {
  const start = Date.now();
  try {
    const r = await fetch(`${TARGET}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(10000),
    });
    return { status: r.status, ms: Date.now() - start };
  } catch (e) {
    return { status: 0, ms: Date.now() - start };
  }
}

async function browse() {
  let r = await req('GET', '/api/products'); record('browse', r.status, r.ms);
  await sleep(THINK_TIME);
  r = await req('GET', `/api/products/${rand(PRODUCTS)}`); record('browse', r.status, r.ms);
}

async function checkout() {
  await browse();
  await sleep(THINK_TIME);
  const items = Array.from({ length: rand([1,2,3]) }, () => ({
    productId: rand(PRODUCTS), qty: 1, price: parseFloat((Math.random()*80+10).toFixed(2)),
  }));
  const r = await req('POST', '/api/checkout', {
    items, customerId: `cust-${rand(['001','002','003','004'])}`, paymentMethod: rand(METHODS),
  });
  record('checkout', r.status, r.ms);
}

async function errorProbe() {
  const r = await req('GET', rand(['/api/debug/error', '/api/debug/slow?ms=1500']));
  record('error_probe', r.status, r.ms);
}

const JOURNEYS = [
  { fn: browse,     w: 45 },
  { fn: checkout,   w: 45 },
  { fn: errorProbe, w: 10 },
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
