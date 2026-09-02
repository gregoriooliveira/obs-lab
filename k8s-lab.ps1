# obs-lab em Kubernetes (kind) - driver para Windows
#
#   .\k8s-lab.ps1 up        cria cluster, carrega imagens, sobe app + vendors
#   .\k8s-lab.ps1 cluster   so cria o cluster kind
#   .\k8s-lab.ps1 images    rebuild + kind load das imagens do app
#   .\k8s-lab.ps1 app       (re)aplica os manifests do app
#   .\k8s-lab.ps1 splunk    (re)instala o Splunk OTel Collector
#   .\k8s-lab.ps1 appd      (re)instala o AppDynamics Cluster Agent
#   .\k8s-lab.ps1 te        (re)aplica o ThousandEyes Enterprise Agent
#   .\k8s-lab.ps1 status    health check + contadores de export
#   .\k8s-lab.ps1 logs      logs do collector agent
#   .\k8s-lab.ps1 down      destroi o cluster

param(
    [Parameter(Position = 0)]
    [ValidateSet('help','up','cluster','images','app','splunk','appd','te','status','logs','down')]
    [string]$Target = 'help'
)

# 'Continue' de proposito: docker/kind/helm/kubectl escrevem progresso em
# stderr, e com 'Stop' o PowerShell aborta neles mesmo com exit 0. As etapas que
# importam sao validadas por exit code em Invoke-Checked.
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

$CLUSTER = 'obs-lab'
$CTX     = "kind-$CLUSTER"
$NS      = 'obs-lab'

# Perfis (LAB_PROFILE no .env ou no ambiente) - espelha o k8s-lab.sh:
#   full  3 nos, AppD InfraViz e ThousandEyes ligados   ~10 GB
#   lite  2 nos, InfraViz e ThousandEyes desligados     ~5 GB
$script:KindConfig    = 'k8s/kind-config.yaml'
$script:AppdInfraViz  = 'true'
$script:DeployTe      = $true

# ── PATH: Docker Desktop e winget nao entram no PATH da sessao ───────────────
foreach ($p in @(
    (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Kubernetes.kind_Microsoft.Winget.Source_8wekyb3d8bbwe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Helm.Helm_Microsoft.Winget.Source_8wekyb3d8bbwe\windows-amd64')
)) { if (Test-Path $p) { $env:Path = "$p;$env:Path" } }

foreach ($c in 'docker','kubectl','kind','helm') {
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
        Write-Error "$c nao encontrado no PATH."
    }
}

function Get-DotEnv {
    if (-not (Test-Path '.env')) { Write-Error '.env nao encontrado' }
    $map = @{}
    foreach ($line in Get-Content '.env') {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line -split '=', 2
        $v = ($v -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
        $map[$k.Trim()] = $v
    }
    return $map
}

function Set-Profile {
    $e = Get-DotEnv
    $p = if ($env:LAB_PROFILE) { $env:LAB_PROFILE } elseif ($e['LAB_PROFILE']) { $e['LAB_PROFILE'] } else { 'full' }
    switch ($p) {
        'full' { $script:KindConfig = 'k8s/kind-config.yaml';      $script:AppdInfraViz = 'true';  $script:DeployTe = $true }
        'lite' { $script:KindConfig = 'k8s/kind-config-lite.yaml'; $script:AppdInfraViz = 'false'; $script:DeployTe = $false }
        default { Write-Error "LAB_PROFILE invalido: $p (use full ou lite)" }
    }
    if ($env:APPD_INFRAVIZ_OVERRIDE) { $script:AppdInfraViz = $env:APPD_INFRAVIZ_OVERRIDE }
    if ($env:DEPLOY_TE_OVERRIDE)     { $script:DeployTe     = ($env:DEPLOY_TE_OVERRIDE -eq 'true') }
    Write-Host "perfil: $p (InfraViz=$script:AppdInfraViz, ThousandEyes=$script:DeployTe)" -ForegroundColor Cyan
}

# docker/kind/helm/kubectl escrevem progresso em stderr. Com
# ErrorActionPreference=Stop o PowerShell trata isso como NativeCommandError e
# aborta mesmo com exit 0 - por isso baixamos a preferencia durante a chamada e
# decidimos pelo exit code, que e o unico sinal confiavel.
function Invoke-Checked([string]$exe, [string[]]$argv, [string]$what) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @argv } finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { Write-Error "$what falhou (exit $LASTEXITCODE)" }
}

function New-Cluster {
    # `kind get clusters` escreve "No kind clusters found." em stderr quando vazio
    $existing = @(& kind get clusters 2>$null)
    if ($existing -contains $CLUSTER) {
        Write-Host "cluster '$CLUSTER' ja existe" -ForegroundColor Yellow
    } else {
        Invoke-Checked 'kind' @('create','cluster','--config',$script:KindConfig,'--wait','120s') 'kind create cluster'
    }
    & kubectl config use-context $CTX | Out-Null
    & kubectl get nodes -o wide
}

