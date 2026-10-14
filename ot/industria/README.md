# OT industria — planta de motores/linhas (perfil WEG)

Simula **1 planta + 5 linhas/motores** (Extrusao, Prensa, Fundicao, Usinagem,
Montagem) e entrega a telemetria ao Splunk como métrica. Autocontido: o
`process-sim` gera o próprio dado (não existe PLC/motor real). A curva de
produção de um "dia" (três turnos) roda em ~20 min pra a tela subir e descer na
demo; o poll é 5 s (realista de SCADA).

```
process-sim ──Modbus TCP:502──┬── telegraf ──HEC──> Splunk (ot_metrics)
   (modelo da planta)         ├── fuxa (HMI :1884)
   HTTP :8097 (fault/...)     └── meter-sim ──MQTT──> mosquitto ──┘
                                 (contadores, fase 2) HTTP :8098 (fraude/...)
```

Duas camadas: o **process-sim** entrega a produção/estado por linha (SCADA/Modbus);
o **meter-sim** rateia a produção de cada linha entre os **contadores** (ativos) e
publica por MQTT. A diferença entre o que a linha produziu e o que os contadores
reportam é a **produção desviada** (fraude de OEE). Fraude e a ponte com a TI: ver
[`../FRAUDE.md`](../FRAUDE.md).

## Subir

```bash
cd ot/industria
cp .env.example .env     # preencher SPLUNK_HEC_URL e SPLUNK_HEC_TOKEN
docker compose up -d --build
```

- **HMI (FUXA):** http://<host>:1884
- **Controle do process-sim:** http://<host>:8097/snapshot · `/events` · `/health`
- **Controle dos contadores:** http://<host>:8098/snapshot

## Mapa de fraude industrial

Diferente da energia (furto de medição de consumo), aqui a fraude é de
**produção, sensor e cyber/insider**:

| Família | O que é | Sinal no lab |
|---|---|---|
| Fraude de produção / OEE | contador reporta menos peças do que a linha produziu (desvio de produção, OEE inflado/maquiado) | balanço linha − Σ contadores |
| Adulteração de sensor | contador/sensor adulterado reportando valor falso | flag de **tamper** (desvio alto) |
| Cyber / sabotagem | parada por **comando não autorizado**; **spoof** de telemetria que congela a leitura e **esconde** superaquecimento/vibração em curso | evento no índice `ot` (severidade seguranca) |
| Insider | operador cuja conta corporativa (portal) foi comprometida e que também adultera o contador | a UC/ativo liga TI↔OT pela `conta_portal` |

### Balanço de produção (o detector)

```
producao_desviada(linha) = producao_pph da linha (process-sim)
                           − Σ producao_pph reportada pelos contadores (MQTT)
```

Linha sem fraude: reportado ≈ produzido. Linha com desvio: reportado cai e a
diferença é a produção desviada. O Splunk reconstrói o balanço e aponta a linha
e, pelo ativo, a `conta_portal` (ponte com a TI).

### Spoof esconde falha

O `spoof` congela a leitura reportada da linha numa faixa benigna **enquanto o
dano real cresce** (temperatura/vibração sobem de verdade). Dois sintomas no
dado: a linha reportada fica estática e descola do total da planta (Σ linhas ≠
total medido no barramento), e um superaquecimento/defeito de rolamento avança
sem alarme — a assinatura de um ataque que mascara a planta.

## Injeção de falha (o "chaos" do OT)

Com `AUTO_FAULT=true` o sim injeta sozinho. Na mão:

```bash
curl -X POST "http://<host>:8097/fault?linha=IN-03&tipo=superaquecimento&dur=30"
curl -X POST "http://<host>:8097/fault?linha=IN-04&tipo=vibracao_alta&dur=30"
curl -X POST "http://<host>:8097/fault?linha=IN-01&tipo=unauth_command&dur=20"
curl -X POST "http://<host>:8097/fault?linha=IN-05&tipo=spoof&dur=120"
```

| Tipo | Categoria | Efeito |
|---|---|---|
| `superaquecimento` | operação | temperatura do motor passa de ~90 °C |
| `vibracao_alta` | operação | vibração passa de ~7 mm/s (defeito de rolamento, ISO 10816) |
| `parada` | operação | linha para (status 0, tudo zera), religa no fim |
| `unauth_command` | **cyber** | parada por comando não autorizado (fisicamente = parada, evento `critical`) |
| `spoof` | **cyber** | leitura da linha congela benigna enquanto o dano real cresce |

Fraude de produção na mão (contadores):

```bash
curl -X POST "http://<host>:8098/fraude?ativo=AT-0005&desvio=0.6&tamper=true"
curl -X DELETE "http://<host>:8098/fraude?ativo=AT-0005"
```

Cada falha emite um **evento** em `ot/industria/evento/<categoria>` (MQTT) →
Telegraf → índice `ot`. `CYBER_FAULT_PROB` controla quanto do auto-fault é ataque.

## Mapa de registradores (holding, FLOAT32 ABCD, 2 regs cada)

Planta, base 0:

| Addr | Tag |
|---|---|
| 0 | consumo_total_kw |
| 2 | producao_total_pph |
| 4 | motores_ativos |
| 6 | temperatura_ambiente_c |
| 8 | hora_do_dia |

Linha *f* (0..4), base **100 + f·20**: `+0` corrente_a · `+2` temperatura_c ·
`+4` vibracao_mms · `+6` rpm · `+8` producao_pph · `+10` status · `+12` consumo_kw.

O `telegraf/telegraf.conf` lê tudo isso com a tag `linha=IN-0x` e `setor=industria`.
**Armadilha TOML:** em cada `request`, `tags` e `fields` são inline; o
sub-cabeçalho `[inputs.modbus.request.tags]` seguido de `fields =` faz o `fields`
cair dentro de `tags` e o Telegraf não lê campo nenhum.

## Ativos (meter-sim) e fraude de produção

50 ativos em `lookups/obs_lab_ativo_industria.csv` (gerado por
`gen_ativo_industria.py`, determinístico). Cada ativo tem `conta_portal =
cust-NNN` — o mesmo customerId da web store de TI, a ponte (fraca) do Fusion.
Regerar: `python gen_ativo_industria.py`.

## Config do HMI (FUXA)

Primeira vez, pela UI (http://host:1884): Devices → add → Modbus TCP, host
`process-sim`, porta `502`, slave id `1`; tags Holding Register FLOAT32 nos
endereços acima; monte a tela (linha de produção, gauges de temperatura/vibração
por motor, contador de peças).

## Pendente

- View de Fusion industrial no Splunk (mapa das linhas, OEE, perda de produção,
  alarmes, correlação com TI) — quando o dado fluir em `ot_metrics`/`ot`.
- Testes ThousandEyes: HTTP no HMI e agent-to-server nos "CLPs" (porta Modbus).
