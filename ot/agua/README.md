# OT água — saneamento/distribuição

Simula **1 estação (ETA/reservatório) + 5 setores de abastecimento** e entrega a
telemetria ao Splunk como métrica. Autocontido: o `process-sim` gera o próprio
dado (não existe PLC/hidrômetro real). A curva de consumo de um "dia" roda em
~20 min pra a tela subir e descer na demo; o poll é 5 s (realista de SCADA).

```
process-sim ──Modbus TCP:502──┬── telegraf ──HEC──> Splunk (ot_metrics)
   (modelo hidráulico)        ├── fuxa (HMI :1882)
   HTTP :8093 (fault/...)     └── meter-sim ──MQTT──> mosquitto ──┘
                                 (hidrômetros, fase 2)  HTTP :8094 (fraude/...)
```

Duas camadas: o **process-sim** entrega o consumo por setor (SCADA/Modbus); o
**meter-sim** rateia esse consumo entre 56 **hidrômetros** (UCs) e publica por
MQTT. A diferença entre o que o setor entrega e o que os hidrômetros reportam é a
**perda** — física (honesta) + não-técnica (furto). Fraude e a ponte com a TI:
ver [`../FRAUDE.md`](../FRAUDE.md).

Portas de host desencontradas da energia (8093/8094/1882) pra os dois setores
rodarem na mesma VM sem colisão. Modbus 502 e MQTT ficam internos.

## Subir

```bash
cd ot/agua
cp .env.example .env     # preencher SPLUNK_HEC_URL e SPLUNK_HEC_TOKEN
docker compose up -d --build
```

- **HMI (FUXA):** http://<host>:1882 — device Modbus TCP → `process-sim:502`.
- **Controle do sim:** http://<host>:8093/snapshot (JSON ao vivo), `/health`, `/events`.

## Injeção de falha (o "chaos" do OT)

Com `AUTO_FAULT=true` o sim injeta sozinho. Na mão:

```bash
curl -X POST "http://<host>:8093/fault?feeder=AG-02&tipo=vazamento&dur=30"
curl -X POST "http://<host>:8093/fault?feeder=AG-03&tipo=rompimento&dur=20"
curl -X POST "http://<host>:8093/fault?feeder=estacao&tipo=dosagem&dur=20"
```

| Tipo | Categoria | Efeito |
|---|---|---|
| `vazamento` | operação | vazão sobe, pressão cai, perda do setor dispara |
| `rompimento` | operação | adutora rompe (status 0, pressão/vazão = 0), volta no fim |
| `bomba_off` | operação | bomba fora: pressão 0 no setor |
| `unauth_command` | **cyber** | fechamento de válvula por comando não autorizado (parada; evento `critical`) |
| `spoof` | **cyber** | telemetria do setor **congela** enquanto o consumo real anda → Σ setores descola da vazão de entrada da estação |
| `dosagem` | **cyber** | dosagem de cloro adulterada: cloro despenca (risco de contaminação; só `feeder=estacao`) |

Cada falha emite um **evento** em `ot/agua/evento/<categoria>` (MQTT) → Telegraf →
índice `ot`. `CYBER_FAULT_PROB` controla quanto do auto-fault é ataque. Eventos
recentes: `curl http://<host>:8093/events`.

## Mapa de registradores (holding, FLOAT32 ABCD, 2 regs cada)

Estação, base 0:

| Addr | Tag |
|---|---|
| 0 | nivel_reservatorio_pct |
| 2 | vazao_entrada_ls |
| 4 | pressao_saida_mca |
| 6 | turbidez_ntu |
| 8 | cloro_mgl |
| 10 | setores_ativos |
| 12 | hora_do_dia |

Setor *s* (0..4), base **100 + s·20**: `+0` pressao_mca · `+2` vazao_ls ·
`+4` consumo_m3h · `+6` perda_pct · `+8` status.

**Armadilha TOML** (igual à energia): em cada `request`, `tags` e `fields` são
inline; `[inputs.modbus.request.tags]` seguido de `fields =` faz o `fields` cair
dentro de `tags` e o Telegraf não lê campo nenhum.

## Hidrômetros (meter-sim) e furto de água

56 UCs em `lookups/obs_lab_uc_agua.csv` (gerado por `gen_uc_agua.py`,
determinístico, seed própria — quem furta água não é quem furta luz). Cada UC tem
`conta_portal = cust-NNN` — o **mesmo customerId da web store de TI**, a ponte do
Fusion. O `meter-sim` lê o `/snapshot` do process-sim, rateia o consumo e publica
cada hidrômetro em `ot/agua/medidor/<UC>`.

Furto na mão (ou veja as UCs já marcadas no CSV):

```bash
curl -X POST "http://<host>:8094/fraude?uc=UC-A0005&desvio=0.6&tamper=true"
curl -X DELETE "http://<host>:8094/fraude?uc=UC-A0005"
curl "http://<host>:8094/snapshot"   # leituras atuais dos hidrômetros
```

- `desvio` = fração do consumo subnotificada (0.6 = reporta 40% do real).
- `tamper=true` levanta a flag de adulteração (evento `ot/agua/evento/tamper`).
- Detecção no Splunk: `perda_não_técnica(AG) = consumo do setor − Σ hidrômetros`.
  O setor sem furto fica ~5% (perda física de rua); os demais sobem. Ver
  [`../FRAUDE.md`](../FRAUDE.md).

Regerar a tabela de UCs (mesma seed → mesma saída): `python gen_uc_agua.py`.

## Mapa de fraude de água

| Família | Sinal no lab |
|---|---|
| **Comercial / perda não-técnica** — "gato" d'água, ligação clandestina, by-pass do hidrômetro | hidrômetro subnotifica consumo (`desvio`); o balanço setor − hidrômetros acusa |
| **Hidrômetro adulterado** — imã, violação do lacre, inversão | flag de `tamper` (evento segurança), com desvio alto |
| **Cyber-OT / SCADA** — comando não autorizado (fecha válvula), spoof de telemetria, **adulteração da dosagem de cloro** (contaminação) | faults `unauth_command`/`spoof`/`dosagem` → evento no índice `ot` |

**A ponte com a TI:** a UC (`conta_portal` = `username` na web store) liga o furto
de água no campo à fraude no portal (ATO, cartão). A mesma pessoa que adultera o
hidrômetro e é fraudada no portal fecha o caso coordenado — detalhe em
[`../FRAUDE.md`](../FRAUDE.md).

## Config do HMI (FUXA)

Primeira vez, pela UI (http://host:1882): **Devices → add** → Modbus TCP, host
`process-sim`, porta `502`, `slave id = 1`; tags Holding Register FLOAT32 nos
endereços acima; monte a tela (reservatório + pressão/vazão por setor). O projeto
fica no volume `fuxa-appdata`.

## Pendente

- View de água no Splunk (ou integrar ao `obs_lab_ot_fusion`) — quando `ot_metrics`
  tiver dado.
- Testes ThousandEyes: HTTP no HMI e agent-to-server nos "RTUs" (porta Modbus).
