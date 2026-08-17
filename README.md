# obs-lab — Cadeia de microserviços observável (multi-vendor)

Lab de observabilidade sobre uma cadeia real de microserviços Node.js + Postgres,
instrumentada simultaneamente para **Splunk Observability Cloud**, **AppDynamics**
e **ThousandEyes**. Roda em `docker-compose` ou em **Kubernetes (kind)**.

O tráfego é gerado continuamente e o lab injeta falhas de propósito — recusa de
pagamento, timeout de adquirente, slow query com lock no Postgres, janelas de
degradação a cada 3 minutos. Serve para validar dashboards, alertas e correlação
sem depender de produção.

---

## Subir num servidor limpo

```bash
git clone https://github.com/gregoriooliveira/obs-lab.git
cd obs-lab

sudo ./bootstrap.sh            # instala docker, kubectl, kind, helm
cp .env.example .env
${EDITOR:-vi} .env             # preencher credenciais dos vendors

# escolha um dos dois modos:
make all                       # A) docker-compose
./k8s-lab.sh up                # B) Kubernetes (kind)
```

Verificação:

```bash
make status         # modo A
./k8s-lab.sh status # modo B
```

`bootstrap.sh --check` só verifica pré-requisitos, sem instalar nada.

---

## Segredos

Nenhuma credencial mora no repositório. Todas vêm do `.env`, que está no
`.gitignore` e nunca deve ser commitado. `.env.example` só tem placeholders.

```bash
make hooks          # instala pre-commit que bloqueia .env / segredo literal
make secrets-scan   # varre arquivos versionados e todo o histórico do git
```

Regras:

- Segredo novo entra como variável no `.env` + `.env.example` com placeholder.
- Nos compose/manifests use `${VAR}` ou `secretKeyRef` — nunca o valor.
- `DB_PASSWORD` é obrigatório (sem default): o compose falha se não estiver no `.env`.
- Se um token vazar, **rotacione no vendor primeiro**, depois limpe o histórico.

---

## Os dois modos

| | **A. docker-compose** | **B. Kubernetes (kind)** |
|---|---|---|
| Subir | `make all` | `./k8s-lab.sh up` |
| RAM | ~3 GB | ~10 GB (full) / ~5 GB (lite) |
| Gateway | `localhost:8080` | `localhost:18080` |
| `deployment.environment` | `lab` | `lab-k8s` |
| Splunk APM | ✅ | ✅ |
| Splunk Infra (host + container) | ✅ | ✅ |
| **Splunk navigators de Kubernetes** | ❌ | ✅ |
| Splunk DBM (query performance) | ✅ | ✅ |
| AppD APM (3 tiers) | ✅ | ✅ |
| AppD Cluster Agent | ❌ | ✅ |
| ThousandEyes Enterprise Agent | ✅ | ✅ |

Os dois usam ambientes diferentes, então **podem rodar em paralelo** e aparecem
separados no Splunk — desde que a máquina aguente (~13 GB somados).

> **Por que o modo B existe:** o Splunk Observability não tem navigator de Docker.
> Em Infrastructure, a categoria *Containers* só oferece *Kubernetes*. As métricas
> de container do docker-compose chegam (`container.cpu.utilization`,
> `container.memory.usage.total`, com dimensões `container.id` / `container.name` /
> `container.image.name`) mas só dá pra vê-las em dashboard custom. Para usar os
> navigators nativos — Clusters, Nodes, Pods, Containers, Workloads, Namespaces —
> a carga precisa rodar em Kubernetes.

### Perfis do modo B

```bash
LAB_PROFILE=full ./k8s-lab.sh up   # 3 nós, AppD InfraViz + ThousandEyes  ~10 GB
LAB_PROFILE=lite ./k8s-lab.sh up   # 2 nós, sem InfraViz, sem TE           ~5 GB
```

Ou fixe no `.env`. Knobs individuais sobrescrevem o perfil:
`APPD_INFRAVIZ_OVERRIDE=true`, `DEPLOY_TE_OVERRIDE=false`.

O que mais pesa: o **InfraViz da AppDynamics** é um Machine Agent Java por nó
(~600 MB cada) e o **ThousandEyes Enterprise Agent** pede ~1 GB.

---

## Requisitos

