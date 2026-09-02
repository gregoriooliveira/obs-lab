.PHONY: help up down splunk splunk-hec appd appd-db te all tunnel logs ps clean rebuild status hooks secrets-scan k8s-up k8s-status k8s-down

BASE    := docker compose -f docker-compose.yml
SPLUNK  := $(BASE) -f docker-compose.splunk.yml
HEC     := $(SPLUNK) -f docker-compose.splunk-hec.yml
APPD    := $(BASE) -f docker-compose.appd.yml
APPDDB  := $(APPD) -f docker-compose.appd-db.yml
TE      := $(BASE) -f docker-compose.thousandeyes.yml
TUNNEL  := $(BASE) -f docker-compose.tunnel.yml
ALL     := $(BASE) -f docker-compose.splunk.yml -f docker-compose.appd.yml -f docker-compose.appd-db.yml

## Default: show help
help:
	@echo ""
	@echo "  obs-lab – Observability Lab Template"
	@echo ""
	@echo "  Usage: make <target>"
	@echo ""
	@echo "  Targets:"
	@echo "    up         Start base stack (app + postgres + load-gen)"
	@echo "    splunk     Start with Splunk O11y (metrics + APM + DBM)"
	@echo "    splunk-hec Same as splunk + logs to Splunk Core via HEC"
	@echo "    appd       Start with AppDynamics APM (set APPDYNAMICS_* in .env)"
	@echo "    appd-db    AppDynamics APM + Database Agent"
	@echo "    te         Start ThousandEyes Enterprise Agent"
	@echo "    all        All vendors (Splunk + AppD APM + AppD DB agent)"
	@echo "    tunnel     Start Cloudflare tunnel (publish URLs to internet)"
	@echo "    logs       Follow logs from all containers"
	@echo "    ps         Show running containers"
	@echo "    status     Health check all endpoints"
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

## Base stack only (debug/stdout output)
up: _check-env
	$(BASE) up -d --build
	@echo ""
	@echo "  ✓ Demo app:        http://localhost:8080"
	@echo "  ✓ OTel zPages:     http://localhost:55679/debug/tracez"
	@echo "  ✓ Collector stats: http://localhost:8888/metrics"
	@echo ""

## Splunk Observability (metrics + APM + DBM)
splunk: _check-env _check-splunk
	$(SPLUNK) up -d --build
	@echo "  ✓ Metricas/APM/DBM -> Splunk Observability"

## Splunk Observability + logs para Splunk Core via HEC
splunk-hec: _check-env _check-splunk _check-hec
	$(HEC) up -d --build
	@echo "  ✓ Metricas/APM/DBM -> Splunk Observability"
	@echo "  ✓ Logs -> Splunk Core via HEC"

## AppDynamics APM
appd: _check-env _check-appd
	$(APPD) up -d --build
	@echo "  ✓ Exporting to AppDynamics"

## AppDynamics APM + Database Agent
appd-db: _check-env _check-appd
	$(APPDDB) up -d --build
	@echo "  ✓ AppDynamics APM + DB Agent"

## ThousandEyes Enterprise Agent
te: _check-env _check-te
	$(TE) up -d --build
	@echo "  ✓ ThousandEyes Enterprise Agent"

## All vendors simultaneously (Splunk + AppDynamics APM + AppD DB agent)
all: _check-env _check-splunk _check-appd
	$(ALL) up -d --build
	@echo "  ✓ Exporting to Splunk + AppDynamics"

## Cloudflare tunnel (publish to internet)
tunnel: _check-env _check-tunnel
	$(TUNNEL) up -d
	@echo "  ✓ Cloudflare tunnel ativo – URLs publicadas (veja Public Hostname no Zero Trust)"

## Follow logs (todos os overlays)
logs:
	$(ALL) logs -f --tail=100

## Show running containers
ps:
	$(ALL) ps

## Hit main endpoints and report HTTP status
status:
	@echo "=== Health check ==="
	@curl -sf http://localhost:8080/health       >/dev/null && echo "  gateway  /health        OK" || echo "  gateway  /health        FAIL"
	@curl -sf http://localhost:8081/health       >/dev/null && echo "  orders   /health        OK" || echo "  orders   /health        FAIL"
	@curl -sf http://localhost:8082/health       >/dev/null && echo "  payment  /health        OK" || echo "  payment  /health        FAIL"
	@curl -sf http://localhost:8080/api/products >/dev/null && echo "  gateway  /api/products  OK" || echo "  gateway  /api/products  FAIL"
	@curl -sf http://localhost:13133/            >/dev/null && echo "  otel-collector health   OK" || echo "  otel-collector health   n/a"

## Rebuild images
rebuild:
	$(BASE) up -d --build --force-recreate

## Full cleanup
clean:
	$(ALL) down -v --remove-orphans

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