function Publish-Images {
    Write-Host '--- build das imagens (docker compose) ---' -ForegroundColor Cyan
    Invoke-Checked 'docker' @('compose','-f','docker-compose.yml','build') 'docker compose build'
    $imgs = @(
        'obs-lab-gateway-service:latest',
        'obs-lab-orders-service:latest',
        'obs-lab-payment-service:latest',
        'obs-lab-load-gen:latest'
    )
    foreach ($i in $imgs) {
        Write-Host "--- kind load $i ---" -ForegroundColor Cyan
        Invoke-Checked 'kind' @('load','docker-image',$i,'--name',$CLUSTER) "kind load $i"
    }
}

function Set-Secrets {
    $e = Get-DotEnv
    & kubectl --context $CTX apply -f k8s/base/00-namespace.yaml | Out-Null

    # Postgres
    & kubectl --context $CTX -n $NS create secret generic obs-lab-db `
        --from-literal=DB_USER=$($e['DB_USER']) `
        --from-literal=DB_PASSWORD=$($e['DB_PASSWORD']) `
        --from-literal=DB_NAME=$($e['DB_NAME']) `
        --dry-run=client -o yaml | kubectl --context $CTX apply -f - | Out-Null

    # AppDynamics (envFrom nos 3 deployments)
    & kubectl --context $CTX -n $NS create secret generic appd-creds `
        --from-literal=APPDYNAMICS_CONTROLLER_HOST_NAME=$($e['APPDYNAMICS_CONTROLLER_HOST_NAME']) `
        --from-literal=APPDYNAMICS_CONTROLLER_PORT=$($e['APPDYNAMICS_CONTROLLER_PORT']) `
        --from-literal=APPDYNAMICS_CONTROLLER_SSL_ENABLED=$($e['APPDYNAMICS_CONTROLLER_SSL_ENABLED']) `
        --from-literal=APPDYNAMICS_AGENT_ACCOUNT_NAME=$($e['APPDYNAMICS_AGENT_ACCOUNT_NAME']) `
        --from-literal=APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY=$($e['APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY']) `
        --from-literal=APPDYNAMICS_AGENT_APPLICATION_NAME=$($e['APPDYNAMICS_AGENT_APPLICATION_NAME']) `
        --dry-run=client -o yaml | kubectl --context $CTX apply -f - | Out-Null

    # ThousandEyes
    if ($e['TE_ACCOUNT_TOKEN']) {
        & kubectl --context $CTX -n $NS create secret generic te-agent-token `
            --from-literal=TE_ACCOUNT_TOKEN=$($e['TE_ACCOUNT_TOKEN']) `
            --dry-run=client -o yaml | kubectl --context $CTX apply -f - | Out-Null
    }

    # init SQL do Postgres (mesma fonte usada pelo docker-compose)
    & kubectl --context $CTX -n $NS create configmap db-init `
        --from-file=db/init `
        --dry-run=client -o yaml | kubectl --context $CTX apply -f - | Out-Null

    Write-Host 'secrets/configmap aplicados' -ForegroundColor Green
}

function Deploy-App {
    Set-Secrets
    Invoke-Checked 'kubectl' @('--context',$CTX,'apply','-f','k8s/base/') 'kubectl apply base'
    & kubectl --context $CTX -n $NS rollout status deploy/inventory-db --timeout=180s
    & kubectl --context $CTX -n $NS rollout status deploy/gateway-service --timeout=180s
    & kubectl --context $CTX -n $NS rollout status deploy/orders-service --timeout=180s
    & kubectl --context $CTX -n $NS rollout status deploy/payment-service --timeout=180s
}

function Deploy-Splunk {
    $e = Get-DotEnv
    if (-not $e['SPLUNK_ACCESS_TOKEN']) { Write-Error 'SPLUNK_ACCESS_TOKEN nao definido no .env' }
    $realm = if ($e['SPLUNK_REALM']) { $e['SPLUNK_REALM'] } else { 'us0' }
    & helm repo add splunk-otel-collector-chart https://signalfx.github.io/splunk-otel-collector-chart 2>&1 | Out-Null
    Invoke-Checked 'helm' @('repo','update') 'helm repo update'

    # Logs de container (inclui os eventos JSON de fraude do gateway) so tem
    # destino se o HEC do Splunk Core estiver preenchido - mesma regra do modo A.
    $hec = @()
    if ($e['SPLUNK_HEC_URL'] -and $e['SPLUNK_HEC_TOKEN']) {
        $idx = if ($e['SPLUNK_HEC_INDEX']) { $e['SPLUNK_HEC_INDEX'] } else { 'main' }
        $hec = @(
            '--set',"splunkPlatform.endpoint=$($e['SPLUNK_HEC_URL'])",
            '--set',"splunkPlatform.token=$($e['SPLUNK_HEC_TOKEN'])",
            '--set',"splunkPlatform.index=$idx",
            '--set','splunkPlatform.logsEnabled=true',
            '--set','splunkPlatform.insecureSkipVerify=true'
        )
        Write-Host '--- logs de container -> Splunk Core (HEC) ---' -ForegroundColor Cyan
    } else {
        Write-Host '--- SPLUNK_HEC_* vazio: logs de container ficam so no kubectl logs ---' -ForegroundColor Yellow
    }

    Invoke-Checked 'helm' (@(
        'upgrade','--install','splunk-otel-collector',
        'splunk-otel-collector-chart/splunk-otel-collector',
        '--kube-context',$CTX,
        '--namespace','splunk-otel','--create-namespace',
        '-f','k8s/splunk/values.yaml',
        '--set',"splunkObservability.accessToken=$($e['SPLUNK_ACCESS_TOKEN'])",
        '--set',"splunkObservability.realm=$realm",
        # credenciais do Postgres pro receiver de DBM (nao versionadas)
        '--set',"clusterReceiver.config.receivers.postgresql.username=$($e['DB_USER'])",
        '--set',"clusterReceiver.config.receivers.postgresql.password=$($e['DB_PASSWORD'])",
        '--set',"clusterReceiver.config.receivers.postgresql.databases[0]=$($e['DB_NAME'])",
        # o endpoint de DBM depende do realm; fixo em us1 no values.yaml mandava
        # os eventos de query pro realm errado em qualquer conta fora de us1
        '--set-string',"clusterReceiver.config.exporters.otlp_http/dbmon.logs_endpoint=https://ingest.$realm.observability.splunkcloud.com/v3/event"
    ) + $hec + @('--wait','--timeout','10m')) 'helm install splunk-otel-collector'
    & kubectl --context $CTX -n splunk-otel get pods
}
function Deploy-Appd {
    $e = Get-DotEnv
    if (-not $e['APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY']) { Write-Error 'APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY nao definido no .env' }
    # o repo antigo (appdynamics.github.io) responde 404; o atual e o artifactory
    & helm repo add appdynamics-charts https://appdynamics.jfrog.io/artifactory/appdynamics-cloud-helmcharts 2>&1 | Out-Null
    Invoke-Checked 'helm' @('repo','update') 'helm repo update'
    $url = "https://$($e['APPDYNAMICS_CONTROLLER_HOST_NAME']):$($e['APPDYNAMICS_CONTROLLER_PORT'])"
    Invoke-Checked 'helm' @(
        'upgrade','--install','appd-cluster-agent',
        'appdynamics-charts/cluster-agent',
        '--kube-context',$CTX,
        '--namespace','appdynamics','--create-namespace',
        '-f','k8s/appd/values.yaml',
        # o appdynamics-operator reescreve campos do CR Clusteragent depois que o
        # helm cria (ex: .spec.metricsSyncInterval). No re-install, o server-side
        # apply do helm 4 bate de frente com o field manager do operator:
        #   Apply failed with 1 conflict: conflict with "appdynamics-operator"
        # --force-conflicts faz o helm reassumir esses campos.
        '--force-conflicts',
        '--set',"installInfraViz=$script:AppdInfraViz",
        '--set',"controllerInfo.url=$url",
        '--set',"controllerInfo.account=$($e['APPDYNAMICS_AGENT_ACCOUNT_NAME'])",
        '--set',"controllerInfo.accessKey=$($e['APPDYNAMICS_AGENT_ACCOUNT_ACCESS_KEY'])",
        # CRD do InfraViz exige globalAccount como string nao-vazia (ver values.yaml)
        '--set',"controllerInfo.globalAccount=$($e['APPDYNAMICS_AGENT_ACCOUNT_NAME'])",
        '--timeout','5m'
    ) 'helm install appd-cluster-agent'
    & kubectl --context $CTX -n appdynamics get pods
}

function Deploy-Te {
    if (-not $script:DeployTe) {
        Write-Host 'ThousandEyes desligado neste perfil (use DEPLOY_TE_OVERRIDE=true)' -ForegroundColor Yellow
        return
    }
    $e = Get-DotEnv
    if (-not $e['TE_ACCOUNT_TOKEN']) { Write-Error 'TE_ACCOUNT_TOKEN nao definido no .env' }
    Set-Secrets
    Invoke-Checked 'kubectl' @('--context',$CTX,'apply','-f','k8s/thousandeyes/agent.yaml') 'kubectl apply te'
}

function Show-Status {
    Write-Host '=== nodes ===' -ForegroundColor Cyan
    & kubectl --context $CTX get nodes
    Write-Host ''
    Write-Host '=== pods obs-lab ===' -ForegroundColor Cyan
    & kubectl --context $CTX -n $NS get pods -o wide
    Write-Host ''
    Write-Host '=== pods splunk-otel ===' -ForegroundColor Cyan
    & kubectl --context $CTX -n splunk-otel get pods -o wide
    Write-Host ''
    Write-Host '=== pods appdynamics ===' -ForegroundColor Cyan
    & kubectl --context $CTX -n appdynamics get pods -o wide 2>$null
    Write-Host ''
    Write-Host '=== endpoints (host) ===' -ForegroundColor Cyan
    foreach ($u in @(
        @('gateway /health',       'http://localhost:18080/health'),
        @('gateway /api/products', 'http://localhost:18080/api/products')
    )) {
        try {
            $r = Invoke-WebRequest -Uri $u[1] -UseBasicParsing -TimeoutSec 10
            Write-Host ("  {0,-24} OK ({1})" -f $u[0], $r.StatusCode) -ForegroundColor Green
        } catch {
            Write-Host ("  {0,-24} FAIL" -f $u[0]) -ForegroundColor Red
        }
    }
    Write-Host ''
    Write-Host '=== export para os vendors ===' -ForegroundColor Cyan
    # a imagem do collector nao tem shell nem wget, entao nao da pra `kubectl
    # exec ... curl`. Port-forward e o caminho que funciona.
    $targets = @(
        @{ nome = 'agent';            sel = 'component=otel-collector-agent';       porta = 8889 },
        @{ nome = 'cluster-receiver'; sel = 'component=otel-k8s-cluster-receiver';  porta = 8899 }
    )
    foreach ($t in $targets) {
        $pod = (& kubectl --context $CTX -n splunk-otel get pod -l $t.sel -o jsonpath='{.items[0].metadata.name}' 2>$null)
        if (-not $pod) { continue }
        Write-Host "  [$($t.nome)]"
        $local = 10000 + $t.porta
        $job = Start-Job { param($c,$p,$l,$r) & kubectl --context $c -n splunk-otel port-forward $p "${l}:${r}" } `
               -ArgumentList $CTX, $pod, $local, $t.porta
        Start-Sleep -Seconds 5
        try {
            ((Invoke-WebRequest "http://localhost:$local/metrics" -UseBasicParsing -TimeoutSec 10).Content -split "`n" |
                Select-String -Pattern '^otelcol_exporter_(sent|send_failed)_(spans|metric_points|log_records)\{') |
                ForEach-Object { Write-Host "    $_" }
        } catch {
            Write-Host '    (sem dados)' -ForegroundColor Yellow
        }
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

