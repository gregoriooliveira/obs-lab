# Dashboard Splunk Observability — obs-lab

## Importar

1. Splunk Observability Cloud → **Dashboards**
2. Botão **Create (+)** → **Import** → **Dashboard**
   (se não estiver num Dashboard Group, primeiro crie/importe um Dashboard Group)
3. Selecione o arquivo `obs-lab-splunk-dashboard.json`
4. Confirme

## Charts incluídos

| Chart | Métrica origem | Serviço |
|-------|---------------|---------|
| Requests por segundo | http.requests.total | gateway |
| Taxa de erros 5xx | http.errors.total | gateway |
| Latência p50/p90/p99 | http.request.duration | gateway |
| Pedidos criados vs falhados | orders.created.total / orders.failed.total | orders |
| Latência do banco | orders.db.duration | orders |
| Pagamentos aprovados vs recusados | payment.processed.total / payment.failed.total | payment |
| Receita acumulada | payment.revenue.total | payment |
| Taxa de aprovação (%) | calculado | payment |
| CPU dos containers | cpu.utilization | docker_stats |
| Memória dos containers | memory.usage.total | docker_stats |

## Notas importantes

- **Filtro de ambiente**: o dashboard filtra por `deployment.environment = lab`
  (definido no resource processor do OTel Collector). Se o seu ENVIRONMENT
  no .env for outro valor, ajuste a variável no topo do dashboard.

- **Nomes de métricas de infra**: `cpu.utilization` e `memory.usage.total`
  são os nomes que o Splunk OTel Collector (modo agent) usa para docker_stats.
  Se os charts de CPU/Memória vierem vazios, abra o Metric Finder e confirme
  o nome exato — pode variar conforme a versão do collector
  (ex: `container.cpu.utilization`). Ajuste o programText do chart.

- **Counters via signalfx**: as métricas `.total` são counters cumulativos.
  Os charts usam rollup='rate' para mostrar taxa por segundo, que é o que
  faz sentido para tráfego. Para ver acumulado, troque rollup para 'sum'.

- **Dimensões (by=[...])**: os agrupamentos por route, reason, method dependem
  dos atributos que a app envia. Se algum agrupamento vier vazio, remova o
  by=[] correspondente no programText.
