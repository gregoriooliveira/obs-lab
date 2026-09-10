#!/usr/bin/env bash
# Imprime a lista de "-f <arquivo>" de todos os overlays que o .env habilita.
#
# Por que existe: cada alvo do Makefile so recria os containers com os overlays
# que ele passa. Rodar "make splunk-hec" depois de "make appd" recriava
# gateway/orders/payment SEM as APPDYNAMICS_*, e o AppD parava de reportar sem
# erro nenhum no log. Montando a lista a partir do .env, qualquer alvo sobe o
# conjunto inteiro e nenhum vendor cai calado.
set -euo pipefail

cd "$(dirname "$0")/.."

files=("docker-compose.yml")

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

add() { [ -f "$1" ] && files+=("$1"); }

[ -n "${SPLUNK_ACCESS_TOKEN:-}" ]                                 && add docker-compose.splunk.yml
[ -n "${SPLUNK_HEC_URL:-}" ] && [ -n "${SPLUNK_HEC_TOKEN:-}" ]    && add docker-compose.splunk-hec.yml
[ -n "${APPDYNAMICS_AGENT_ACCOUNT_NAME:-}" ]                      && add docker-compose.appd.yml
[ -n "${APPDYNAMICS_DB_AGENT_NAME:-}${APPDYNAMICS_AGENT_ACCOUNT_NAME:-}" ] && add docker-compose.appd-db.yml
[ -n "${TE_ACCOUNT_TOKEN:-}" ]                                    && add docker-compose.thousandeyes.yml
# Tunel: entra na lista so depois de subido uma vez, pra nao ressuscitar o
# cloudflared (e trocar a URL publica) em todo "make status".
if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  add docker-compose.tunnel-named.yml
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^obs-cloudflared$'; then
  add docker-compose.tunnel.yml
fi

printf -- '-f %s ' "${files[@]}"
