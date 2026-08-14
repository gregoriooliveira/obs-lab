# obs-lab - equivalente ao Makefile para Windows (sem make instalado)
#
#   .\obs-lab.ps1 up | splunk | splunk-hec | appd | appd-db | te | all
#   .\obs-lab.ps1 tunnel | status | ps | logs | rebuild | clean

param(
    [Parameter(Position = 0)]
    [ValidateSet('help','up','splunk','splunk-hec','appd','appd-db','te','all',
                 'tunnel','status','ps','logs','rebuild','clean')]
    [string]$Target = 'help'
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Docker Desktop nem sempre esta no PATH da sessao
$dockerBin = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'
if (Test-Path $dockerBin) { $env:Path = "$dockerBin;$env:Path" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'docker nao encontrado no PATH. Docker Desktop esta instalado e rodando?'
}

$BASE   = @('-f','docker-compose.yml')
$SPLUNK = $BASE   + @('-f','docker-compose.splunk.yml')
$HEC    = $SPLUNK + @('-f','docker-compose.splunk-hec.yml')
$APPD   = $BASE   + @('-f','docker-compose.appd.yml')
$APPDDB = $APPD   + @('-f','docker-compose.appd-db.yml')
$TE     = $BASE   + @('-f','docker-compose.thousandeyes.yml')
$TUNNEL = $BASE   + @('-f','docker-compose.tunnel.yml')
$ALL    = $BASE   + @('-f','docker-compose.splunk.yml','-f','docker-compose.appd.yml','-f','docker-compose.appd-db.yml')

function Get-DotEnv {
    if (-not (Test-Path '.env')) {
        Write-Error '.env nao encontrado - copie .env.example para .env e preencha os valores'
    }
    $map = @{}
    foreach ($line in Get-Content '.env') {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line -split '=', 2
        # docker compose remove comentario inline em valor sem aspas
        $v = ($v -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
        $map[$k.Trim()] = $v
    }
    return $map
}

function Assert-EnvVar([hashtable]$env, [string[]]$names) {
    foreach ($n in $names) {
        if (-not $env[$n]) { Write-Error "  x $n nao definido no .env" }
    }
}

function Invoke-Compose([string[]]$files, [string[]]$cmd) {
    & docker compose @files @cmd
    if ($LASTEXITCODE -ne 0) { Write-Error "docker compose falhou (exit $LASTEXITCODE)" }
}

function Test-Endpoint([string]$label, [string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
        Write-Host ("  {0,-26} OK ({1})" -f $label, $r.StatusCode) -ForegroundColor Green
    } catch {
        Write-Host ("  {0,-26} FAIL" -f $label) -ForegroundColor Red
    }
}

switch ($Target) {
    'help' {
        Write-Host ''
        Write-Host '  obs-lab - Observability Lab'
        Write-Host ''
        Write-Host '  Uso: .\obs-lab.ps1 <target>'
        Write-Host ''
        Write-Host '    up          Stack base (app + postgres + load-gen)'
        Write-Host '    splunk      + Splunk O11y (metricas + APM + DBM)'
        Write-Host '    splunk-hec  + logs para Splunk Core via HEC'
        Write-Host '    appd        + AppDynamics APM'
        Write-Host '    appd-db     + AppDynamics APM e Database Agent'
        Write-Host '    te          + ThousandEyes Enterprise Agent'
        Write-Host '    all         Splunk + AppD APM + AppD DB agent'
        Write-Host '    tunnel      Cloudflare tunnel'
        Write-Host '    status      Health check dos endpoints'
        Write-Host '    ps          Containers'
        Write-Host '    logs        Follow logs'
        Write-Host '    rebuild     Rebuild forcado'
        Write-Host '    clean       Derruba tudo e remove volumes'
        Write-Host ''
    }
    'up'         { Get-DotEnv | Out-Null; Invoke-Compose $BASE   @('up','-d','--build') }
    'splunk'     { Assert-EnvVar (Get-DotEnv) @('SPLUNK_ACCESS_TOKEN'); Invoke-Compose $SPLUNK @('up','-d','--build') }
    'splunk-hec' { Assert-EnvVar (Get-DotEnv) @('SPLUNK_ACCESS_TOKEN','SPLUNK_HEC_URL','SPLUNK_HEC_TOKEN'); Invoke-Compose $HEC @('up','-d','--build') }
    'appd'       { Assert-EnvVar (Get-DotEnv) @('APPDYNAMICS_AGENT_ACCOUNT_NAME'); Invoke-Compose $APPD   @('up','-d','--build') }
    'appd-db'    { Assert-EnvVar (Get-DotEnv) @('APPDYNAMICS_AGENT_ACCOUNT_NAME'); Invoke-Compose $APPDDB @('up','-d','--build') }
    'te'         { Assert-EnvVar (Get-DotEnv) @('TE_ACCOUNT_TOKEN'); Invoke-Compose $TE @('up','-d') }
    'tunnel'     { Assert-EnvVar (Get-DotEnv) @('CLOUDFLARE_TUNNEL_TOKEN'); Invoke-Compose $TUNNEL @('up','-d') }
    'all' {
        Assert-EnvVar (Get-DotEnv) @('SPLUNK_ACCESS_TOKEN','APPDYNAMICS_AGENT_ACCOUNT_NAME')
        Invoke-Compose $ALL @('up','-d','--build')
    }
    'ps'      { Invoke-Compose $ALL @('ps') }
    'logs'    { Invoke-Compose $ALL @('logs','-f','--tail=100') }
    'rebuild' { Invoke-Compose $ALL @('up','-d','--build','--force-recreate') }
    'clean'   { Invoke-Compose $ALL @('down','-v','--remove-orphans') }
    'status' {
        Write-Host '=== Health check ==='
        Test-Endpoint 'gateway /health'       'http://localhost:8080/health'
        Test-Endpoint 'orders  /health'       'http://localhost:8081/health'
        Test-Endpoint 'payment /health'       'http://localhost:8082/health'
        Test-Endpoint 'gateway /api/products' 'http://localhost:8080/api/products'
        Test-Endpoint 'otel-collector health' 'http://localhost:13133/'
        Test-Endpoint 'otel-collector metrics' 'http://localhost:8888/metrics'
        Write-Host ''
        Write-Host '=== Export para os vendors (contadores do collector) ==='
        try {
            $m = (Invoke-WebRequest -Uri 'http://localhost:8888/metrics' -UseBasicParsing -TimeoutSec 10).Content
            $m -split "`n" |
                Where-Object { $_ -match '^otelcol_exporter_(sent|send_failed)_(spans|metric_points|log_records)\{' } |
                ForEach-Object { Write-Host "  $_" }
        } catch { Write-Host '  (metricas internas indisponiveis)' -ForegroundColor Yellow }
    }
}
