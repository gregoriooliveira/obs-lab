.PHONY: help up down splunk datadog appd all logs ps clean rebuild status

BASE    := docker-compose -f docker-compose.yml
SPLUNK  := $(BASE) -f docker-compose.splunk.yml
DD      := $(BASE) -f docker-compose.datadog.yml
APPD    := $(BASE) -f docker-compose.appd.yml
ALL     := $(BASE) -f docker-compose.splunk.yml -f docker-compose.datadog.yml -f docker-compose.appd.yml

## Default: show help
help:
	@echo ""
	@echo "  obs-lab – Observability Lab Template"
	@echo ""
	@echo "  Usage: make <target>"
	@echo ""
	@echo "  Targets:"
	@echo "    up       Start base stack (app + collector debug output + load-gen)"
	@echo "    splunk   Start with Splunk Observability Cloud"
	@echo "    datadog  Start with Datadog"
	@echo "    appd     Start with AppDynamics (set APPDYNAMICS_* in .env)"
	@echo "    all      Start with all three vendors"
	@echo "    logs     Follow logs from all containers"
	@echo "    ps       Show running containers"
	@echo "    status   Health check all endpoints"
	@echo "    rebuild  Rebuild images and restart"
	@echo "    clean    Stop everything and remove volumes"
	@echo ""

## Base stack only (debug/stdout output)
up: _check-env
	$(BASE) up -d --build
	@echo ""
	@echo "  ✓ Demo app:        http://localhost:8080"
	@echo "  ✓ OTel zPages:     http://localhost:55679/debug/tracez"
	@echo "  ✓ Collector stats: http://localhost:8888/metrics"
	@echo ""

## Splunk Observability Cloud
splunk: _check-env _check-splunk
	$(SPLUNK) up -d --build
	@echo "  ✓ Exporting to Splunk Observability (realm: $(SPLUNK_REALM))"

## Datadog
datadog: _check-env _check-dd
	$(DD) up -d --build
	@echo "  ✓ Exporting to Datadog (site: $(DD_SITE))"

## AppDynamics (Cloud OTLP mode)
appd: _check-env _check-appd
	$(APPD) up -d --build
	@echo "  ✓ Exporting to AppDynamics ($(APPDYNAMICS_OTLP_ENDPOINT))"

## All vendors simultaneously
all: _check-env _check-splunk _check-dd _check-appd
	$(ALL) up -d --build
	@echo "  ✓ Exporting to Splunk + Datadog + AppDynamics"

## Follow logs
logs:
	$(BASE) logs -f --tail=100

## Show running containers
ps:
	$(BASE) ps

## Hit main endpoints and report HTTP status
status:
	@echo "=== Health check ==="
	@curl -sf http://localhost:8080/health         && echo "  /health          OK" || echo "  /health          FAIL"
	@curl -sf http://localhost:8080/api/products   && echo "  /api/products    OK" || echo "  /api/products    FAIL"
	@curl -sf http://localhost:13133/              && echo "  otel-collector   OK" || echo "  otel-collector   FAIL"

## Rebuild images
rebuild:
	$(BASE) up -d --build --force-recreate

## Full cleanup
clean:
	$(BASE) down -v --remove-orphans
	docker rmi obs-demo-app obs-load-gen 2>/dev/null || true

# ── Internal guards ──────────────────────────────────────────────────────────

_check-env:
	@test -f .env || (echo "  ✗ .env not found – copy .env.example to .env and fill in values" && exit 1)
	@export $$(cat .env | grep -v '^#' | xargs)

_check-splunk:
	@test -n "$$SPLUNK_ACCESS_TOKEN" || (source .env && test -n "$$SPLUNK_ACCESS_TOKEN") || \
		(echo "  ✗ SPLUNK_ACCESS_TOKEN not set in .env" && exit 1)

_check-dd:
	@test -n "$$DD_API_KEY" || (source .env && test -n "$$DD_API_KEY") || \
		(echo "  ✗ DD_API_KEY not set in .env" && exit 1)

_check-appd:
	@test -n "$$APPDYNAMICS_OTLP_ENDPOINT" || (source .env && test -n "$$APPDYNAMICS_OTLP_ENDPOINT") || \
		(echo "  ✗ APPDYNAMICS_OTLP_ENDPOINT not set in .env" && exit 1)
