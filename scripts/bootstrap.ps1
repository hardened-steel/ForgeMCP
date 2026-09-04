#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$currentDirectory = [IO.Path]::GetFullPath((Get-Location).Path)
if (-not $currentDirectory.Equals($repositoryRoot, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath (Join-Path $repositoryRoot "pyproject.toml") -PathType Leaf)) {
    throw "Run scripts/bootstrap.ps1 from the ForgeMCP repository root."
}

$pythonCommand = Get-Command python -ErrorAction Stop
$pythonVersion = & $pythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$pythonVersion -lt [version]"3.11") { throw "Python 3.11 or later is required (found $pythonVersion)." }
$nodeVersion = (& node --version).Trim().TrimStart("v")
if ([version]$nodeVersion -lt [version]"22.0") { throw "Node.js 22 or later is required (found v$nodeVersion)." }

if ($ValidateOnly) {
    Write-Host "Bootstrap prerequisites validated; no files were changed."
    return
}

$venvPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
function Invoke-Checked([string]$Description, [scriptblock]$Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE." }
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Invoke-Checked "virtual environment creation" { & $pythonCommand.Source -m venv (Join-Path $repositoryRoot ".venv") }
}
Invoke-Checked "pip upgrade" { & $venvPython -m pip install --upgrade pip }
Invoke-Checked "development dependency installation" { & $venvPython -m pip install -e ".[dev]" }
Invoke-Checked "frontend dependency installation" { & npm ci --prefix frontend }