| | Mínimo | Recomendado |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 8 GB (`lite`) | 16 GB (`full`) / 32 GB (os dois modos juntos) |
| Disco | 40 GB | 80 GB |
| SO | qualquer Linux com Docker Engine | Ubuntu 22.04/24.04 LTS |
| Rede | saída HTTPS/443 | — |

Saída para: `ingest.<realm>.signalfx.com`, `api.<realm>.signalfx.com`,
`ingest.<realm>.observability.splunkcloud.com`, `*.saas.appdynamics.com`,
`*.thousandeyes.com`, mais os registries (`docker.io`, `quay.io`,
`appdynamics.jfrog.io`, `ghcr.io`).

**arm64**: os apps e o Splunk Collector rodam, mas as imagens de agente da
AppDynamics e do ThousandEyes só existem em amd64. Em ARM, use `LAB_PROFILE=lite`
e fique só no Splunk.

---

## Alternativa barata na nuvem

Se o servidor da empresa demorar, a opção mais simples é **uma VM só**, com o
modo A ou B — nada de cluster gerenciado.

| Opção | Shape | Ordem de grandeza | Observação |
|---|---|---|---|
| **Hetzner Cloud** (CX/CPX) | 8 vCPU / 16 GB | ~€15–20/mês | Melhor custo-benefício. Cabe o modo B `full`. |
| **Hetzner Cloud** | 4 vCPU / 8 GB | ~€7–10/mês | Só modo A, ou modo B `lite`. |
| **Oracle Cloud Always Free** | 4 OCPU ARM / 24 GB | **grátis** | ARM: só Splunk. AppD/TE não rodam. |
| **DigitalOcean / Vultr / Linode** | 8 vCPU / 16 GB | ~US$ 80–100/mês | Mais caro pelo mesmo shape. |
| **AWS EC2 spot** (`t3.xlarge`) | 4 vCPU / 16 GB | ~US$ 30–40/mês | Barato se desligar fora do horário. |

Valores são ordem de grandeza — confirme o preço atual antes de contratar.

**O truque de custo:** lab não precisa ficar de pé 24/7. Numa VM cobrada por hora,
ligar 8h/dia útil corta ~75% da conta. `./k8s-lab.sh down` + desligar a VM ao fim
do dia; `./k8s-lab.sh up` reconstrói tudo em ~5 minutos.

Se quiser expor o lab pra internet (ex: ThousandEyes Cloud Agents testando de fora)
sem abrir porta no firewall, use o Cloudflare Tunnel — `make tunnel`, veja abaixo.

---

## Arquitetura

```
load-gen → gateway-service (8080) → orders-service (8081) → payment-service (8082)
                  │                        │                       │
                  │                  inventory-db (Postgres 5432)
                  └────────────────────────┴───────────────────────┘
                         OTel SDK → Splunk OTel Collector
                                        ├─ métricas + APM → Splunk Observability
                                        ├─ postgresql (DBM) → Database Query Performance
                                        ├─ k8s_cluster + k8s events → navigators de K8s
                                        ├─ logs (stdout) → Splunk Core (HEC)*
                                        └─ host/container → Infrastructure
                         AppD agent → Controller (3 tiers)
                         AppD Cluster Agent + InfraViz (modo B)
                         AppD DB Agent → Database Monitoring

  ThousandEyes Enterprise Agent → testa de dentro da rede
  cloudflared (tunnel) → publica URLs → ThousandEyes Cloud Agents (de fora)

  * pipeline de logs é opt-in, precisa do HEC configurado
```

### Fluxo de uma compra (trace distribuído)

```
POST /api/checkout (gateway)
  └─ checkout span
      └─ HTTP POST orders-service /orders
          └─ order.create span
              ├─ pg: BEGIN / SELECT FOR UPDATE / UPDATE / COMMIT   ← spans reais
              └─ HTTP POST payment-service /charge
                  └─ payment.charge span
                      └─ acquirer.authorize span
```

### Serviços

| Serviço | Porta | Papel |
|---|---|---|
| gateway-service | 8080 | Entrada, catálogo, roteamento |
| orders-service | 8081 | Cria pedido, reserva estoque no Postgres |
| payment-service | 8082 | Processa pagamento (adquirente simulado) |
| inventory-db | 5432 | Postgres 16 (`products` / `orders` / `order_items`) |

### Endpoints (gateway)

