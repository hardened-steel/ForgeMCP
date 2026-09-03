[CmdletBinding()]
param(
    [ValidateSet("Apps", "Inspector", "Portable", "Live")]
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

$reportRoot = Join-Path ([IO.Path]::GetTempPath()) "forgemcp-verification"
New-Item -ItemType Directory -Force -Path $reportRoot | Out-Null
$runId = [guid]::NewGuid().ToString("N")
$runDirectory = Join-Path $reportRoot $runId
New-Item -ItemType Directory -Path $runDirectory | Out-Null
$phaseEvents = [Collections.Generic.List[object]]::new()
$processResults = [Collections.Generic.List[object]]::new()
$ownedProcesses = [Collections.Generic.List[object]]::new()
$startedAt = [DateTime]::UtcNow
$capabilityResult = "available"

function Add-Phase([string]$Name, [DateTime]$Timestamp = [DateTime]::UtcNow) {
    $phaseEvents.Add([ordered]@{ name = $Name; timestamp = $Timestamp.ToString("o") })
    Write-Host ("[{0}] {1}" -f $Timestamp.ToString("o"), $Name)
}

function Get-BoundedAppend([Text.StringBuilder]$Buffer, [string]$Text) {
    [void]$Buffer.AppendLine($Text)
    if ($Buffer.Length -gt 65536) { $Buffer.Remove(0, $Buffer.Length - 65536) }
}

function ConvertTo-CmdArgument([string]$Argument) {
    # npm.cmd is a batch entrypoint, so Windows requires one serialized
    # argument string at the association boundary.  Keep argv ownership in this
    # helper and reject cmd metacharacters rather than accepting a command line.
    if ($Argument -match '[&|<>^%]' -or $Argument.Contains("`r") -or $Argument.Contains("`n")) { throw "Unsafe cmd metacharacter in verify argument." }
    '"' + $Argument.Replace('"', '\"') + '"'
}

function Stop-OwnedProcessTree([Diagnostics.Process]$Process) {
    if ($Process.HasExited) { return }
    if ($IsWindows) {
        $killInfo = [Diagnostics.ProcessStartInfo]::new()
        $killInfo.FileName = "taskkill.exe"
        $killInfo.UseShellExecute = $false
        $killInfo.RedirectStandardOutput = $true
        $killInfo.RedirectStandardError = $true
        foreach ($argument in @("/pid", [string]$Process.Id, "/t", "/f")) { [void]$killInfo.ArgumentList.Add($argument) }
        $killer = [Diagnostics.Process]::new(); $killer.StartInfo = $killInfo
        [void]$killer.Start()
        if (-not $killer.WaitForExit(5000)) { throw "taskkill timed out while terminating verify-owned process tree $($Process.Id)." }
    } else {
        $Process.Kill($true)
    }
    if (-not $Process.WaitForExit(5000)) { throw "Verify-owned process tree $($Process.Id) did not exit after termination." }
}

function Invoke-CheckedProcess {
    param(
        [string]$Description,
        [string]$Executable,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 300,
        [switch]$QuietOutput
    )
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.WorkingDirectory = $repositoryRoot
    $isBatch = [IO.Path]::GetExtension($Executable).Equals(".cmd", [StringComparison]::OrdinalIgnoreCase)
    # A .cmd file is launched by Windows' documented association; this avoids
    # manufacturing an extra cmd.exe/powershell command string in the runner.
    $startInfo.UseShellExecute = $isBatch
    $startInfo.RedirectStandardOutput = -not $isBatch
    $startInfo.RedirectStandardError = -not $isBatch
    $startInfo.CreateNoWindow = $true
    if ($isBatch) { $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-CmdArgument $_ }) -join " ") }
    else { foreach ($argument in $Arguments) { [void]$startInfo.ArgumentList.Add($argument) } }
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $startInfo
    [void]$process.Start()
    $ownedProcesses.Add([ordered]@{ pid = $process.Id; started = $process.StartTime.ToUniversalTime().ToString("o"); description = $Description })
    # Start both reads before waiting so neither pipe can back up a child.  The
    # retained diagnostic tails are bounded; command output itself is relayed
    # only after both streams have completed.
    $stdoutTask = if ($isBatch) { $null } else { $process.StandardOutput.ReadToEndAsync() }
    $stderrTask = if ($isBatch) { $null } else { $process.StandardError.ReadToEndAsync() }
    $completed = $process.WaitForExit($TimeoutSeconds * 1000)
    if (-not $completed) {
        Stop-OwnedProcessTree $process
        $stdoutText = if ($null -eq $stdoutTask) { "" } else { $stdoutTask.GetAwaiter().GetResult() }; $stderrText = if ($null -eq $stderrTask) { "" } else { $stderrTask.GetAwaiter().GetResult() }
        throw "$Description timed out after $TimeoutSeconds seconds; only its created process tree was terminated. stderr: $stderrText"
    }
    $process.WaitForExit()
    $stdoutText = if ($null -eq $stdoutTask) { "" } else { $stdoutTask.GetAwaiter().GetResult() }; $stderrText = if ($null -eq $stderrTask) { "" } else { $stderrTask.GetAwaiter().GetResult() }
    if (-not $QuietOutput -and $stdoutText) { Write-Host $stdoutText }
    if (-not $QuietOutput -and $stderrText) { [Console]::Error.WriteLine($stderrText) }
    $result = [pscustomobject]@{ Description = $Description; ExitCode = $process.ExitCode; Output = $stdoutText.Substring([Math]::Max(0, $stdoutText.Length - [Math]::Min($stdoutText.Length, 65536))); Error = $stderrText.Substring([Math]::Max(0, $stderrText.Length - [Math]::Min($stderrText.Length, 65536))) }
    $processResults.Add($result)
    if ($result.ExitCode -ne 0) { throw "$Description failed with exit code $($result.ExitCode). stderr: $($result.Error)" }
    return $result
}

