# Cobertura dos casos de uso (catálogo Splunk Utilities) pelo lab obs-lab

O catálogo do cliente tem 38 casos (UC-001..038), quase todos de **distribuição de
energia**. Este doc mapeia o que o lab **gera/demonstra** hoje. A planilha completa
(com status por caso) é `docs/Casos_de_Uso_Splunk_Utilities.xlsx` (fora do git).

Quatro níveis: **Atende** (dado já existe e montado) · **Criativo** (simulamos
barato a fonte que falta) · **Parcial** (ilustra, falta fonte) · **Não** (precisa
de sistema real do cliente; só posicionamento).

## Atende (núcleo — já pronto)

| Caso | O que demonstra | Onde |
|---|---|---|
| UC-028 | Balanço por alimentador = fronteira (Modbus) − Σ medidores (MQTT) = perda não-técnica | painel `obs_lab_ot_fusion` |
| UC-029 | Tamper × queda de consumo na mesma UC | alerta *Medidor adulterado* |
| UC-013 | Comando de abertura de disjuntor não autorizado (`unauth_command`) | alerta *Comando não autorizado* |
| UC-021 | Correlação evento de segurança × ocorrência operacional no mesmo alimentador | eventos índice `ot` |

## Criativo — construído nesta frente (energia)

A falha que o lab já injeta passou a gerar o sinal que faltava. Tudo no app
`obs_lab_fusion`; painel novo **`obs_lab_ot_experiencia`**.

| Caso | O que foi construído | Dispara |
|---|---|---|
| UC-003 | Reclamação sintética no portal quando a tensão afunda (`reclamacao motivo=tensao`, traz `conta_portal`) | auto (sag) |
| UC-006 | `last_gasp` por UC no trip → interrupção provável antes da ligação + reclamação | auto (trip) |
| UC-035 | UCs essenciais (`hospital`/`saneamento`/`eletrodependente` no lookup) priorizadas em falta/reclamação | auto |
| UC-012 | Evento `ot_config_change` com `mudanca_aprovada=0` (mudança de firmware/parâmetro sem ordem) | auto / `/fault?tipo=config_change` |
| UC-017 | Corte remoto em massa fora de janela | `/corte_massa` |
| UC-022 | Medidor sem comunicação (para de publicar; evento + queda da taxa de leitura) | `/comms?ok=false` |
| UC-025 | Energização/religamento em alimentador com equipe em campo (lookup `obs_lab_manutencao`) | auto (religa) |
| UC-027 | Religamentos sucessivos por alimentador (conta `ot_religamento`) | auto |
| UC-030 | Religação à revelia: UC `situacao=cortada` com consumo > 0 (métrica) | `/cortar` |
| UC-024 | Cadeia de TI: rota do portal que parou (interface caiu a zero) | dado da web store (`obs_lab_http`) |
| UC-037 | Acesso massivo a contas no portal (LGPD / stuffing) | dado da web store (`obs_lab_security`) |

Cada um tem um alerta em `savedsearches.conf` e aparece no painel
`obs_lab_ot_experiencia` (os de TI, UC-024/037, usam o dado que o lab de TI já
produz — sem sim novo).

## Parcial (ilustra, falta uma fonte)

UC-004 (prioridade por classe), UC-007 (first breath / nested outage), UC-008
(sobrecarga de trafo — sem fases/reverso), UC-009 (perfil de carga — sem CNAE),
UC-023 (telecom — só o enlace via ThousandEyes), UC-026 (saúde de ativo — a
indústria tem temperatura/vibração), UC-031 (Grupo A — sem memória de massa).

## Não (só posicionamento — precisa de sistema real)

Cibersegurança OT que depende de IDS/firewall/PAM/AD (UC-010, 011, 014, 015, 016,
018, 019, 020), continuidade com OMS/clima (UC-005), e o bloco de dados/regulatório
com CIS/GIS/OMS (UC-001, 002, 032, 033, 034, 036, 038).

## Pendências para rodar ao vivo

Mesmas de sempre (dependem da VM): criar `ot`/`ot_metrics` + token HEC, confirmar
o `metric_name` (macro `obs_lab_ot_metric`) e subir o compose. Os novos eventos
usam campos conhecidos; só os detectores **métricos** (UC-030 e o balanço) esperam
o `metric_name` real.
