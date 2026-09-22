# Painel unificado no Splunk

A tese: **o cliente pode ter N fontes na ponta — inclusive outro APM — e usar o
Splunk como o painel unico.** Este documento diz por onde cada fonte entra, o
que ela habilita, e o que ainda falta.

O painel e `obs_lab_panorama`, view default do app `obs_lab_fraud`.

---

## O mapa

```
                    ┌──────────────────────────────────────────┐
  App Node.js  ────▶│ OTel Collector                           │
  (3 servicos)      │                                          │
                    │  logs    ──filelog────▶ splunk_hec/core  │──▶ index=lab
  Docker/host  ────▶│  metrics ──host/docker/pg/otlp──┬────────│──▶ index=lab_metrics
  Postgres     ────▶│                                 └────────│──▶ Observability Cloud
                    │  traces  ──otlp─────────────────────────▶│──▶ Observability Cloud (APM)
                    └──────────────────────────────────────────┘

  ThousandEyes ──── stream nativo ──────────────────────────────▶ index=te

  Observability ─── SIM Add-on (SignalFlow) ────────────────────▶ index=o11y   [A FAZER]
  Cloud            (Synthetics, APM, detectores)

  AppDynamics  ──── Add-on / REST do controller ────────────────▶ index=appd   [A FAZER]
```

---

## Fonte 1 — App, infra e banco (OTel Collector) ✅

**Como entra:** o collector ja roda no lab. O overlay `config-hec.yml` tem dois
exporters `splunk_hec`: um para logs (`index=lab`) e um para metricas
(`index=lab_metrics`).

**O que chega de metrica:**

| Origem | Metricas |
|---|---|
| App (OTLP) | `http.requests.total`, `http.errors.total`, `http.request.duration`, `orders.created.total`, `orders.failed.total`, `orders.db.duration`, `payment.processed.total`, `payment.failed.total`, `payment.revenue.total`, `security.fraud.total`, `security.auth.total` |
| docker_stats | `container.cpu.utilization`, `container.memory.usage.total`, `container.network.io.usage.*`, `container.blockio.*` |
| host_metrics | `system.cpu.*`, `system.memory.*`, `system.disk.*`, `system.network.*`, `system.filesystem.*` |
| postgresql | `postgresql.*` |

**Ponto de atencao — counters sao cumulativos.** O exporter `signalfx` converte
cumulativo para delta; o `splunk_hec` **nao**. Entao no Splunk os `.total`
chegam como contador que so cresce. Para taxa use `rate()`, nao `sum()`:

```spl
| mstats rate(_value) as rps WHERE index=lab_metrics metric_name="http.requests.total" span=1m
```

Gauges (`container.cpu.utilization`, `system.memory.utilization`) usam `avg()`
normalmente.

**Pre-requisito ja resolvido:** indice `lab_metrics` criado com
`datatype=metric` e liberado no token HEC `obs-lab`. Num indice de eventos o
HEC responde `200` e o dado some, sem erro no log do collector.

---

## Fonte 2 — ThousandEyes ✅

**Como entra:** stream nativo do TE para o HEC, `index=te`, token `te-hec`.

**Detalhe que muda a SPL:** `te` e um indice de **eventos**, nao de metricas.
Os valores chegam em campos com prefixo `metric_name:`. Entao `mstats` nao
funciona — e `rename` + `stats`:

```spl
index=te sourcetype="cisco:thousandeyes:metric"
| rename "metric_name:network.latency" as lat, "thousandeyes.source.agent.name" as agente
| timechart span=5m avg(lat) by agente
```

Campos uteis: `metric_name:http.server.request.availability`,
`metric_name:network.latency`, `metric_name:network.loss`,
`metric_name:http.client.request.duration`, `thousandeyes.permalink`
(link direto para o teste no TE).

---

## Fonte 3 — Observability Cloud ⏳ A FAZER

O que **so existe no o11y** e nao passa pelo collector:

- **Splunk Synthetics** (`synthetics.run.uptime.percent`,
  `synthetics.run.duration.time.ms`, `synthetics.ttfb.time.ms`)
- Metricas derivadas de **APM** (RED por servico, calculadas no o11y)
- Estado de **detectores e alertas**

**Caminho:** **Splunk Infrastructure Monitoring Add-on** — Splunkbase 4232.

Ele instala no Splunk Enterprise e traz duas coisas:

1. O comando de busca `| sim`, que roda **SignalFlow de dentro do Splunk**:

   ```spl
   | sim signalflow="data('synthetics.run.uptime.percent').mean(by=['test']).publish()"
         earliest=-1h latest=now
   ```

2. Inputs modulares que replicam metricas do o11y para um indice do Splunk
   (sugestao: `o11y`, tambem `datatype=metric`), para historico proprio.

**Precisa de:**
- Um **token de API da org** — o11y > Settings > Access Tokens. **Nao** e o
  `SPLUNK_ACCESS_TOKEN` do `.env`: aquele e token de *ingest*, este precisa de
  escopo de API.
- O realm: `us1`.

---

## Fonte 4 — AppDynamics (o "outro APM") ⏳ A FAZER

E o ponto mais forte da narrativa: o cliente nao precisa trocar de APM para
ter painel unico.

Dois caminhos, nenhum testado neste lab ainda:

1. **AppDynamics Add-on for Splunk** (Splunkbase) — input modular que consulta
   a REST do controller e indexa metricas e eventos.
2. **REST do controller direto**, num input de script:
   `/controller/rest/applications/obs-lab/metric-data?metric-path=...&output=JSON`.
   Precisa de usuario de API do controller.

Destino sugerido: `index=appd`.

---

## O que o painel mostra hoje

| Bloco | Fonte | Estado |
|---|---|---|
| Negocio — checkout, taxa de sucesso, pedidos, fraude bloqueada | `index=lab` logger=http | ✅ |
| Experiencia externa — disponibilidade, latencia, perda por agente | `index=te` | ✅ |
| Aplicacao — p95, erros por rota, erros com `trace_id` | `index=lab` | ✅ |
| Seguranca — ameacas no tempo, risco acumulado por IP | `index=lab` logger=security | ✅ |
| Aplicacao (metricas) — RPS, erros, receita, pedidos | `index=lab_metrics` | ✅ |
| Infraestrutura — CPU/memoria por container, host | `index=lab_metrics` | ✅ |
| Sinteticos — uptime, duracao, TTFB | SIM Add-on | ⏳ |
| APM de terceiro | AppDynamics Add-on | ⏳ |

---

## Medido em 2026-09-22, depois do primeiro `make rebuild`

**76 metricas** chegaram em `lab_metrics`: 11 da aplicacao (OTLP), 13 de
container (docker_stats), 21 de host (host_metrics), 20 do Postgres, mais os
histogramas quebrados em `_bucket`/`_count`/`_sum`.

Dimensoes de `http.requests.total` — sao ricas, da para quebrar o painel:

```
service.name, route, method, status, deployment.environment,
host.arch, os.type, process.*, telemetry.sdk.*
```

`BY` com nome pontuado funciona **entre aspas**: `BY "service.name", route, status`.

Valores conferidos: 14,7 req/s no total (21,6 de pico), 5,7 pedidos/s criados
e 3,6 falhados, 117.506 USD de receita acumulada.

### Duas armadilhas que aparecem so com dado real

**Histogramas viram tres metricas.** `http.request.duration` nao existe em
`lab_metrics` — existem `http.request.duration_bucket`, `_count` e `_sum`. E o
formato Prometheus. Para media: `_sum / _count`.

**`max(_value) - min(_value)` em counter da numero errado.** O painel de
receita usava isso e reportou 43.500 USD contra 117.506 reais. Duas causas
somadas: sao **3 series** (uma por `method`) e o restart do container
**zera o counter**. O certo e `latest(_value)` por serie e somar:

```spl
| mstats latest(_value) as v WHERE index=lab_metrics metric_name="payment.revenue.total" BY method
| stats sum(v) as receita
```

## Conferir depois do `make rebuild`

Primeiro, o que chegou:

```spl
| mstats avg(_value) WHERE index=lab_metrics BY metric_name span=5m | stats count by metric_name
```

Depois, as dimensoes de uma metrica da app — e delas que sai o `BY` dos
graficos:

```spl
| mstats avg(_value) WHERE index=lab_metrics metric_name="http.requests.total" span=5m BY service.name
```

Se `service.name` vier vazio, liste as dimensoes reais:

```spl
| mcatalog values(_dims) WHERE index=lab_metrics metric_name="http.requests.total"
```

Os nomes de dimensao dependem de como o exporter mapeia os resource
attributes; e o unico ponto do painel que pode precisar de ajuste fino.
