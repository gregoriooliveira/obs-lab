# Fraude no OT e a ponte com a TI (Fusion Center unificado)

O lab de TI já tem um Fusion Center de fraude na web store (ATO, credential
stuffing, card cycling, motor de risco). Este documento mapeia a **fraude do lado
OT** (utilities) e como ela **se liga à TI pela mesma entidade** — a ideia de que
a web store é o portal de atendimento de uma distribuidora e o cliente é uma
**Unidade Consumidora (UC)** que existe nos dois mundos.

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
- Sinal no lab: faults `unauth_command`/`spoof` no `process-sim` → evento índice `ot`
  **(a fazer — ver Pendências).**

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

## Pendências (quando os índices `ot`/`ot_metrics` existirem e o dado fluir)

- View `obs_lab_ot_fusion` (Dashboard Studio): mapa por UC, perda por alimentador,
  fila de UCs coordenadas, drill TI↔OT.
- Roteamento do evento `ot/energia/evento/tamper` (MQTT) para o índice `ot`.
- Faults cyber (`unauth_command`, `spoof`) no `process-sim` + evento no índice `ot`.
- Cenário coordenado no load-gen de TI (ATO nas contas `cust-001/0NN` que furtam),
  pra o caso coordenado aparecer sempre, não só por acaso.