function Add-BrowserPhases([string]$Output) {
    foreach ($line in $Output -split "`r?`n") {
        if ($line -notmatch '^\{.*"phase"') { continue }
        try {
            $entry = $line | ConvertFrom-Json
            if ($entry.phase -and $entry.timestamp) { Add-Phase ([string]$entry.phase) ([DateTime]::Parse([string]$entry.timestamp).ToUniversalTime()) }
        } catch { }
    }
}

function Invoke-CheckedNpm {
    param([string]$Description, [string[]]$Arguments)
    # Use npm.cmd as the native batch entrypoint PowerShell resolves. No command
    # text is evaluated and no nested powershell/cmd runner is constructed.
    $lines = @(& npm.cmd @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $text = (($lines | ForEach-Object { $_.ToString() }) -join "`n")
    if ($text) { Write-Host $text }
    $result = [pscustomobject]@{ Description = $Description; ExitCode = $exitCode; Output = $text.Substring([Math]::Max(0, $text.Length - [Math]::Min($text.Length, 65536))); Error = "" }
    $processResults.Add($result)
    if ($exitCode -ne 0) { throw "$Description failed with exit code $exitCode." }
    return $result
}

function Invoke-Apps {
    Add-Phase "frontend-install"
    if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot "frontend\node_modules") -PathType Container)) { throw "Missing frontend/node_modules. Run .\scripts\bootstrap.ps1 first." }
    Add-Phase "frontend-build"
    Invoke-CheckedNpm "frontend build" @("run", "build", "--prefix", "frontend") | Out-Null
    Add-Phase "frontend-unit"
    $unit = Invoke-CheckedNpm "frontend unit and production browser harness" @("test", "--prefix", "frontend")
    Add-BrowserPhases $unit.Output
    if ($unit.Output -match '"status":"capability_absent"') { $script:capabilityResult = "capability_absent: compatible Chromium browser not found" }
    Add-Phase "asset-validation"
    Invoke-CheckedProcess "production asset freshness" "node.exe" @("frontend/git-status/build.mjs", "--check") 60 | Out-Null
    Add-Phase "asset-validation"
    Invoke-CheckedProcess "MCP App packaging/protocol tests" $python @("-m", "pytest", "-q", "-ra", "--basetemp", (Join-Path $runDirectory "apps-pytest"), "tests/test_mcp_apps.py") 180 | Out-Null
}