switch ($Target) {
    'help' {
        Write-Host ''
        Write-Host '  obs-lab em Kubernetes (kind)'
        Write-Host ''
        Write-Host '    up        cria cluster + carrega imagens + sobe tudo'
        Write-Host '    cluster   so cria o cluster kind'
        Write-Host '    images    rebuild + kind load'
        Write-Host '    app       (re)aplica manifests do app'
        Write-Host '    splunk    (re)instala o Splunk OTel Collector'
        Write-Host '    appd      (re)instala o AppD Cluster Agent'
        Write-Host '    te        (re)aplica o ThousandEyes Enterprise Agent'
        Write-Host '    status    health check + contadores'
        Write-Host '    logs      logs do collector agent'
        Write-Host '    down      destroi o cluster'
        Write-Host ''
        Write-Host '  Perfis:  LAB_PROFILE=full (padrao, ~10 GB) | lite (~5 GB)'
        Write-Host ''
        Write-Host '  gateway no host: http://localhost:18080'
        Write-Host ''
    }
    'cluster' { Set-Profile; New-Cluster }
    'images'  { Publish-Images }
    'app'     { Deploy-App }
    'splunk'  { Deploy-Splunk }
    'appd'    { Set-Profile; Deploy-Appd }
    'te'      { Set-Profile; Deploy-Te }
    'status'  { Show-Status }
    'logs'    { & kubectl --context $CTX -n splunk-otel logs -l component=otel-collector-agent -c otel-collector --tail=100 -f }
    'down'    { Invoke-Checked 'kind' @('delete','cluster','--name',$CLUSTER) 'kind delete cluster' }
    'up' {
        Set-Profile
        New-Cluster
        Publish-Images
        Deploy-App
        Deploy-Splunk
        Deploy-Appd
        Deploy-Te
        Show-Status
    }
}
