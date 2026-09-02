#!/usr/bin/env bash
# obs-lab em Kubernetes (kind) - driver para Linux/macOS
#
#   ./k8s-lab.sh up        cria cluster, carrega imagens, sobe app + vendors
#   ./k8s-lab.sh cluster   so cria o cluster kind
#   ./k8s-lab.sh images    rebuild + kind load das imagens do app
#   ./k8s-lab.sh app       (re)aplica os manifests do app
#   ./k8s-lab.sh splunk    (re)instala o Splunk OTel Collector
#   ./k8s-lab.sh appd      (re)instala o AppDynamics Cluster Agent
#   ./k8s-lab.sh te        (re)aplica o ThousandEyes Enterprise Agent
#   ./k8s-lab.sh status    health check + contadores de export
#   ./k8s-lab.sh logs      logs do collector agent
#   ./k8s-lab.sh down      destroi o cluster
#
# Perfis (variavel LAB_PROFILE, ou no .env):
#   full (padrao)  3 nos, InfraViz da AppD ligado, ThousandEyes ligado  ~10 GB
#   lite           2 nos, InfraViz desligado,     ThousandEyes desligado ~5 GB
#
#   LAB_PROFILE=lite ./k8s-lab.sh up

set -euo pipefail
cd "$(dirname "$0")"

CLUSTER=obs-lab
CTX="kind-${CLUSTER}"
NS=obs-lab

# ── .env ─────────────────────────────────────────────────────────────────────
# `help` funciona sem .env; todo o resto precisa.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
elif [[ "${1:-help}" != "help" ]]; then
  echo "  ✗ .env nao encontrado - copie .env.example para .env e preencha" >&2
  exit 1
fi

LAB_PROFILE="${LAB_PROFILE:-full}"
case "$LAB_PROFILE" in
  full) KIND_CONFIG=k8s/kind-config.yaml;      APPD_INFRAVIZ=true;  DEPLOY_TE=true  ;;
  lite) KIND_CONFIG=k8s/kind-config-lite.yaml; APPD_INFRAVIZ=false; DEPLOY_TE=false ;;
  *) echo "  ✗ LAB_PROFILE invalido: $LAB_PROFILE (use full ou lite)" >&2; exit 1 ;;
esac
# knobs individuais sobrescrevem o perfil
APPD_INFRAVIZ="${APPD_INFRAVIZ_OVERRIDE:-$APPD_INFRAVIZ}"
DEPLOY_TE="${DEPLOY_TE_OVERRIDE:-$DEPLOY_TE}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "  ✗ $1 nao encontrado no PATH. Rode ./bootstrap.sh" >&2; exit 1; }; }
if [[ "${1:-help}" != "help" ]]; then
  for c in docker kubectl kind helm; do need "$c"; done
fi

require_var() {
  local n=$1
  [[ -n "${!n:-}" ]] || { echo "  ✗ $n nao definido no .env" >&2; exit 1; }
}

# ── etapas ───────────────────────────────────────────────────────────────────

make_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    echo "cluster '$CLUSTER' ja existe"
  else
    echo "--- criando cluster (perfil: $LAB_PROFILE) ---"
    kind create cluster --config "$KIND_CONFIG" --wait 120s
  fi
  kubectl config use-context "$CTX" >/dev/null
  kubectl get nodes -o wide
}

publish_images() {
  echo "--- build das imagens ---"
  docker compose -f docker-compose.yml build
  for i in obs-lab-gateway-service obs-lab-orders-service obs-lab-payment-service obs-lab-load-gen; do
    echo "--- kind load ${i}:latest ---"
    kind load docker-image "${i}:latest" --name "$CLUSTER"
  done
}

