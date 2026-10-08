# obs_lab_fusion — Fusion Center

App proprio, em **Dashboard Studio** (`fusion_center`). Junta cyber, fraude,
risco e compliance numa visao so. A unidade nao e o alerta, e a **entidade**
(IP / conta): o mesmo ataque aparece como tecnica MITRE, valor em risco e
controle PCI/LGPD sob pressao.

Aparece em **Apps → obs-lab Fusion Center**.

## Dependencias

| De onde | O que | Por que |
|---|---|---|
| `obs_lab_fraud` | macros `obs_lab_fraud`, `obs_lab_app`, `obs_lab_http`, `obs_lab_te`, `obs_lab_synth_*` | fontes; nao sao redefinidos aqui (mesma regra do Panorama) |
| Infrastructure Monitoring Add-on | comando `\| sim` | KPI "Canal - Synthetics" |

## O que vem dentro

| Arquivo | Conteudo |
|---|---|
| `lookups/obs_lab_geo.csv` | IP/CIDR → pais, cidade, lat/lon, `ator` (cliente/atacante) |
| `lookups/obs_lab_threat.csv` | ameaca → dominio, MITRE ATT&CK, controle PCI DSS 4.0, LGPD |
| `macros.conf` | `obs_lab_geo_enrich`, `obs_lab_threat_enrich` (globais) |
| `data/ui/views/fusion_center.xml` | o painel |

### Por que um lookup de geo

O load-gen usa faixas de documentacao (RFC 5737: `203.0.113.x`,
`198.51.100.x`, `192.0.2.x`; RFC 2544: `198.18.x`). O `iplocation` devolve
vazio para elas - sem o CSV o mapa fica em branco. IP publico real (trafego do
Synthetics/TE que entra pelo tunel) cai no `iplocation` como fallback, com
`ator=externo`.

**Mudou IP em `load-gen/index.js` (`ACTORS`)? Atualize `obs_lab_geo.csv`.**
Conferencia:

```bash
for ip in $(grep -oE "'(203\.0\.113|198\.51\.100|192\.0\.2)\.[0-9]+'" load-gen/index.js | tr -d "'" | sort -u); do
  grep -q "^$ip/32," splunk-app/obs_lab_fusion/lookups/obs_lab_geo.csv || echo "FALTA $ip"; done
```

## O painel

| Bloco | Pergunta que responde |
|---|---|
| KPIs | quanto risco, quantos atacantes, quanto dinheiro, o canal esta de pe? |
| Mapa (bolhas) | de onde vem - tamanho = risco somado |
| Roubo de credencial | quantos IPs testaram credencial → tomaram conta → compraram |
| MITRE ATT&CK | quais taticas/tecnicas estao ativas |
| Sankey | origem → tecnica → rota → bloqueado/passou (o "flow map" do ataque) |
| Casos por entidade | uma linha por IP com cyber + fraude + $ + PCI + compras aprovadas |
| Grafo | pais → IP → tecnica → conta: a cadeia de um ator |
| Ataque x negocio | ataques, 5xx e checkouts ok no mesmo eixo |
| Controles sob pressao | PCI/LGPD com eventos que passaram sem bloqueio |

O mapa e de bolhas, nativo do Studio. Arcos origem→destino exigiriam o app
Missile Map (Simple XML, sem manutencao) - ficou de fora de proposito.

## Cadeia de credential stuffing

`fraudStuffing` no load-gen testa 8 pares "vazados" do mesmo IP; so `alice`
e valida, na 5a posicao. O gateway avisa a partir de 4 usuarios distintos e
bloqueia no 7o, entao o acerto passa:

```
credential_stuffing (T1110.004) -> account_takeover (T1078) -> checkout 201
```

Tudo com o mesmo `client_ip` - por isso o access log ganhou `client_ip`.

**Efeito conhecido:** a volta da dona da conta (login de casa, `192.0.2.11`)
depois do ataque tambem sai como `account_takeover`, porque o gateway so
compara com o ultimo IP. O mesmo ja acontecia com o `bob` no `fraudATO`. O
lookup marca esses IPs como `ator=cliente`, e o mapa e o KPI de IPs hostis os
excluem.

## Instalacao

```bash
sudo cp -r splunk-app/obs_lab_fusion /opt/splunk/etc/apps/
sudo chown -R splunk:splunk /opt/splunk/etc/apps/obs_lab_fusion
curl -k -u admin https://localhost:8089/services/apps/local/obs_lab_fusion/_reload -X POST
```
