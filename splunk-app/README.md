# obs_lab_fraud — app de detecção de fraude para Splunk Enterprise

Detecta os 6 fluxos de fraude que o gateway do lab gera, usando só Splunk
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
| `savedsearches.conf` | 6 detecções + 1 de risco acumulado, todas agendadas |
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
| **Risco acumulado por IP** | correlação | 5 |

A última é a que vale na demo: soma o `risk_score` de todas as ameaças do mesmo
IP numa janela de 15 min e só dispara quando o IP aparece em **dois ou mais
fluxos distintos**. É o Risk-Based Alerting do ES emulado com `stats` — mostra
correlação em vez de alerta isolado.

Os disparos aparecem em **Activity → Triggered Alerts** (`alert.track = 1`), que
é o mais perto de um notable que Splunk Enterprise puro entrega.

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