| Método | Path | Descrição |
|---|---|---|
| GET | `/api/products` | Lista catálogo |
| GET | `/api/products/:id` | Produto por id |
| POST | `/api/checkout` | Compra → orders → payment (trace distribuído) |
| GET | `/api/debug/error` | 500 forçado |
| GET | `/api/debug/slow?ms=N` | Lento |
| GET | `/health` | Health |

### Falhas injetadas

| Cenário | Onde | Frequência |
|---|---|---|
| Slow query com `pg_sleep` | orders → Postgres | 8% das transações |
| Estoque insuficiente | orders | ~3%, sobe com pedido > $250 |
| Recusa de pagamento | payment | 10%, sobe com boleto e valor alto |
| Pico de latência (p99) | payment | 5% das chamadas |
| Timeout do adquirente → 503 | payment | latência > 5s |
| Janela de degradação (45s) | payment | a cada 3 minutos |
| Cache miss | gateway | 15% |
| 500 forçado | gateway | 10% do tráfego do load-gen |

---

## Comandos

### Modo A — docker-compose

```bash
make up          # só a app (sem vendor)
make splunk      # + Splunk Observability
make splunk-hec  # + logs pro Splunk Core (precisa do HEC no .env)
make appd        # + AppDynamics APM
make appd-db     # + AppD APM e Database Agent
make te          # + ThousandEyes Enterprise Agent
make all         # Splunk + AppD APM + AppD DB Agent
make tunnel      # Cloudflare Tunnel
make status      # health check
make logs        # follow dos logs
make clean       # derruba tudo e remove volumes
```

No Windows, sem `make`: `.\obs-lab.ps1 <target>` (mesmos alvos).

### Modo B — Kubernetes

```bash
./k8s-lab.sh up        # cluster + imagens + app + vendors
./k8s-lab.sh status    # nodes, pods, endpoints, contadores de export
./k8s-lab.sh logs      # logs do collector agent
./k8s-lab.sh app       # re-aplica só os manifests do app
./k8s-lab.sh splunk    # re-instala só o collector
./k8s-lab.sh down      # destrói o cluster
```

No Windows: `.\k8s-lab.ps1 <target>`.

---

## Depois de subir — o que configurar em cada vendor

Nem tudo é automático. Estes passos são na UI:

**AppDynamics — Database Agent** (modo A, `make appd-db`)
Databases → Configure → Add → PostgreSQL
Hostname `inventory-db`, porta `5432`, database `inventory`, user/senha do `.env`.
O agent só executa; o alvo é definido no Controller.

**ThousandEyes — testes**
O agente se registra sozinho. Crie os testes apontando para:
- modo A: `http://gateway-service:8080/health` e `/api/products`
- modo B: `http://gateway-service.obs-lab.svc.cluster.local:8080/health`

**Cloudflare Tunnel** (opcional, pra testar de fora)
1. Zero Trust → Networks → Tunnels → Create tunnel → Cloudflared
2. Copie o token → `.env` (`CLOUDFLARE_TUNNEL_TOKEN`)
3. Public Hostname → serviço `HTTP → gateway-service:8080`
4. `make tunnel`

**Splunk Core — logs via HEC** (opcional)
Settings → Data Inputs → HTTP Event Collector, crie um token, preencha
`SPLUNK_HEC_URL` / `SPLUNK_HEC_TOKEN` no `.env` e suba com `make splunk-hec`.
Busca depois: `index=main source="obs-lab"`.

A pipeline de logs é **opt-in** de propósito: o exporter `splunk_hec` exige
endpoint não-vazio, então com o HEC em branco o collector **não sobe** — e
métricas e traces parariam junto.

---

## Onde olhar o dado

**Splunk Observability**
- APM → Services: `gateway-service`, `orders-service`, `payment-service`
- APM → filtro `deployment.environment` = `lab` ou `lab-k8s`
- Infrastructure → Kubernetes (só modo B)
- Infrastructure → Hosts overview
- Databases → `inventory` (Query Performance)
- Metric explorer → busque `container.` ou `k8s.`

**AppDynamics**
- Applications → `obs-lab` → 3 tiers com flow map
- Servers → Clusters (modo B)
- Databases → `inventory` (após configurar o collector)

---

## Estrutura