apply_secrets() {
  kubectl --context "$CTX" apply -f k8s/base/00-namespace.yaml >/dev/null

  kubectl --context "$CTX" -n "$NS" create secret generic obs-lab-db \
    --from-literal=DB_USER="${DB_USER:-obslab}" \
    --from-literal=DB_PASSWORD="${DB_PASSWORD:?defina DB_PASSWORD no .env}" \
    --from-literal=DB_NAME="${DB_NAME:-inventory}" \
    --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null

  kubectl --context "$CTX" -n "$NS" create secret generic appd-creds \
    --from-literal=APPDYNAMICS_CONTROLLER_HOST_NAME="${APPDYNAMICS_CONTROLLER_HOST_NAME:-}" \
    --from-literal=APPDYNAMICS_CONTROLLER_PORT="${APPDYNAMICS_CONTROLLER_PORT:-443}" \
    --from-literal=APPDYNAMICS_CONTROLLER_SSL_ENABLED="${APPDYNAMICS_CONTROLLER_SSL_ENABLED:-true}" \
    --from-literal=APPDYNAMICS_AGENT_ACCOUNT_NAME="${APPDYNAMICS_AGENT_ACCOUNT_NAME:-}" \
    --from-literal=APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY="${APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY:-}" \
    --from-literal=APPDYNAMICS_AGENT_APPLICATION_NAME="${APPDYNAMICS_AGENT_APPLICATION_NAME:-obs-lab}" \
    --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null

  if [[ -n "${TE_ACCOUNT_TOKEN:-}" ]]; then
    kubectl --context "$CTX" -n "$NS" create secret generic te-agent-token \
      --from-literal=TE_ACCOUNT_TOKEN="$TE_ACCOUNT_TOKEN" \
      --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null
  fi

  # mesma fonte de init SQL usada pelo docker-compose
  kubectl --context "$CTX" -n "$NS" create configmap db-init \
    --from-file=db/init \
    --dry-run=client -o yaml | kubectl --context "$CTX" apply -f - >/dev/null

  echo "secrets/configmap aplicados"
}

deploy_app() {
  apply_secrets
  kubectl --context "$CTX" apply -f k8s/base/
  for d in inventory-db gateway-service orders-service payment-service; do
    kubectl --context "$CTX" -n "$NS" rollout status "deploy/$d" --timeout=300s
  done
}

deploy_splunk() {
  require_var SPLUNK_ACCESS_TOKEN
  local realm="${SPLUNK_REALM:-us0}"
  helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart >/dev/null 2>&1 || true
  helm repo update >/dev/null

  # Logs de container (inclui os eventos JSON de fraude do gateway) so tem
  # destino se o HEC do Splunk Core estiver configurado - mesma regra do modo A.
  local hec=()
  if [[ -n "${SPLUNK_HEC_URL:-}" && -n "${SPLUNK_HEC_TOKEN:-}" ]]; then
    hec=(--set "splunkPlatform.endpoint=${SPLUNK_HEC_URL}"
         --set "splunkPlatform.token=${SPLUNK_HEC_TOKEN}"
         --set "splunkPlatform.index=${SPLUNK_HEC_INDEX:-main}"
         --set "splunkPlatform.logsEnabled=true"
         --set "splunkPlatform.insecureSkipVerify=true")
    echo "--- logs de container -> Splunk Core (HEC) ---"
  else
    echo "--- SPLUNK_HEC_* vazio: logs de container ficam so no kubectl logs ---"
  fi

  # o endpoint de DBM depende do realm; hardcodar us1 no values.yaml mandava
  # os eventos de query pro realm errado em qualquer conta fora de us1
  helm upgrade --install splunk-otel-collector \
    splunk-otel-collector-chart/splunk-otel-collector \
    --kube-context "$CTX" \
    --namespace splunk-otel --create-namespace \
    -f k8s/splunk/values.yaml \
    --set "splunkObservability.accessToken=${SPLUNK_ACCESS_TOKEN}" \
    --set "splunkObservability.realm=${realm}" \
    --set "clusterReceiver.config.receivers.postgresql.username=${DB_USER:-obslab}" \
    --set "clusterReceiver.config.receivers.postgresql.password=${DB_PASSWORD:?defina DB_PASSWORD no .env}" \
    --set "clusterReceiver.config.receivers.postgresql.databases[0]=${DB_NAME:-inventory}" \
    --set-string "clusterReceiver.config.exporters.otlp_http/dbmon.logs_endpoint=https://ingest.${realm}.observability.splunkcloud.com/v3/event" \
    "${hec[@]}" \
    --wait --timeout 10m
  kubectl --context "$CTX" -n splunk-otel get pods
}

