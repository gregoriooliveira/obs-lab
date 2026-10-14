# OT gás — distribuição de gás canalizado

Simula **1 city-gate (UTR) + 5 zonas** e entrega a telemetria ao Splunk como
métrica. Autocontido: o `process-sim` gera o próprio dado (não existe PLC/medidor
real). A curva de consumo de um "dia" roda em ~20 min pra a tela subir e descer na
demo; o poll é 5 s (realista de SCADA). Mesmo molde do setor de energia.

```
process-sim ──Modbus TCP:502──┬── telegraf ──HEC──> Splunk (ot_metrics)
   (modelo da rede)           ├── fuxa (HMI :1881)
   HTTP :8095 (fault/...)     └── meter-sim ──MQTT──> mosquitto ──┘
                                 (medidores)           HTTP :8096 (fraude/...)
```

Duas camadas: o **process-sim** entrega a vazão por zona (SCADA/Modbus); o
**meter-sim** rateia essa vazão entre os medidores (UCs) e publica o consumo por
MQTT. A diferença entre o que a zona entrega e o que os medidores reportam é a
**perda** — técnica (honesta) + não-técnica (furto).

## Subir

```bash
cd ot/gas
cp .env.example .env     # preencher SPLUNK_HEC_URL e SPLUNK_HEC_TOKEN
docker compose up -d --build
```

- **HMI (FUXA):** http://<host>:1883 (porta 1881 do host já é do energia).
- **Controle do sim:** http://<host>:8095/snapshot · `/health` · `/events`.
- **Controle dos medidores:** http://<host>:8096/snapshot.

## Fraude no gás (famílias) e a ponte com a TI

| Família | O que é | Sinal no lab |
|---|---|---|
| **Furto / bypass** | desvio clandestino antes do medidor, hidrômetro de gás burlado | medidor subnotifica consumo (`desvio`) → aparece no **balanço** |
| **Medidor adulterado** | imã, inversão, firmware trocado | flag de **tamper** (evento) + subnotificação |
| **Cyber de SCADA** | comando não autorizado fecha válvula; spoof de telemetria | `unauth_command` / `spoof` → evento `seguranca` |
| **Odorização desligada** | gás sem odor = risco de segurança (não se detecta vazamento pelo cheiro) | `odorizacao_off` → evento `high` |

**A ponte:** cada UC tem `conta_portal = cust-NNN`, o mesmo `customerId` da web
store de TI (campo `username` no log de fraude). A mesma UC que furta gás no campo
pode sofrer ATO/cartão no portal — o Fusion correlaciona pela UC. Mapa geral e
correlação: [`../FRAUDE.md`](../FRAUDE.md).

**Detecção de furto (balanço):**

```
perda_não_técnica(zona) = vazão da zona (Modbus) − Σ consumo dos medidores (MQTT)
```

Zona sem furto fica ~3–4% (perda técnica); com furto sobe a 7–9%.

## Injeção de falha (o "chaos" do OT)

Com `AUTO_FAULT=true` o sim injeta sozinho. Na mão:

```bash
curl -X POST "http://<host>:8095/fault?zona=GS-02&tipo=vazamento&dur=30"
curl -X POST "http://<host>:8095/fault?zona=GS-01&tipo=unauth_command&dur=20"
curl -X POST "http://<host>:8095/fault?zona=estacao&tipo=odorizacao_off&dur=20"
# furto num medidor (meter-sim):
curl -X POST "http://<host>:8096/fraude?uc=UC-G0005&desvio=0.6&tamper=true"
```

| Tipo | Categoria | Efeito |
|---|---|---|
| `vazamento` | operação | vazão sobe, pressão afunda |
| `sobrepressao` | operação | pressão acima do limite |
| `valvula_fechada` | operação | zona bloqueada (status 0, vazão/pressão 0), reabre no fim |
| `unauth_command` | **cyber** | fecha a válvula por comando não autorizado (evento `critical`) |
| `spoof` | **cyber** | leitura da zona **congela** enquanto a vazão real anda → Σ zonas descola da entrada do city-gate |
| `odorizacao_off` | **cyber** | odorização despenca (só `zona=estacao`) — gás sem cheiro |

Cada falha emite um evento em `ot/gas/evento/<categoria>` (MQTT) → Telegraf →
índice `ot`.

## Mapa de registradores (holding, FLOAT32 ABCD, 2 regs cada)

City-gate / estação, base 0:

| Addr | Tag |
|---|---|
| 0 | pressao_entrada_bar |
| 2 | vazao_entrada_m3h |
| 4 | pressao_saida_mbar |
| 6 | temperatura_c |
| 8 | odorizacao_mgm3 |
| 10 | zonas_ativas |
| 12 | hora_do_dia |

Zona *z* (0..4), base **100 + z·20**: `+0` pressao_mbar · `+2` vazao_m3h ·
`+4` consumo_m3h · `+6` status.

**Armadilha TOML (telegraf):** em cada `request`, `tags` e `fields` são inline;
`[inputs.modbus.request.tags]` seguido de `fields =` faz o `fields` cair dentro
de `tags` e o Telegraf não lê campo nenhum.

## Medidores (meter-sim) e a tabela de UC

55 UCs em `lookups/obs_lab_uc_gas.csv` (gerado por `gen_uc_gas.py`,
determinístico). Cada UC tem `conta_portal = cust-NNN` — a ponte com a TI.
Regerar: `python gen_uc_gas.py`.

## Pendente (quando os índices `ot`/`ot_metrics` existirem e o dado fluir)

- Integração da view/alertas de gás no app `obs_lab_fusion` (mesmo padrão do
  energia; o lookup `obs_lab_uc_gas` entra como mais uma ponte).
- Config do device no FUXA (Modbus TCP → process-sim:502) pela UI.
- Testes ThousandEyes no HMI e no enlace até o city-gate.
