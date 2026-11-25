param(
    [ValidateRange(1, 50)]
    [int]$Iterations = 50,
    [string]$TestFilter = ""
)

$ErrorActionPreference = "Continue"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$testDirectory = Join-Path $repositoryRoot "experimentos_reales"
$evidenceDirectory = Join-Path $PSScriptRoot "evidencia"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$evidenceFile = Join-Path $evidenceDirectory "experimentos-reales-$timestamp.log"
$composeProject = "task2-e2e-$timestamp"
$stackStarted = $false

New-Item -ItemType Directory -Force -Path $evidenceDirectory | Out-Null
Push-Location $repositoryRoot
try {
    $envFile = Join-Path $repositoryRoot ".env"
    if (-not (Test-Path $envFile)) {
        throw "Falta el archivo .env requerido por Docker Compose."
    }

    foreach ($name in @("ADMIN_API_KEY", "CLIENT_API_KEY")) {
        $line = Get-Content $envFile | Where-Object {
            $_ -match "^\s*$name\s*="
        } | Select-Object -First 1
        if (-not $line) {
            throw "Falta $name en .env; no se iniciará la suite integral."
        }
        $value = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
        if ($name -eq "ADMIN_API_KEY") {
            $env:EXPERIMENTS_ADMIN_API_KEY = $value
        }
    }

    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()

    $env:KRAKEND_PORT = [string]$port
    $env:PAYMENTS_PROVIDER = "mock"
    $env:PAYMENTS_PROVIDER_MODE = "NORMAL"
    $env:PAYMENTS_API_URL = "http://payments-api:8003"
    $env:PAYMENTS_TIMEOUT_SECONDS = "3"
    $env:PAYMENTS_EXTERNAL_TIMEOUT_SECONDS = "1"
    $env:PAYMENTS_PROVIDER_TIMEOUT_SECONDS = "2"
    $env:PAYMENTS_CIRCUIT_FAILURE_THRESHOLD = "3"
    $env:PAYMENTS_CIRCUIT_OPEN_SECONDS = "15"
    $env:PAYMENTS_BULKHEAD_CAPACITY = "5"
    $env:PAYMENTS_TRANSBANK_RETURN_URL = "http://127.0.0.1:$port/payments/transbank/return"
    $baseUrl = "http://127.0.0.1:$port"

    python -m pip install -r (Join-Path $testDirectory "requirements.txt") 2>&1 |
        Tee-Object -FilePath $evidenceFile
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudieron instalar las dependencias de experimentos."
    }

    docker compose -p $composeProject config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "La configuración de Docker Compose no es válida."
    }
    docker buildx build --network=host --load `
        -t "$composeProject-equipos-api" `
        -f (Join-Path "Equipos" "Dockerfile") `
        --target base "Equipos" 2>&1 |
        Tee-Object -FilePath $evidenceFile -Append
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo construir Equipos con acceso de red para su health probe."
    }
    $stackStarted = $true
    docker compose -p $composeProject up -d --build 2>&1 |
        Tee-Object -FilePath $evidenceFile -Append
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo construir o iniciar el stack real. Revisa la evidencia."
    }
    Write-Output "Esperando el gateway en $baseUrl/health"
    $ready = $false
    $lastHealthError = "No se recibió respuesta del gateway."
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $health = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "$baseUrl/health" `
                -TimeoutSec 3
            if ($health.StatusCode -ge 200 -and $health.StatusCode -lt 300) {
                $ready = $true
                break
            }
            $lastHealthError = "HTTP $($health.StatusCode)"
        }
        catch {
            $lastHealthError = $_.Exception.Message
            Start-Sleep -Seconds 3
        }
    }
    if (-not $ready) {
        throw "El gateway no quedó listo en 180 segundos en $baseUrl. Último error: $lastHealthError"
    }

    $pytestArgs = @(
        "-m", "pytest", "-s", "-v", $testDirectory,
        "--iterations", $Iterations, "--base-url", $baseUrl
    )
    if ($TestFilter) {
        $pytestArgs += @("-k", $TestFilter)
    }
    python @pytestArgs 2>&1 | Tee-Object -FilePath $evidenceFile -Append
    $testExitCode = $LASTEXITCODE
    if ($testExitCode -ne 0) {
        throw "Fallaron los experimentos integrales. Revisa la evidencia: $evidenceFile"
    }
    Write-Output "Evidencia guardada en: $evidenceFile"
}
finally {
    if ($stackStarted) {
        docker compose -p $composeProject down --volumes --remove-orphans 2>&1 |
            Tee-Object -FilePath $evidenceFile -Append
    }
    Remove-Item Env:\EXPERIMENTS_ADMIN_API_KEY -ErrorAction SilentlyContinue
    Pop-Location
}
