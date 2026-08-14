#!/usr/bin/env bash
# obs-lab - bootstrap de um servidor Linux limpo.
#
# Instala o que faltar: Docker Engine + plugin compose, kubectl, kind, helm.
# Idempotente: o que ja estiver instalado e pulado.
#
#   sudo ./bootstrap.sh          # instala tudo
#   ./bootstrap.sh --check       # so verifica, nao instala
#
# Depois:
#   cp .env.example .env && vi .env     # preencher credenciais
#   make all                            # lab em docker-compose
#   ./k8s-lab.sh up                     # lab em Kubernetes (kind)

set -euo pipefail

CHECK_ONLY=false
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

KUBECTL_VERSION="${KUBECTL_VERSION:-v1.34.1}"
KIND_VERSION="${KIND_VERSION:-v0.32.0}"
HELM_VERSION="${HELM_VERSION:-v3.19.0}"

ARCH_RAW=$(uname -m)
case "$ARCH_RAW" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "  ✗ arquitetura nao suportada: $ARCH_RAW" >&2; exit 1 ;;
esac

if [[ "$ARCH" == "arm64" ]]; then
  cat >&2 <<'EOF'
  ! ATENCAO: arquitetura arm64.
    O Splunk OTel Collector e os apps do lab rodam em arm64, mas as imagens de
    agente da AppDynamics e do ThousandEyes sao publicadas so em amd64.
    Nesse caso use LAB_PROFILE=lite e mantenha so o Splunk.
EOF
fi

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
miss() { printf '  \033[33m•\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "instalacao precisa de root. Rode: sudo ./bootstrap.sh"
}

have() { command -v "$1" >/dev/null 2>&1; }

# ── Docker ───────────────────────────────────────────────────────────────────
install_docker() {
  if have docker && docker compose version >/dev/null 2>&1; then
    ok "docker $(docker --version | awk '{print $3}' | tr -d ,) + compose"
    return
  fi
  miss "docker ausente"
  $CHECK_ONLY && return
  need_root
  echo "    instalando Docker Engine (script oficial get.docker.com)..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  systemctl enable --now docker
  # quem chamou via sudo passa a usar docker sem sudo (requer relogin)
  if [[ -n "${SUDO_USER:-}" ]]; then
    usermod -aG docker "$SUDO_USER"
    echo "    '$SUDO_USER' adicionado ao grupo docker - faca logout/login"
  fi
  ok "docker instalado"
}

# ── kubectl ──────────────────────────────────────────────────────────────────
install_kubectl() {
  if have kubectl; then ok "kubectl $(kubectl version --client -o json 2>/dev/null | grep -o '"gitVersion":"[^"]*"' | head -1 | cut -d'"' -f4)"; return; fi
  miss "kubectl ausente"
  $CHECK_ONLY && return
  need_root
  curl -fsSLo /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"
  chmod +x /usr/local/bin/kubectl
  ok "kubectl ${KUBECTL_VERSION} instalado"
}

# ── kind ─────────────────────────────────────────────────────────────────────
install_kind() {
  if have kind; then ok "kind $(kind version | awk '{print $2}')"; return; fi
  miss "kind ausente"
  $CHECK_ONLY && return
  need_root
  curl -fsSLo /usr/local/bin/kind \
    "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
  chmod +x /usr/local/bin/kind
  ok "kind ${KIND_VERSION} instalado"
}

# ── helm ─────────────────────────────────────────────────────────────────────
install_helm() {
  if have helm; then ok "helm $(helm version --short 2>/dev/null)"; return; fi
  miss "helm ausente"
  $CHECK_ONLY && return
  need_root
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" -o /tmp/helm.tgz
  tar -xzf /tmp/helm.tgz -C /tmp
  install -m 0755 "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm
  rm -rf /tmp/helm.tgz "/tmp/linux-${ARCH}"
  ok "helm ${HELM_VERSION} instalado"
}

# ── sysctl exigidos pelo kind ────────────────────────────────────────────────
tune_sysctl() {
  local want_watches=524288 want_instances=512
  local cur_watches cur_instances
  cur_watches=$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo 0)
  cur_instances=$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo 0)
  if (( cur_watches >= want_watches && cur_instances >= want_instances )); then
    ok "inotify (watches=$cur_watches instances=$cur_instances)"
    return
  fi
  miss "inotify baixo (watches=$cur_watches instances=$cur_instances)"
  $CHECK_ONLY && return
  need_root
  # kind com varios nos estoura o default e os pods entram em CrashLoop com
  # "too many open files" - a doc do kind pede exatamente estes valores.
  cat > /etc/sysctl.d/99-kind.conf <<EOF
fs.inotify.max_user_watches = $want_watches
fs.inotify.max_user_instances = $want_instances
EOF
  sysctl --system >/dev/null
  ok "inotify ajustado"
}

# ── recursos da maquina ──────────────────────────────────────────────────────
check_resources() {
  local mem_gb cpus disk_gb
  mem_gb=$(awk '/MemTotal/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  cpus=$(nproc)
  disk_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')

  echo "  recursos: ${cpus} vCPU, ${mem_gb} GB RAM, ${disk_gb} GB livres em /"
  local warn=0
  (( cpus   >= 4  )) || { printf '    \033[33m! CPU abaixo do recomendado (4+)\033[0m\n'; warn=1; }
  (( mem_gb >= 16 )) || { printf '    \033[33m! RAM abaixo do recomendado pro perfil full (16 GB+). Use LAB_PROFILE=lite\033[0m\n'; warn=1; }
  (( disk_gb >= 40 )) || { printf '    \033[33m! disco abaixo do recomendado (40 GB+)\033[0m\n'; warn=1; }
  (( warn == 0 )) && ok "recursos suficientes pro perfil full"
}

echo ""
echo "  obs-lab - bootstrap ($([[ $CHECK_ONLY == true ]] && echo 'somente verificacao' || echo 'instalacao'))"
echo ""
check_resources
install_docker
install_kubectl
install_kind
install_helm
tune_sysctl
echo ""

if [[ ! -f .env ]]; then
  printf '  \033[33m•\033[0m .env ausente. Proximo passo:\n'
  echo "      cp .env.example .env && \${EDITOR:-vi} .env"
else
  ok ".env presente"
fi

cat <<'EOF'

  Subir o lab:
    make all          docker-compose (app + Splunk + AppDynamics)
    ./k8s-lab.sh up   Kubernetes (kind) - navigators de K8s no Splunk

EOF