deploy_appd() {
  require_var APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY
  # o repo antigo (appdynamics.github.io) responde 404; o atual e o artifactory
  helm repo add appdynamics-charts https://appdynamics.jfrog.io/artifactory/appdynamics-cloud-helmcharts >/dev/null 2>&1 || true
  helm repo update >/dev/null
  local url="https://${APPDYNAMICS_CONTROLLER_HOST_NAME}:${APPDYNAMICS_CONTROLLER_PORT:-443}"
  echo "--- AppD: InfraViz=${APPD_INFRAVIZ} ---"
  helm upgrade --install appd-cluster-agent \
    appdynamics-charts/cluster-agent \
    --kube-context "$CTX" \
    --namespace appdynamics --create-namespace \
    -f k8s/appd/values.yaml \
    --force-conflicts \
    --set "installInfraViz=${APPD_INFRAVIZ}" \
    --set "controllerInfo.url=${url}" \
    --set "controllerInfo.account=${APPDYNAMICS_AGENT_ACCOUNT_NAME}" \
    --set "controllerInfo.accessKey=${APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY}" \
    --set "controllerInfo.globalAccount=${APPDYNAMICS_AGENT_ACCOUNT_NAME}" \
    --timeout 10m
  kubectl --context "$CTX" -n appdynamics get pods
}

deploy_te() {
  if [[ "$DEPLOY_TE" != "true" ]]; then
    echo "ThousandEyes desligado no perfil $LAB_PROFILE (pule com DEPLOY_TE_OVERRIDE=true)"
    return 0
  fi
  require_var TE_ACCOUNT_TOKEN
  apply_secrets
  kubectl --context "$CTX" apply -f k8s/thousandeyes/agent.yaml
}

show_status() {
  echo "=== nodes ==="
  kubectl --context "$CTX" get nodes
  echo
  echo "=== pods ==="
  kubectl --context "$CTX" get pods -A -o wide \
    --field-selector metadata.namespace!=kube-system 2>/dev/null || true
  echo
  echo "=== endpoints ==="
  for u in "gateway /health|http://localhost:18080/health" \
           "gateway /api/products|http://localhost:18080/api/products"; do
    local label="${u%%|*}" url="${u##*|}"
    if curl -sf -o /dev/null --max-time 10 "$url"; then
      printf '  %-24s OK\n' "$label"
    else
      printf '  %-24s FAIL\n' "$label"
    fi
  done
  echo
  echo "=== export para os vendors ==="
  local agent cr
  agent=$(kubectl --context "$CTX" -n splunk-otel get pod -l component=otel-collector-agent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  cr=$(kubectl --context "$CTX" -n splunk-otel get pod -l component=otel-k8s-cluster-receiver -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  for pair in "agent|$agent|8889" "cluster-receiver|$cr|8899"; do
    local name="${pair%%|*}" rest="${pair#*|}" pod="${rest%%|*}" port="${rest##*|}"
    [[ -n "$pod" ]] || continue
    echo "  [$name]"
    kubectl --context "$CTX" -n splunk-otel port-forward "$pod" "1${port}:${port}" >/dev/null 2>&1 &
    local pf=$!
    sleep 4
    curl -s --max-time 10 "http://localhost:1${port}/metrics" 2>/dev/null \
      | grep -E '^otelcol_exporter_(sent|send_failed)_(spans|metric_points|log_records)\{' \
      | sed 's/^/    /' || echo "    (sem dados)"
    kill "$pf" 2>/dev/null || true
    wait "$pf" 2>/dev/null || true
  done
}

usage() {
  cat <<'EOF'

  obs-lab em Kubernetes (kind)

    up        cria cluster + carrega imagens + sobe tudo
    cluster   so cria o cluster kind
    images    rebuild + kind load
    app       (re)aplica manifests do app
    splunk    (re)instala o Splunk OTel Collector
    appd      (re)instala o AppD Cluster Agent
    te        (re)aplica o ThousandEyes Enterprise Agent
    status    health check + contadores
    logs      logs do collector agent
    down      destroi o cluster

  Perfis:  LAB_PROFILE=full (padrao, ~10 GB) | lite (~5 GB)

  gateway no host: http://localhost:18080

EOF
}

case "${1:-help}" in
  cluster) make_cluster ;;
  images)  publish_images ;;
  app)     deploy_app ;;
  splunk)  deploy_splunk ;;
  appd)    deploy_appd ;;
  te)      deploy_te ;;
  status)  show_status ;;
  logs)    kubectl --context "$CTX" -n splunk-otel logs -l component=otel-collector-agent -c otel-collector --tail=100 -f ;;
  down)    kind delete cluster --name "$CLUSTER" ;;
  up)
    make_cluster
    publish_images
    deploy_app
    deploy_splunk
    deploy_appd
    deploy_te
    show_status
    ;;
  help|*) usage ;;
esac
