#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("Apps", "Portable", "Live")]
    [string]$Mode = "Portable"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not [IO.Path]::GetFullPath((Get-Location).Path).Equals($repositoryRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run scripts/verify.ps1 from the ForgeMCP repository root."
}
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Missing .venv. Run .\scripts\bootstrap.ps1 first." }

function Invoke-CheckedCommand([string]$Description, [scriptblock]$Command) {
    Write-Host $Description
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE." }
}

function Invoke-Apps {
    Invoke-CheckedCommand "frontend dependency installation" { & npm ci --prefix frontend }
    Invoke-CheckedCommand "frontend build and asset freshness check" { & npm run build --prefix frontend }
    Invoke-CheckedCommand "frontend static tests" { & npm test --prefix frontend }
    Invoke-CheckedCommand "MCP App packaging and protocol tests" { & $python -m pytest -q -ra tests/test_mcp_apps.py tests/test_verify_workflow.py }
}

switch ($Mode) {
    "Apps" { Invoke-Apps }
    "Portable" {
        Invoke-Apps
        Invoke-CheckedCommand "portable pytest" { & $python -m pytest -q -ra }
        Invoke-CheckedCommand "compileall" { & $python -m compileall -q src tests }
    }
    "Live" {
        Invoke-Apps
        Invoke-CheckedCommand "live acceptance pytest" { & $python -m pytest -q -ra --run-forgemcp-live-acceptance }
        Invoke-CheckedCommand "compileall" { & $python -m compileall -q src tests }
    }
}
