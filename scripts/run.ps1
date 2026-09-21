param(
  [ValidateSet("dev", "start", "build", "info", "test", "typecheck", "pc", "eval", "eval:business", "eval:orca", "eval:orca-audio", "periodic:check", "secret:scan")]
  [string]$Command = "dev",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$CommandArgs = @()
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BundledBin = Join-Path (Split-Path -Parent $ProjectRoot) ".pnpm-bin\bin"
$BundledNode = Join-Path $BundledBin "node.exe"
$BundledPnpm = Join-Path $BundledBin "pnpm.cmd"

if (Test-Path -LiteralPath $BundledNode) {
  $env:PATH = "$BundledBin;$env:PATH"
}

$NodeMajor = [int]((& node --version).TrimStart("v").Split(".")[0])
if ($NodeMajor -lt 24) {
  throw "Node.js 24以上が必要です。現在: $(& node --version)"
}

Push-Location $ProjectRoot
try {
  if ($Command -eq "pc") {
    function Test-MiruPortAvailable {
      param([int]$Port)
      $listener = $null
      try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
      } catch {
        return $false
      } finally {
        if ($null -ne $listener) { $listener.Stop() }
      }
    }

    if ($env:MIRU_PC_PORT) {
      $requestedPort = [int]$env:MIRU_PC_PORT
      if (-not $(Test-MiruPortAvailable -Port $requestedPort)) {
        throw "MIRU_PC_PORT=$requestedPort is already in use. Choose another port or stop the existing Mission Room PC."
      }
    } elseif ($(Test-MiruPortAvailable -Port 4317)) {
      $env:MIRU_PC_PORT = "4317"
    } else {
      $fallback = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
      try {
        $fallback.Start()
        $env:MIRU_PC_PORT = ([System.Net.IPEndPoint]$fallback.LocalEndpoint).Port.ToString()
      } finally {
        $fallback.Stop()
      }
      Write-Host "Port 4317 is in use. Starting Mission Room PC on available port $env:MIRU_PC_PORT."
    }
  }

  if (Test-Path -LiteralPath $BundledPnpm) {
    if ($CommandArgs.Count -gt 0) {
      & $BundledPnpm run $Command -- @CommandArgs
    } else {
      & $BundledPnpm run $Command
    }
  } else {
    if ($CommandArgs.Count -gt 0) {
      & pnpm run $Command -- @CommandArgs
    } else {
      & pnpm run $Command
    }
  }
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
