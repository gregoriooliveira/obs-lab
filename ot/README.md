# obs-lab OT — camada de Operational Technology

Ambiente **separado do lab de TI** (outra VM), focado em **utilities e indústria**:
energia, água, gás e motores. Tudo **simulado e isolado** — nenhum container
fala com equipamento ou rede real. A telemetria vai pro **mesmo Splunk** do lab
de TI, pra mostrar TI + OT numa visão só.

Um **stack (compose) por setor**. O que muda entre setores é o simulador de
processo + o mapa de tags; PLC/SCADA/Telegraf são reusados.

| Setor | Pasta | Estado |
|---|---|---|
| Energia (distribuição) | [`energia/`](energia/) | **fase 1 pronta** (sim + Modbus + Telegraf + HMI) |
| Água | `agua/` | a fazer (troca o simulador por EPANET/WNTR) |
| Gás | `gas/` | a fazer (pandapipes) |
| Indústria / motores | `industria/` | a fazer |

Uma VM de 8 vCPU / 16 GB roda 2–3 setores. Plano completo e decisões na memória
do projeto: `obs-lab-camada-ot-plano`.

## Índices no Splunk (criar no deploy)

- `ot_metrics` — **datatype=metric** — telemetria de processo (Telegraf → HEC).
- `ot` — eventos — alarmes e mudança de estado.

Token HEC dedicado com `ot_metrics` como índice default. Mesmo padrão do
`lab_metrics` do lab de TI.
