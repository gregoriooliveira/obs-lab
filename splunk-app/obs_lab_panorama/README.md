# obs_lab_panorama — o painel unificado

Um app proprio, separado do `obs_lab_fraud`. O painel mostra negocio,
ThousandEyes, aplicacao, seguranca, metricas e infraestrutura — fraude e
**um bloco dentro dele**, nao o dono dele.

Aparece em **Apps → obs-lab Panorama**, ja abrindo no painel.

## Dependencia

O painel usa macros que vivem no app `obs_lab_fraud` e estao com
`export = system` (globais):

| Macro | Para que serve |
|---|---|
| `obs_lab_app` | todos os logs da aplicacao |
| `obs_lab_http` | so o access log (`logger=http`) |
| `obs_lab_security` | so os eventos de seguranca |
| `obs_lab_fraud` | so os eventos que sao fraude |
| `obs_lab_te` | metricas do stream do ThousandEyes |

Este app **nao redefine** nenhum deles de proposito. Macro duplicado em dois
apps vira conflito de precedencia — e a busca passa a depender de qual app
voce esta olhando, o que e horrivel de diagnosticar.

Ou seja: **instale os dois apps**, ou mova os macros para um app de base.

## Instalacao

```bash
# no host do Splunk (splunkenterprise), SPLUNK_HOME=/opt/splunk
sudo cp -r splunk-app/obs_lab_panorama /opt/splunk/etc/apps/
sudo chown -R splunk:splunk /opt/splunk/etc/apps/obs_lab_panorama
curl -k -u admin https://localhost:8089/services/apps/local/obs_lab_panorama/_reload -X POST
```

## Indices que o painel consulta

| Indice | Tipo | Conteudo |
|---|---|---|
| `lab` | eventos | logs dos 3 servicos, via HEC |
| `te` | eventos | stream do ThousandEyes |
| `lab_metrics` | **metrico** | metricas OTLP, container, host e Postgres |

O `lab_metrics` precisa existir como `datatype=metric` e estar liberado no
token HEC `obs-lab`. Ver `dashboards/README-painel-unificado.md`.
