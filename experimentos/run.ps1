$ErrorActionPreference = "Continue"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$evidenceDirectory = Join-Path $PSScriptRoot "evidencia"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$evidenceFile = Join-Path $evidenceDirectory "experimentos-$timestamp.log"

New-Item -ItemType Directory -Force -Path $evidenceDirectory | Out-Null
Push-Location $repositoryRoot
try {
    python -m pytest -s -v experimentos 2>&1 | Tee-Object -FilePath $evidenceFile
    $testExitCode = $LASTEXITCODE
    if ($testExitCode -ne 0) {
        throw "Fallaron los experimentos. Revisa la evidencia: $evidenceFile"
    }
    $formatter = Join-Path $PSScriptRoot "formatear_log.py"
    python $formatter $evidenceFile
    $formatterExitCode = $LASTEXITCODE
    if ($formatterExitCode -ne 0) {
        throw "No se pudo formatear la evidencia: $evidenceFile"
    }
    $readableEvidenceFile = [System.IO.Path]::ChangeExtension(
        $evidenceFile,
        ".legible.txt"
    )
    Write-Output "Evidencia guardada en: $evidenceFile"
    Write-Output "Reporte legible guardado en: $readableEvidenceFile"
}
finally {
    Pop-Location
}