```
├── bootstrap.sh                  instala pré-requisitos no servidor
├── Makefile / obs-lab.ps1        driver do modo A (Linux / Windows)
├── k8s-lab.sh / k8s-lab.ps1      driver do modo B (Linux / Windows)
├── .env.example                  template de credenciais
│
├── services/{gateway,orders,payment}/    apps Node.js + instrumentação
├── load-gen/                     gerador de tráfego
├── db/init/                      schema + seed + pg_stat_statements
├── dashboards/                   dashboard de exemplo do Splunk
│
├── docker-compose.yml            base (app + postgres + load-gen)
├── docker-compose.splunk.yml     + Splunk OTel Collector
├── docker-compose.splunk-hec.yml + logs pro Splunk Core
├── docker-compose.appd.yml       + AppD APM e Machine Agent
├── docker-compose.appd-db.yml    + AppD Database Agent
├── docker-compose.thousandeyes.yml
├── docker-compose.tunnel.yml     + Cloudflare Tunnel
├── splunk-otel-collector/        config.yml e config-hec.yml
│
└── k8s/
    ├── kind-config.yaml          cluster 3 nós (perfil full)
    ├── kind-config-lite.yaml     cluster 2 nós (perfil lite)
    ├── base/                     manifests do app
    ├── splunk/values.yaml        Helm values do collector
    ├── appd/values.yaml          Helm values do Cluster Agent
    └── thousandeyes/agent.yaml   Enterprise Agent
```

`docker-compose.all.yml` está **deprecado** — duplicava os overlays e saía de
sincronia. Use os overlays.

---

## Armadilhas conhecidas

Cada uma custou uma sessão de debug. Estão comentadas no arquivo correspondente.

**Collector (modo A)**
- `splunk_hec` com endpoint vazio impede o collector inteiro de subir — por isso
  a pipeline de logs é opt-in.
- Porta `8888` não é mais exposta por padrão (collector ≥ v0.123); o `config.yml`
  declara um reader `pull/prometheus` explícito.
- Em Docker Desktop/WSL2 o scraper `filesystem` enxerga mounts virtuais que não
  existem dentro do container — excluídos por regexp no `config.yml`.
- zPages (`:55679/debug/tracez`) sobe, mas o span processor não é registrado
  nesta build: a página fica vazia. Use as métricas de `:8888`.

**Kubernetes (modo B)**
- `kubelet_stats` falha no kind com `x509: ... doesn't contain any IP SANs`.
  É o receiver que gera **toda** métrica de pod/container: sem
  `insecure_skip_verify: true`, os navigators de Pods e Containers ficam vazios.
  Em EKS/GKE/AKS não é necessário.
- O kind sobe controller-manager/scheduler/etcd escutando métricas só em
  `127.0.0.1`. Os `kubeadmConfigPatches` abrem o bind para o collector coletar
  o control plane.
- CRD do InfraViz rejeita `globalAccount` nulo — o driver preenche com o nome
  da conta só para satisfazer o schema.
- O `appdynamics-operator` reescreve campos do CR depois que o Helm cria, e o
  server-side apply do Helm 4 conflita no re-install. Daí o `--force-conflicts`.
- Repo Helm da AppD: o `appdynamics.github.io` responde **404**. O atual é
  `appdynamics.jfrog.io/artifactory/appdynamics-cloud-helmcharts`.

**Postgres / DBM**
- O receiver `postgresql` abre a conexão de `top_query` no database **default**
  (`postgres`), não no de negócio. `pg_stat_statements` precisa existir nos
  **dois** — é o que `db/init/00-dbm.sql` faz.
- Requer `shared_preload_libraries=pg_stat_statements` no servidor (já está no
  compose e no manifest do k8s).
- Ruído esperado: o receiver tenta dar `EXPLAIN` nas próprias queries que ele vê
  em `pg_stat_activity`; como vêm normalizadas com `$N`, o EXPLAIN falha. Os
  samples continuam sendo exportados — é log, não perda de dado.

**Recursos**
- Modo A + modo B juntos passam de 13 GB. Em máquina de 16 GB, rode um por vez
  ou use `LAB_PROFILE=lite`.
- No Docker Desktop (Windows), o WSL2 pega 50% da RAM física por padrão. Se
  aumentar via `%USERPROFILE%\.wslconfig`, **não passe de ~60% do total** — o
  Windows precisa do resto.
- `kind` com vários nós estoura os limites de inotify padrão e os pods entram em
  CrashLoop com "too many open files". O `bootstrap.sh` ajusta.
