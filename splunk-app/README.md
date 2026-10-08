# obs_lab_fraud — app de detecção de fraude para Splunk Enterprise

Detecta os 8 fluxos de fraude que o gateway do lab gera, usando só Splunk
Enterprise: **sem Enterprise Security, sem Security Essentials e sem KVStore**.

Isso não é uma limitação de gosto. O Splunk do lab roda num Xeon E7-4870
(Westmere, sem AVX), e o KVStore do Splunk 10 é MongoDB 7, que exige AVX — o
`mongod` não sobe, então tokens, MCP Server e Log Observer Connect ficam fora.
Nada aqui depende de nenhum dos três.

## Instalação

```bash
# na máquina do Splunk
scp -r splunk-app/obs_lab_fraud <splunk>:/tmp/
sudo cp -r /tmp/obs_lab_fraud /opt/splunk/etc/apps/
sudo chown -R splunk:splunk /opt/splunk/etc/apps/obs_lab_fraud
sudo /opt/splunk/bin/splunk restart
```

Depois: **Apps → obs-lab Fraude → Fraude - obs-lab**.

## O que vem dentro

| Arquivo | Conteúdo |
|---|---|
| `macros.conf` | `obs_lab_security` e `obs_lab_fraud` — a base de todas as buscas |
| `props.conf` | extração JSON do sourcetype `obs-lab:container` |
| `savedsearches.conf` | 8 detecções + 1 de risco acumulado, todas agendadas |
| `data/ui/views/fraude_obs_lab.xml` | dashboard com drilldown por IP |

## As detecções

| Busca | Ameaça | Severidade |
|---|---|---|
| Brute force de login | `brute_force` | 4 |
| Account takeover (IP novo) | `account_takeover` | 3 |
| Adulteração de preço no checkout | `price_tampering` | 5 |
| Card testing | `card_testing` | 5 |
| Velocity abuse em pedidos | `velocity_abuse` | 3 |
| Bot scraping do catálogo | `bot_scraping` | 2 |
| Credential stuffing | `credential_stuffing` | 4 |
| Card cycling (motor de risco) | `card_cycling` | 5 |
| **Risco acumulado por IP** | correlação | 5 |

A última é a que vale na demo: soma o `risk_score` de todas as ameaças do mesmo
IP numa janela de 15 min e só dispara quando o IP aparece em **dois ou mais
fluxos distintos**. É o Risk-Based Alerting do ES emulado com `stats` — mostra
correlação em vez de alerta isolado.

Os disparos aparecem em **Activity → Triggered Alerts** (`alert.track = 1`), que
é o mais perto de um notable que Splunk Enterprise puro entrega.

## Motor de risco do checkout

Os detectores acima são regra única. O checkout também passa por um **motor de
risco** (`riskEngine` em `services/gateway/src/security.js`) que soma pontos por
sinal do dispositivo (`X-Device-Id`; sem ele, o IP) numa janela de 10 min:

| Sinal | Pontos |
|---|---|
| 3-4 cartões distintos / 5+ | +25 / +50 |
| 3+ contas distintas | +20 |
| mais de 5 tentativas em 2 min | +15 |
| aparelho novo na conta | +10 |
| valor acima de US$ 250 | +10 |
| já bloqueado antes | +20 |

**< 40 aprova, 40-69 responde 428 (pede 3DS), >= 70 responde 403.** Toda decisão
vira `event_type=risk_decision` com `risk_score`, `decision` e `rules{}`
(macro `obs_lab_risk`); com 3+ cartões e decisão diferente de aprovar, sai como
`threat=card_cycling`. O ataque do load-gen (`fraudCardCycling`) usa 8 cartões
em 4 contas: aprova 2, desafia 2 e bloqueia a partir do 5º cartão.

## CIM

`props.conf`, `eventtypes.conf` e `tags.conf` mapeiam os logs para os data
models **Authentication** (`tag=authentication`, `action=success|failure`,
`user`, `src`) e **Web** (`tag=web`, `status`, `uri_path`, `dest`). É o que o
Security Essentials procura para as detecções prontas acharem campo.

```
tag=authentication index=lab | stats count by action, user
```

## Se os painéis vierem vazios

O `index` e o `sourcetype` estão fixos na macro `obs_lab_security`. Confira o que
o HEC está gravando e ajuste a macro (Settings → Advanced search → Macros):

```
index=lab | stats count by sourcetype, source
```

Se os campos não aparecerem quebrados (`threat`, `risk_score`, `client_ip`), o
`spath` dentro da macro resolve — mas vale checar se o `_raw` é mesmo a linha
JSON do gateway e não um envelope do runtime de container.

## Onde os eventos nascem

`services/gateway/src/security.js` (scraping, tampering, card testing, velocity)
e `services/gateway/src/server.js` (brute force, account takeover). Todo evento
sai como uma linha JSON com `logger: "security"`, `threat`, `risk_score` e
`blocked` — o `risk_score` já vem calculado pelo gateway; as buscas só agregam.

---

## Panorama - painel unico

`obs_lab_panorama` e a view default do app. Junta num painel so:

| Bloco | Fonte | Depende de |
|---|---|---|
| Negocio (checkout, taxa de sucesso, pedidos) | `index=lab` logger=http | so o HEC de logs |
| Experiencia externa | `index=te` (stream do ThousandEyes) | integracao do TE |
| Aplicacao (p95, erros, trace_id) | `index=lab` logger=http / severity=ERROR | so o HEC de logs |
| Seguranca e fraude | `index=lab` logger=security | so o HEC de logs |
| Infraestrutura (CPU/memoria por container) | `index=lab_metrics` | **exige o passo abaixo** |

### Indice de metricas

Os quatro primeiros blocos funcionam sem configurar nada alem do que o lab ja
tem. O bloco de INFRAESTRUTURA depende de um indice de **metricas**:

```
# Settings > Indexes > New Index
#   Index Name : lab_metrics
#   Index Data Type : Metrics        <- NAO deixe em Events
```

Depois libere o indice no token do HEC (`obs-lab`) em
Settings > Data inputs > HTTP Event Collector > obs-lab > Selected indexes.

E no `.env` do servidor:

```
SPLUNK_METRICS_INDEX=lab_metrics
```

`make rebuild` recria o collector com o exporter `splunk_hec/metrics`.

Conferir que chegou:

```
| mstats avg(_value) WHERE index=lab_metrics metric_name="container.cpu.utilization" BY container.name span=1m
```

**Armadilha:** se o indice for de eventos, o HEC responde `200` e o dado
simplesmente some - nao aparece em `search` nem em `mstats`. Nao ha erro no log
do collector.

### Synthetics

As metricas de **Splunk Synthetics** (`synthetics.*`) nascem no Observability
Cloud, nao no collector. O bloco EXPERIENCIA SINTETICA le direto da org com o
comando `| sim flow` do **Splunk Infrastructure Monitoring Add-on** - consulta
ao vivo, nada indexado. Os macros `obs_lab_synth_uptime`,
`obs_lab_synth_duration` e `obs_lab_synth_failed` (em `obs_lab_fraud`) filtram
os testes `OBS-LAB-*`.
