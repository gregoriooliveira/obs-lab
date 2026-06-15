# obs-lab – Cadeia de microserviços observável

Lab multi-vendor (Splunk, Datadog, AppDynamics) sobre uma cadeia de 3 microserviços
Node.js que se chamam via HTTP, gerando traces distribuídos reais.

## Arquitetura

```
load-gen → gateway-service (8080) → orders-service (8081) → payment-service (8082)
                  │                        │                       │
                  └────────────────────────┴───────────────────────┘
                         OTel SDK → Splunk OTel Collector → Splunk
                         AppD agent → Controller (3 tiers)
                                              │
                  dd-agent (infra) ───────────┴─→ Datadog
                  appd-machine-agent (infra) ──→ AppD SIM
```

### Fluxo de uma compra (trace distribuído)
```
POST /api/checkout (gateway)
  └─ checkout span
      └─ HTTP POST orders-service /orders
          └─ order.create span
              ├─ inventory.reserve span
              └─ HTTP POST payment-service /charge
                  └─ payment.charge span
                      └─ acquirer.authorize span
```
Um único trace atravessa os 3 serviços — visível como service map nos 3 vendors.

## Serviços

| Serviço | Porta | Papel | Chama |
|---------|-------|-------|-------|
| gateway-service | 8080 | Entrada, catálogo, roteamento | orders |
| orders-service | 8081 | Cria pedido, valida estoque | payment |
| payment-service | 8082 | Processa pagamento (adquirente sim.) | — |

## Endpoints (gateway)

| Método | Path | Descrição |
|--------|------|-----------|
| GET | /api/products | Lista catálogo |
| GET | /api/products/:id | Produto por id |
| POST | /api/checkout | Compra → orders → payment (trace distribuído) |
| GET | /api/debug/error | 500 forçado |
| GET | /api/debug/slow?ms=N | Lento |
| GET | /health | Health |

## Subir

```bash
cp .env.example .env   # preencher credenciais
# Todos os vendors:
docker compose -f docker-compose.yml -f docker-compose.all.yml up -d --build
# Individual:
docker compose -f docker-compose.yml -f docker-compose.splunk.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.datadog.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.appd.yml up -d --build
```

## Validar

```bash
docker compose ps
curl http://localhost:8080/api/products
curl -X POST http://localhost:8080/api/checkout -H "Content-Type: application/json" \
  -d '{"items":[{"productId":"prod-001","qty":2,"price":19.99}],"paymentMethod":"pix"}'
```

No AppD aparecem 3 tiers (gateway, orders, payment) com flow map entre eles.
No Splunk APM aparece o service map gateway→orders→payment.

## Notas
- AppD Machine Agent não roda no Docker Desktop Windows (limitação de socket). Funciona no Linux/ESXi.
- Datadog free tier: só infra, sem APM.
- Splunk: traces + métricas; Infrastructure Navigator popula melhor no Linux.
