param(
  [switch]$Full
)

$ErrorActionPreference = "Stop"
$Runner = Join-Path $PSScriptRoot "run.ps1"

& (Join-Path $PSScriptRoot "secret-scan.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Runner test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Runner typecheck
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Full) {
  & $Runner info
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

  & $Runner build
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Verification completed successfully."
