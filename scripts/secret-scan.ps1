$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Patterns = @(
  'sk-orca-[A-Za-z0-9_-]{16,}',
  '(?i)(api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*["''][A-Za-z0-9_./+-]{16,}["'']'
)
$Hits = @()
foreach ($Pattern in $Patterns) {
  $Result = & rg -l --hidden --glob '!node_modules/**' --glob '!.git/**' --glob '!.eve/**' --glob '!.output/**' --glob '!pnpm-lock.yaml' --glob '!.env.example' -- $Pattern $ProjectRoot
  if ($LASTEXITCODE -eq 0) { $Hits += $Result }
  elseif ($LASTEXITCODE -ne 1) { throw "Secret scan could not run." }
}
$Hits = $Hits | Sort-Object -Unique
if ($Hits.Count -gt 0) {
  Write-Error ("Potential secret detected. Values are hidden. Files:" + [Environment]::NewLine + ($Hits -join [Environment]::NewLine))
  exit 1
}
Write-Host "Secret scan passed."
exit 0
