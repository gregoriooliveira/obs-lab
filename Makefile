.PHONY: help up base stack down splunk splunk-hec appd appd-db te all tunnel tunnel-url tunnel-named logs ps clean rebuild status hooks secrets-scan k8s-up k8s-status k8s-down

BASE    := docker compose -f docker-compose.yml
TUNNEL   = docker compose $(shell bash scripts/active-overlays.sh) -f docker-compose.tunnel.yml
TUNNELN  = docker compose $(shell bash scripts/active-overlays.sh) -f docker-compose.tunnel-named.yml

# STACK = base + TODOS os overlays que o .env habilita (ver scripts/active-overlays.sh).
# Subir vendor por vendor recriava os apps so com aquele overlay e apagava as env
# vars dos outros - o AppD parava de reportar sem erro no log. Por isso todo alvo
# de vendor sobe o conjunto inteiro.
STACK   = docker compose $(shell bash scripts/active-overlays.sh)

## Default: show help
help:
	@echo ""
	@echo "  obs-lab – Observability Lab Template"
	@echo ""
	@echo "  Usage: make <target>"
	@echo ""
	@echo "  Targets:"
	@echo "    up/stack   Sobe app + TODOS os vendores habilitados no .env"
	@echo "    base       So a app, sem vendor (derruba a instrumentacao)"
	@echo "    splunk     Start with Splunk O11y (metrics + APM + DBM)"
	@echo "    splunk-hec Same as splunk + logs to Splunk Core via HEC"
	@echo "    appd       Start with AppDynamics APM (set APPDYNAMICS_* in .env)"
	@echo "    appd-db    AppDynamics APM + Database Agent"
	@echo "    te         Start ThousandEyes Enterprise Agent"
	@echo "    all        All vendors (Splunk + AppD APM + AppD DB agent)"
	@echo "    tunnel     Start Cloudflare Quick Tunnel (free, no domain/token)"
	@echo "    tunnel-url Print the public *.trycloudflare.com URL"
	@echo "    tunnel-named  Named tunnel (needs domain + CLOUDFLARE_TUNNEL_TOKEN)"
	@echo "    logs       Follow logs from all containers"
	@echo "    ps         Show running containers"
	@echo "    status     Health check + prova de exportacao por vendor"
	@echo "    rebuild    Rebuild images and restart"
	@echo "    clean      Stop everything and remove volumes"
	@echo "    hooks      Install anti-secret-leak git pre-commit hook"
	@echo "    secrets-scan  Scan tracked files + history for secrets"
	@echo ""
	@echo "  Modo B (Kubernetes/kind) - atalhos pro k8s-lab.sh:"
	@echo "    k8s-up     Cluster kind + imagens + app + vendors"
	@echo "    k8s-status Nodes, pods, endpoints, contadores de export"
	@echo "    k8s-down   Destroi o cluster"
	@echo ""

## Sobe o lab com tudo que o .env habilita (alias de stack)
up: stack
	@echo ""
	@echo "  ✓ Demo app:        http://localhost:8080"
	@echo "  ✓ OTel zPages:     http://localhost:55679/debug/tracez"
	@echo "  ✓ Collector stats: http://localhost:8888/metrics"
	@echo ""

## SO a app, sem nenhum vendor (debug/stdout). Recria os containers sem as env
## vars dos agentes - use de proposito, nao no meio de uma demo.
base: _check-env
	@echo "  ! 'make base' derruba a instrumentacao dos vendores; volte com 'make stack'"
	$(BASE) up -d --build

## Sobe base + todos os overlays habilitados no .env (alvo canonico)
stack: _check-env
	@echo "  overlays ativos: $$(bash scripts/active-overlays.sh)"
	$(STACK) up -d --build
	@$(MAKE) --no-print-directory status

## Splunk Observability (metrics + APM + DBM)
splunk: _check-env _check-splunk stack
	@echo "  ✓ Metricas/APM/DBM -> Splunk Observability"

## Splunk Observability + logs para Splunk Core via HEC
splunk-hec: _check-env _check-splunk _check-hec stack
	@echo "  ✓ Metricas/APM/DBM -> Splunk Observability"
	@echo "  ✓ Logs -> Splunk Core via HEC"

## AppDynamics APM
appd: _check-env _check-appd stack
	@echo "  ✓ Exporting to AppDynamics"

## AppDynamics APM + Database Agent
appd-db: _check-env _check-appd stack
	@echo "  ✓ AppDynamics APM + DB Agent"

## ThousandEyes Enterprise Agent
te: _check-env _check-te stack
	@echo "  ✓ ThousandEyes Enterprise Agent"

