'use strict';
// Log estruturado - uma linha JSON por evento, com os nomes de campo que o
// Splunk Observability espera no Log Observer Connect:
//
//   severity                 -> coluna Severity (sem ela tudo vira "unknown")
//   trace_id / span_id       -> Related Content: liga a linha de log ao trace
//                               do APM e ao span exato
//   service.name             -> casa com o service do APM
//   deployment.environment   -> casa com o environment do APM
//   host.name                -> casa com o host da Infra
//
// O Splunk extrai esses campos sozinho (auto-KV de JSON), entao nao precisa de
// props.conf nem de mudanca no collector: basta a aplicacao emitir os nomes
// certos.
const os = require('os');
const { trace, isSpanContextValid } = require('@opentelemetry/api');

const SERVICE     = process.env.OTEL_SERVICE_NAME || 'unknown-service';
const ENVIRONMENT = process.env.ENVIRONMENT || 'lab';
const HOST        = process.env.HOSTNAME_LAB || os.hostname();
// So existe no K8s (downward API). Fecha o Related Content com a Infra do o11y.
const POD         = process.env.POD_NAME || '';

// spanCtx explicito: em callbacks como res.on('finish') o span do request ja
// saiu do contexto ativo. Quem chama captura o contexto no momento certo.
function emit(severity, message, fields = {}, spanCtx) {
  const rec = {
    ts: new Date().toISOString(),
    severity,                       // INFO | WARN | ERROR
    level: severity,                // alias: algumas orgs mapeiam "level"
    message,
    'service.name': SERVICE,
    service: SERVICE,               // mantido: buscas antigas do app usam este
    'deployment.environment': ENVIRONMENT,
    'host.name': HOST,
    ...(POD ? { 'k8s.pod.name': POD } : {}),
    ...fields,
  };

  const ctx = spanCtx || trace.getActiveSpan()?.spanContext();
  if (ctx && isSpanContextValid(ctx)) {
    rec.trace_id = ctx.traceId;
    rec.span_id  = ctx.spanId;
  }

  console.log(JSON.stringify(rec));
}

const info  = (msg, f, c) => emit('INFO',  msg, f, c);
const warn  = (msg, f, c) => emit('WARN',  msg, f, c);
const error = (msg, f, c) => emit('ERROR', msg, f, c);

// Access log por request. O contexto do span e capturado na entrada (onde a
// instrumentacao HTTP do OTel ainda e o span ativo) e usado no 'finish'.
function httpMiddleware() {
  return (req, res, next) => {
    const spanCtx = trace.getActiveSpan()?.spanContext();
    const t0 = process.hrtime.bigint();
    res.on('finish', () => {
      const ms = Number(process.hrtime.bigint() - t0) / 1e6;
      const sev = res.statusCode >= 500 ? 'ERROR' : res.statusCode >= 400 ? 'WARN' : 'INFO';
      emit(sev, `${req.method} ${req.originalUrl} ${res.statusCode}`, {
        logger: 'http',
        http_method: req.method,
        http_path: req.path,
        http_status: res.statusCode,
        duration_ms: Math.round(ms),
      }, spanCtx);
    });
    next();
  };
}

module.exports = { emit, info, warn, error, httpMiddleware };