function Invoke-InspectorReview {
    $inspectorEntry = Join-Path $repositoryRoot "frontend\node_modules\@modelcontextprotocol\inspector\clients\launcher\build\index.js"
    if (-not (Test-Path -LiteralPath $inspectorEntry -PathType Leaf)) { throw "Missing locked MCP Inspector. Run .\scripts\bootstrap.ps1 after updating frontend dependencies." }
    # The locked Inspector launcher is a Node program; running that entrypoint
    # directly preserves bounded stdout/stderr drainage without npx or network.
    $inspector = "node.exe"
    $target = @("frontend/node_modules/@modelcontextprotocol/inspector/clients/launcher/build/index.js", "--cli", ".venv\Scripts\python.exe", "-m", "forgemcp.server", "--")
    $common = @("--format", "json", "--cwd", ".", "-e", "FORGEMCP_LOG_LEVEL=CRITICAL")
    Add-Phase "inspector-tools-list-app-info"
    $appList = Invoke-CheckedProcess "Inspector tools/list --app-info" $inspector ($target + @("--method", "tools/list", "--app-info") + $common) 120 -QuietOutput
    $appRows = @($appList.Output -split "`r?`n" | Where-Object { $_ -match '^\{' } | ForEach-Object { $_ | ConvertFrom-Json })
    $apps = @($appRows | Where-Object { $_.hasApp })
    if ($apps.Count -ne 1 -or $apps[0].toolName -ne "git__status" -or $apps[0].resourceUri -ne "ui://forgemcp/git/status" -or $apps[0].resourceMimeType -ne "text/html;profile=mcp-app") { throw "Inspector App inventory is not exactly git__status -> ui://forgemcp/git/status." }
    $csp = $apps[0].csp
    if ($null -eq $csp -or @($csp.connectDomains, $csp.resourceDomains, $csp.frameDomains, $csp.baseUriDomains | ForEach-Object { @($_).Count }).Where({ $_ -ne 0 }).Count -ne 0) { throw "Inspector reported unexpected App CSP metadata." }
    Add-Phase "inspector-tool-call-app-info"
    $toolInfo = Invoke-CheckedProcess "Inspector tools/call git__status --app-info" $inspector ($target + @("--method", "tools/call", "--tool-name", "git__status", "--app-info") + $common) 120 -QuietOutput
    if ($toolInfo.Output -notmatch '"resourceUri":"ui://forgemcp/git/status"') { throw "Inspector tools/call --app-info did not report the exact Git Status App URI." }
    Add-Phase "inspector-resource-read"
    $resource = Invoke-CheckedProcess "Inspector resources/read Git Status App" $inspector ($target + @("--method", "resources/read", "--uri", "ui://forgemcp/git/status") + $common) 120 -QuietOutput
    if ($resource.Output -notmatch 'text/html;profile=mcp-app') { throw "Inspector resource read did not return the Git Status App MIME type." }
    Add-Phase "inspector-no-apps-fallback"
    $plain = Invoke-CheckedProcess "Inspector ordinary tools/list" $inspector ($target + @("--method", "tools/list") + $common) 120 -QuietOutput
    if ($plain.Output -notmatch '"name":"git__status"') { throw "Inspector ordinary tools/list did not return git__status." }
}

