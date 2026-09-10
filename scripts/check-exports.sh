#!/usr/bin/env bash
# Confere se cada vendor habilitado no .env esta REALMENTE exportando.
#
# "Container Up" nao prova nada: ja aconteceu de o AppD ficar horas com os tres
# containers de pe e o application vazio no controller, porque um "make" de outro
# vendor recriou os apps sem as APPDYNAMICS_*. Aqui olhamos evidencia: env var
# dentro do container, contador do collector e log do agente.
set -uo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; rc=1; }
skip() { printf '    - %s\n' "$1"; }
rc=0

metrics() { curl -s --max-time 5 http://localhost:8888/metrics; }
counter() { metrics | grep -E "^$1" | awk '{s+=$NF} END {printf "%.0f", s+0}'; }

echo "=== Exportacao ==="

if [ -n "${SPLUNK_ACCESS_TOKEN:-}" ]; then
  sent=$(counter 'otelcol_exporter_sent_spans')
  fails=$(counter 'otelcol_exporter_send_failed_spans')
  [ "${sent:-0}" -gt 0 ] 2>/dev/null \
    && ok "Splunk O11y: $sent spans enviados (falhas: ${fails:-0})" \
    || fail "Splunk O11y: nenhum span enviado - veja 'docker logs obs-splunk-otel'"
fi

if [ -n "${SPLUNK_HEC_URL:-}" ] && [ -n "${SPLUNK_HEC_TOKEN:-}" ]; then
  sent=$(counter 'otelcol_exporter_sent_log_records\{exporter="splunk_hec/core"')
  fails=$(counter 'otelcol_exporter_send_failed_log_records\{exporter="splunk_hec/core"')
  [ "${sent:-0}" -gt 0 ] 2>/dev/null \
    && ok "Splunk HEC: $sent logs entregues (falhas: ${fails:-0})" \
    || fail "Splunk HEC: nada entregue - HTTP x HTTPS ou token; 'docker logs obs-splunk-otel | grep splunk_hec'"
fi

if [ -n "${APPDYNAMICS_AGENT_ACCOUNT_NAME:-}" ]; then
  missing=""
  for c in obs-gateway obs-orders obs-payment; do
    docker exec "$c" env 2>/dev/null | grep -q '^APPDYNAMICS_AGENT_TIER_NAME=' || missing="$missing $c"
  done
  if [ -n "$missing" ]; then
    fail "AppD APM: sem env do agente em$missing - suba com 'make stack'"
  elif docker exec obs-gateway sh -c 'ls /tmp/appd/*/appd_node_agent_*.log' >/dev/null 2>&1; then
    ok "AppD APM: agente carregado nos 3 tiers"
  else
    fail "AppD APM: env presente mas agente nao subiu - 'docker logs obs-gateway | head'"
  fi
  docker logs --tail=200 obs-appd-db-agent 2>&1 | grep -q 'Registered DB_AGENT' \
    && ok "AppD DB Agent: registrado" \
    || skip "AppD DB Agent: sem linha de registro nas ultimas 200 do log"
fi

if [ -n "${TE_ACCOUNT_TOKEN:-}" ]; then
  if docker exec obs-te-agent grep -q 'Invalid account token' /var/log/agent/te-agent.log 2>/dev/null; then
    fail "ThousandEyes: token recusado pelo portal (agente nunca registra)"
  elif docker exec obs-te-agent test -f /var/lib/te-agent/identity.json 2>/dev/null; then
    ok "ThousandEyes: agente registrado"
  else
    fail "ThousandEyes: sem identity.json - 'docker exec obs-te-agent tail /var/log/agent/te-agent.log'"
  fi
fi

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^obs-cloudflared$'; then
  url=$(docker logs obs-cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
  [ -n "$url" ] && ok "Tunnel: $url" || fail "Tunnel: URL ainda nao anunciada no log"
fi

exit $rc