## All vendors simultaneously (Splunk + AppDynamics APM + AppD DB agent)
all: _check-env _check-splunk _check-appd stack
	@echo "  ✓ Exporting to Splunk + AppDynamics"

## Cloudflare Quick Tunnel (gratis, sem dominio e sem token)
tunnel: _check-env
	$(TUNNEL) up -d
	@echo "  ✓ Quick Tunnel subindo – aguardando a URL publica..."
	@$(MAKE) --no-print-directory tunnel-url

## Imprime a URL publica do Quick Tunnel (o cloudflared so a anuncia no log)
tunnel-url:
	@for i in $$(seq 1 20); do url=$$(docker logs obs-cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1); if [ -n "$$url" ]; then echo "  $$url"; exit 0; fi; sleep 1; done; echo "  ✗ URL nao encontrada no log - veja: docker logs obs-cloudflared"; exit 1

## Cloudflare tunnel NOMEADO (hostname fixo; exige dominio na Cloudflare)
tunnel-named: _check-env _check-tunnel
	$(TUNNELN) up -d
	@echo "  ✓ Tunnel nomeado ativo – veja o hostname em Zero Trust > Networks > Tunnels"

## Follow logs (todos os overlays ativos)
logs:
	$(STACK) logs -f --tail=100

## Show running containers
ps:
	$(STACK) ps

## Hit main endpoints, conferir instrumentacao e exportacao real
status:
	@echo "=== Health check ==="
	@curl -sf http://localhost:8080/health       >/dev/null && echo "  gateway  /health        OK" || echo "  gateway  /health        FAIL"
	@curl -sf http://localhost:8081/health       >/dev/null && echo "  orders   /health        OK" || echo "  orders   /health        FAIL"
	@curl -sf http://localhost:8082/health       >/dev/null && echo "  payment  /health        OK" || echo "  payment  /health        FAIL"
	@curl -sf http://localhost:8080/api/products >/dev/null && echo "  gateway  /api/products  OK" || echo "  gateway  /api/products  FAIL"
	@curl -sf http://localhost:13133/            >/dev/null && echo "  otel-collector health   OK" || echo "  otel-collector health   n/a"
	@bash scripts/check-exports.sh

## Rebuild images
rebuild:
	$(STACK) up -d --build --force-recreate

## Full cleanup
clean:
	$(STACK) down -v --remove-orphans

# ── Internal guards ──────────────────────────────────────────────────────────
# .env e lido pelo docker compose automaticamente; aqui so validamos presenca.
# Cada linha de receita roda num shell proprio, por isso o source + test na
# mesma linha (set -a exporta tudo que o .env define).

_check-env:
	@test -f .env || (echo "  ✗ .env not found – copy .env.example to .env and fill in values" && exit 1)

_check-splunk:
	@set -a; . ./.env; set +a; test -n "$$SPLUNK_ACCESS_TOKEN" || \
		(echo "  ✗ SPLUNK_ACCESS_TOKEN not set in .env" && exit 1)

_check-hec:
	@set -a; . ./.env; set +a; test -n "$$SPLUNK_HEC_URL" -a -n "$$SPLUNK_HEC_TOKEN" || \
		(echo "  ✗ SPLUNK_HEC_URL / SPLUNK_HEC_TOKEN not set in .env" && exit 1)

_check-appd:
	@set -a; . ./.env; set +a; test -n "$$APPDYNAMICS_AGENT_ACCOUNT_NAME" || \
		(echo "  ✗ APPDYNAMICS_AGENT_ACCOUNT_NAME not set in .env" && exit 1)

_check-te:
	@set -a; . ./.env; set +a; test -n "$$TE_ACCOUNT_TOKEN" || \
		(echo "  ✗ TE_ACCOUNT_TOKEN not set in .env" && exit 1)

_check-tunnel:
	@set -a; . ./.env; set +a; test -n "$$CLOUDFLARE_TUNNEL_TOKEN" || \
		(echo "  ✗ CLOUDFLARE_TUNNEL_TOKEN not set in .env" && exit 1)

# ── Modo B: Kubernetes (kind) ────────────────────────────────────────────────
# Atalhos pro k8s-lab.sh, pra nao ter dois pontos de entrada na documentacao.
k8s-up: _check-env
	@./k8s-lab.sh up

k8s-status:
	@./k8s-lab.sh status

k8s-down:
	@./k8s-lab.sh down

# ── Seguranca / segredos ─────────────────────────────────────────────────────
hooks:
	@bash scripts/install-hooks.sh

## Varre arquivos versionados E todo o historico do git
secrets-scan:
	@bash scripts/secrets-scan.sh