function Invoke-CleanupAudit {
    Add-Phase "cleanup-audit"
    Invoke-CheckedProcess "git diff --check" "git.exe" @("diff", "--check") 60 | Out-Null
    $artifactPattern = '(^|[ /\\])(node_modules|\.tmp-pytest[^ /\\]*|\.pytest-tmp|playwright-report|test-results)([ /\\]|$)'
    $status = Invoke-CheckedProcess "git status" "git.exe" @("status", "--short") 60
    $unexpected = @($status.Output -split "`r?`n" | Where-Object { $_ -match $artifactPattern })
    if ($unexpected.Count -gt 0) { throw "Generated artifacts are visible to Git: $($unexpected -join '; ')" }
    $profiles = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter "forgemcp-git-status-chromium-*" -ErrorAction SilentlyContinue)
    if ($profiles.Count -gt 0) { throw "Test-owned Chromium profiles remain after verification." }
}

function Get-PytestSummary {
    $joined = ($processResults | Where-Object { $_.Description -match "pytest" } | ForEach-Object { $_.Output }) -join "`n"
    $passed = 0; $skipped = 0; $warnings = 0
    if ($joined -match '(\d+) passed') { $passed = [int]$Matches[1] }
    if ($joined -match '(\d+) skipped') { $skipped = [int]$Matches[1] }
    if ($joined -match '(\d+) warnings?') { $warnings = [int]$Matches[1] }
    [ordered]@{ passed = $passed; skipped = $skipped; warnings = $warnings; skip_reasons = @() }
}

$succeeded = $false; $failure = $null
try {
    switch ($Mode) {
        "Apps" { Invoke-Apps }
        "Inspector" { Invoke-InspectorReview }
        "Portable" { Invoke-Apps; Add-Phase "portable-pytest"; Invoke-CheckedProcess "portable pytest" $python @("-m", "pytest", "-q", "-ra", "--basetemp", (Join-Path $runDirectory "portable-pytest")) 900 | Out-Null; Add-Phase "compileall"; Invoke-CheckedProcess "compileall" $python @("-m", "compileall", "-q", "src", "tests") 120 | Out-Null; Invoke-CleanupAudit }
        "Live" { Invoke-Apps; Add-Phase "live-pytest"; $live = Join-Path $runDirectory "live"; Invoke-CheckedProcess "live acceptance pytest" $python @("-m", "pytest", "-q", "-ra", "--run-forgemcp-live-acceptance", "--basetemp", (Join-Path $runDirectory "live-pytest"), "--forgemcp-live-report-dir", $live) 1800 | Out-Null; Add-Phase "compileall"; Invoke-CheckedProcess "compileall" $python @("-m", "compileall", "-q", "src", "tests") 120 | Out-Null; Invoke-CleanupAudit }
    }
    $succeeded = $true
} catch {
    $failure = $_.Exception.Message
    throw
} finally {
    $endedAt = [DateTime]::UtcNow
    $asset = Join-Path $repositoryRoot "src\forgemcp\apps\assets\git-status.html"
    $report = [ordered]@{
        schema_version = 1; run_id = $runId; mode = $Mode; python_version = (& $python --version 2>&1).ToString().Trim(); node_version = (& node.exe --version).ToString().Trim(); platform = [Runtime.InteropServices.RuntimeInformation]::OSDescription
        phases = @($phaseEvents); exit_state = if ($succeeded) { "success" } else { "failed" }; duration_ms = [int]($endedAt - $startedAt).TotalMilliseconds; pytest = Get-PytestSummary; capability_result = if ($succeeded) { $capabilityResult } else { "failed" }; asset_digest = if (Test-Path -LiteralPath $asset) { (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
    }
    if (-not $succeeded) { $report.failure_category = "verification_failed" }
    $reportPath = Join-Path $reportRoot ("run-" + $runId + ".json")
    $temporaryReport = "$reportPath.tmp"
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporaryReport -Encoding utf8
    Move-Item -LiteralPath $temporaryReport -Destination $reportPath -Force
    Write-Host "verification report: $reportPath"
}
