# OT energia — distribuição (fase 1)

Simula **1 subestação + 5 alimentadores** e entrega a telemetria ao Splunk como
métrica. Autocontido: o `process-sim` gera o próprio dado (não existe PLC/medidor
real). A curva de carga de um "dia" roda em ~20 min pra a tela subir e descer na
demo; o poll é 5 s (realista de SCADA).

```
process-sim ──Modbus TCP:502──┬── telegraf ──HEC──> Splunk (ot_metrics)
   (modelo elétrico)          └── fuxa (HMI :1881)
   HTTP :8091 (fault/snapshot/health)
```

## Subir

```bash
cd ot/energia
cp .env.example .env     # preencher SPLUNK_HEC_URL e SPLUNK_HEC_TOKEN
docker compose up -d --build
```

- **HMI (FUXA):** http://<host>:1881 — ver config do device abaixo.
- **Controle do sim:** http://<host>:8091/snapshot (JSON ao vivo), `/health`.

## Injeção de falha (o "chaos" do OT)

Com `AUTO_FAULT=true` o sim injeta sozinho (trip, sobrecarga, afundamento). Na mão:

```bash
curl -X POST "http://<host>:8091/fault?feeder=AL-02&tipo=overload&dur=30"
curl -X POST "http://<host>:8091/fault?feeder=AL-03&tipo=trip&dur=20"
curl -X POST "http://<host>:8091/fault?feeder=subestacao&tipo=sag&dur=15"
```

| Tipo | Efeito |
|---|---|
| `overload` | carga > 100 %, corrente alta, tensão afunda |
| `trip` | alimentador desliga (status 0, V/I/P = 0), religa no fim |
| `sag` | afundamento de tensão na subestação (só `feeder=subestacao`) |

## Mapa de registradores (holding, FLOAT32 ABCD, 2 regs cada)

Subestação, base 0:

| Addr | Tag |
|---|---|
| 0 | tensao_entrada_kv |
| 2 | potencia_total_mw |
| 4 | potencia_total_mvar |
| 6 | frequencia_hz |
| 8 | alimentadores_ativos |
| 10 | hora_do_dia |

Alimentador *f* (0..4), base **100 + f·20**: `+0` tensao_pu · `+2` corrente_a ·
`+4` potencia_kw · `+6` potencia_kvar · `+8` carga_pct · `+10` status.

O `telegraf/telegraf.conf` já lê tudo isso e marca cada alimentador com a tag
`alimentador=AL-0x`. **Armadilha TOML:** em cada `request`, `tags` e `fields` são
inline; `[inputs.modbus.request.tags]` seguido de `fields =` faz o `fields` cair
dentro de `tags` e o Telegraf não lê campo nenhum.

## Config do HMI (FUXA)

Primeira vez, pela UI (http://host:1881):
1. **Devices → add** → tipo **Modbus TCP**, host `process-sim`, porta `502`,
   `slave id = 1`.
2. Tags: Holding Register, FLOAT32, nos endereços acima.
3. Montar a tela: diagrama unifilar da subestação + gauges de tensão/carga por
   alimentador. O projeto do FUXA fica no volume `fuxa-appdata`.

## Pendente

- **View `obs_lab_ot`** no Splunk (Dashboard Studio) — faço quando o dado estiver
  fluindo no `ot_metrics` (mapa da rede, gauges, alarmes, correlação com TI).
- **Testes ThousandEyes**: HTTP no HMI (externo via túnel + interno) e
  agent-to-server nos "RTUs" (porta Modbus) — monitorar o enlace até a planta.
- Fase 2: medidores inteligentes (Mosquitto + MQTT).
