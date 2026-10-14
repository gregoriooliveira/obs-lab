# Fraude no OT e a ponte com a TI (Fusion Center unificado)

O lab de TI já tem um Fusion Center de fraude na web store (ATO, credential
stuffing, card cycling, motor de risco). Este documento mapeia a **fraude do lado
OT** (utilities e indústria) e como ela **se liga à TI pela mesma entidade** — a
ideia de que a web store é o portal de atendimento de uma distribuidora e o cliente
é uma **Unidade Consumidora (UC)** que existe nos dois mundos.

Abaixo o detalhe começa pela **energia** (o setor-referência); os setores **água**,
**gás** e **indústria** seguem o mesmo molde — ver a tabela "Os quatro setores".

## Os quatro setores (mesmo molde)

Cada setor tem `process-sim` (físico + Modbus, com faults de operação e cyber) e
`meter-sim` (medidores por MQTT, com furto/adulteração). O balanço `entregue −
reportado` é a detecção central de furto em todos.

| Setor | Entidade | "Furto" (subnotifica) | Balanço (entregue − reportado) | Cyber |
|---|---|---|---|---|
| Energia | UC / medidor | energia (gato) | alimentador − Σ medidores (kW) | unauth_command, spoof |
| Água | UC / hidrômetro | água (gato), hidrômetro adulterado | setor − Σ hidrômetros (m³/h) | unauth (válvula), spoof, dosagem (cloro) |
| Gás | UC / medidor | gás (bypass) | zona − Σ medidores (m³/h) | unauth (válvula), spoof, odorização off |
| Indústria | ativo / linha | produção/OEE subnotificada | produção real − Σ contadores (pph) | unauth (parada/sabotagem), spoof (mascara dano) |

**Correlação cross-setor e com a TI:** todos mandam evento pro `ot` e métrica pro
`ot_metrics` com a mesma convenção. O caso coordenado (`obs_lab_ot_coordenada`)
casa o tamper de **qualquer** setor com a fraude de TI pela `conta_portal` =
`username` — um mesmo cliente pode ser fraudado em várias utilidades ao mesmo
tempo. Painel combinado: `obs_lab_ot_geral`. Por-setor: `obs_lab_ot_fusion`
(energia é o template do balanço).

## A entidade-ponte: a UC

| Mundo | Identifica a UC por | Sinais de fraude |
|---|---|---|
| TI (portal / web store) | `conta_portal` = `cust-NNN` (customerId) | ATO, stuffing, cartão roubado quitando fatura, risco |
| OT (campo / medição) | `medidor`, `alimentador` | furto, adulteração (tamper), consumo incoerente |

Verdade compartilhada: [`energia/lookups/obs_lab_uc.csv`](energia/lookups/obs_lab_uc.csv)
(gerado por `gen_uc.py`, determinístico). Mapeia `uc ↔ conta_portal ↔ medidor ↔
alimentador ↔ classe ↔ geo`. No Splunk, um evento de TI (`customerId=cust-001`)
e um de OT (`medidor da UC-0001`) encontram-se pela UC — **UC-0001 = cust-001**
já nasce como caso coordenado (furta no medidor E é conta do portal).

## Famílias de fraude OT (setor energia, molde vale pra qualquer utility)

**1. Comercial / perda não-técnica** — a fraude clássica de distribuidora
- Furto de energia ("gato"), ligação clandestina → medidor **subnotifica** consumo.
- Medidor adulterado (imã, bypass, firmware) → leitura falsa, flag de **tamper**.
- Sinal no lab: o `meter-sim` faz a UC reportar menos que o real (`desvio`).

**2. Cyber-OT**
- Comando não autorizado (abrir disjuntor remoto — Ucrânia 2015).
- Spoofing de telemetria Modbus (protocolo sem autenticação).
- Medidor inteligente comprometido mandando leitura mentirosa.
- Sinal no lab: faults `unauth_command`/`spoof` no `process-sim` → evento no índice
  `ot`. O spoof congela a leitura do alimentador enquanto a carga real anda, e a
  incoerência aparece no balanço `Σ alimentadores ≠ total da subestação`.

**3. Insider / operação indevida** — operador abre alimentador fora de janela.

## O detector que exige os dois mundos: perda não-técnica

O medidor não se denuncia. A fraude aparece no **balanço de energia**:

```
perda_total(AL)       = potencia_alimentador (Modbus/process-sim)
                        − Σ consumo_reportado (MQTT/medidores)
perda_tecnica         ≈ 4% da carga (fio, trafo; honesta, toda rede tem)
perda_nao_tecnica(AL) = perda_total − perda_tecnica   ← o furto
```

No lab, o alimentador sem furto (AL-04) fica em ~5% (técnica pura); os com furto
sobem a 7–10%. O Fusion OT aponta o alimentador, desce até a UC e, pela UC, até a
conta no portal. **Isso só fecha com OT + TI juntos** — é o caso de uso que
justifica o Fusion unificado.

## Correlação no Fusion (por UC)

Score de fraude por UC combina os dois lados:

| Sinal | Origem | Peso |
|---|---|---|
| Perda não-técnica no alimentador da UC | OT (balanço) | alto |
| Flag de tamper no medidor | OT (evento) | alto |
| ATO / stuffing na conta do portal | TI (gateway) | alto |
| Cartão roubado quitando fatura | TI (motor de risco) | médio |
| Comando não autorizado no alimentador | OT (cyber) | crítico |

UC com sinal **nos dois mundos** = fraude coordenada → topo da fila. É a leitura
que nenhum dos dois centros isolados enxerga.

## Feito

- Furto/tamper nos medidores (`meter-sim`) + balanço de perda não-técnica.
- Faults cyber (`unauth_command`, `spoof`) no `process-sim`, com evento no índice `ot`.
- Eventos (segurança/operação/tamper) roteados por MQTT → Telegraf → índice `ot`.
- Cenário coordenado no load-gen de TI (`fraudCoordinated`): fraude no portal nas
  contas `cust-001/014/019/037/054` — exatamente as UCs que furtam no OT.
- **Lado Splunk** no app `obs_lab_fusion`: lookup `obs_lab_uc` (a ponte), macros OT
  (`obs_lab_ot_eventos/_cyber/_metric`, `obs_lab_uc_fraude_ti`), 4 alertas
  (`savedsearches.conf`) e o painel `obs_lab_ot_fusion` (Dashboard Studio).

## Pendências (precisam dos índices `ot`/`ot_metrics` e do dado fluindo)

- Criar os índices `ot` (eventos) e `ot_metrics` (métrica) e o token HEC que
  permite os dois.
- Confirmar o `metric_name` da telemetria com `| mstats ... by metric_name` e, se
  preciso, ajustar só o macro `obs_lab_ot_metric` (é o único ponto não validável
  offline; eventos e a ponte com a TI já usam campos conhecidos).
- Validar ao vivo o balanço, os eventos e os 4 alertas no Splunk (com a VM OT no ar).
