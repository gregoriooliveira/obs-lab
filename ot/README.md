# obs-lab OT — camada de Operational Technology

Ambiente **separado do lab de TI** (outra VM), focado em **utilities e indústria**:
energia, água, gás e motores. Tudo **simulado e isolado** — nenhum container
fala com equipamento ou rede real. A telemetria vai pro **mesmo Splunk** do lab
de TI, pra mostrar TI + OT numa visão só.

Um **stack (compose) por setor**. O que muda entre setores é o simulador de
processo + o mapa de tags; PLC/SCADA/Telegraf são reusados.

Cada setor é um `process-sim` (físico + Modbus) + `meter-sim` (medidores/MQTT com
fraude) + `mosquitto` + `telegraf` + `fuxa` (HMI). Todos mandam pro **mesmo**
`ot_metrics`/`ot`, com a tag `setor`.

| Setor | Pasta | Portas host (sim · meter · HMI) | Estado |
|---|---|---|---|
| Energia (distribuição) | [`energia/`](energia/) | 8091 · 8092 · 1881 | **pronto** (modelo testado) |
| Água (saneamento) | [`agua/`](agua/) | 8093 · 8094 · 1882 | **pronto** (modelo testado) |
| Gás (distribuição) | [`gas/`](gas/) | 8095 · 8096 · 1883 | **pronto** (modelo testado) |
| Indústria / motores | [`industria/`](industria/) | 8097 · 8098 · 1884 | **pronto** (modelo testado) |

Modbus (502) e MQTT (1883) ficam internos a cada compose — sem conflito ao rodar
vários setores na mesma VM. `docker compose up -d --build` em cada pasta.

**Fraude e a ponte com a TI:** o OT não vê só processo — vê fraude (furto,
adulteração, produção desviada, cyber) e correlaciona com a fraude da web store de
TI pela **conta/Unidade Consumidora**. Mapa completo em [`FRAUDE.md`](FRAUDE.md).
O lado Splunk (lookups, macros, alertas e os painéis `obs_lab_ot_fusion` e
`obs_lab_ot_geral`) está no app `splunk-app/obs_lab_fusion`.

Uma VM de 8 vCPU / 16 GB roda 2–3 setores. Plano completo e decisões na memória
do projeto: `obs-lab-camada-ot-plano`.

## Índices no Splunk (criar no deploy)

- `ot_metrics` — **datatype=metric** — telemetria de processo (Telegraf → HEC).
- `ot` — eventos — alarmes e mudança de estado.

Token HEC dedicado com `ot_metrics` como índice default. Mesmo padrão do
`lab_metrics` do lab de TI.
