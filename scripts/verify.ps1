[CmdletBinding()]
param(
    [ValidateSet("Apps", "Portable", "Live")]
    [string]$Mode = "Portable"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$currentDirectory = [IO.Path]::GetFullPath((Get-Location).Path)
if (-not $currentDirectory.Equals($repositoryRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Run scripts/verify.ps1 from the ForgeMCP repository root." }
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Missing .venv. Run .\scripts\bootstrap.ps1 first." }

$systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temporaryDirectory = [IO.Path]::GetFullPath((Join-Path $systemTemp ("forgemcp-verify-" + [guid]::NewGuid().ToString("N"))))
if (-not $temporaryDirectory.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) { throw "Refusing non-system temporary path: $temporaryDirectory" }
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null

function Invoke-Checked([string]$Description, [scriptblock]$Action) {
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Description failed with exit code $LASTEXITCODE." }
}

function Invoke-Apps {
    Invoke-Checked "frontend build" { npm run build --prefix frontend }
    Invoke-Checked "frontend tests and Chromium production-asset harness" { npm test --prefix frontend }
    Invoke-Checked "production asset freshness" { node frontend/git-status/build.mjs --check }
    Invoke-Checked "MCP App packaging/protocol tests" { & $python -m pytest -q -ra --basetemp $temporaryDirectory tests/test_mcp_apps.py }
}

function Invoke-CleanupAudit {
    $artifactPattern = '(^|[ /\\])(node_modules|\.tmp-pytest[^ /\\]*|\.pytest-tmp|playwright-report|test-results)([ /\\]|$)'
    $status = @(git status --short)
    $unexpected = @($status | Where-Object { $_ -match $artifactPattern })
    if ($unexpected.Count -gt 0) { throw "Generated artifacts are visible to Git:`n$($unexpected -join "`n")" }
    Invoke-Checked "git diff --check" { git diff --check }
    $status | ForEach-Object { Write-Host $_ }
}

try {
    Invoke-Apps
    if ($Mode -eq "Portable") {
        Invoke-Checked "portable pytest" { & $python -m pytest -q -ra --basetemp $temporaryDirectory }
        Invoke-Checked "compileall" { & $python -m compileall -q src tests }
        Invoke-CleanupAudit
    } elseif ($Mode -eq "Live") {
        $liveReports = Join-Path $temporaryDirectory "live-reports"
        Invoke-Checked "live acceptance pytest" { & $python -m pytest -q -ra --run-forgemcp-live-acceptance --basetemp $temporaryDirectory --forgemcp-live-report-dir $liveReports }
        Get-ChildItem -LiteralPath $liveReports -File | ForEach-Object { Write-Host "live report: $($_.Name)" }
        Invoke-Checked "compileall" { & $python -m compileall -q src tests }
        Invoke-CleanupAudit
    }
} finally {
    $resolvedTemporary = [IO.Path]::GetFullPath($temporaryDirectory)
    if ($resolvedTemporary.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedTemporary)) {
        Remove-Item -LiteralPath $resolvedTemporary -Recurse -Force
    }
}
