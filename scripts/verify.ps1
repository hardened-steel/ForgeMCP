#requires -Version 5.1
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

function Test-ForgeIsWindows {
    $platform = [Environment]::OSVersion.Platform
    return $platform -eq [PlatformID]::Win32NT -or $platform -eq [PlatformID]::Win32Windows -or $platform -eq [PlatformID]::Win32S
}

# $IsWindows is only an automatic variable in PowerShell Core.  Keep one
# script-scoped immutable value so StrictMode is safe in Windows PowerShell 5.1.
Set-Variable -Name ForgeIsWindows -Value (Test-ForgeIsWindows) -Scope Script -Option Constant

function Add-Phase([string]$Name, [DateTime]$Timestamp = [DateTime]::UtcNow) {
    $phaseEvents.Add([ordered]@{ name = $Name; timestamp = $Timestamp.ToString("o") })
    Write-Host ("[{0}] {1}" -f $Timestamp.ToString("o"), $Name)
}

function Get-BoundedAppend([Text.StringBuilder]$Buffer, [string]$Text) {
    [void]$Buffer.AppendLine($Text)
    if ($Buffer.Length -gt 65536) { $Buffer.Remove(0, $Buffer.Length - 65536) }
}

function ConvertTo-WindowsCommandLineArgument([string]$Argument) {
    if ($null -eq $Argument) { throw "Process arguments cannot be null." }
    if ($Argument.IndexOf([char]0) -ge 0) { throw "Process arguments cannot contain NUL characters." }

    # Quote every argument.  This is the inverse of the Microsoft C runtime's
    # CommandLineToArgv-style parsing: backslashes before a quote are doubled
    # and escaped, while trailing backslashes are doubled before the terminator.
    $quoted = [Text.StringBuilder]::new()
    [void]$quoted.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$quoted.Append('\', ($backslashes * 2) + 1)
            [void]$quoted.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) { [void]$quoted.Append('\', $backslashes) }
        [void]$quoted.Append($character)
        $backslashes = 0
    }
    if ($backslashes -gt 0) { [void]$quoted.Append('\', $backslashes * 2) }
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Set-ProcessStartInfoArguments([Diagnostics.ProcessStartInfo]$StartInfo, [string[]]$Arguments = @()) {
    if ($null -eq $StartInfo.FileName -or $StartInfo.FileName.Length -eq 0) { throw "Process executable cannot be empty." }
    if ($StartInfo.FileName.IndexOf([char]0) -ge 0) { throw "Process executable cannot contain NUL characters." }

    # ArgumentList is available on PowerShell 7/.NET, but not on Windows
    # PowerShell 5.1's .NET Framework ProcessStartInfo.  Test metadata rather
    # than reading the missing property under StrictMode.
    $argumentListProperty = $StartInfo.PSObject.Properties['ArgumentList']
    if ($null -ne $argumentListProperty) {
        foreach ($argument in $Arguments) {
            if ($null -eq $argument) { throw "Process arguments cannot be null." }
            if ($argument.IndexOf([char]0) -ge 0) { throw "Process arguments cannot contain NUL characters." }
            [void]$argumentListProperty.Value.Add($argument)
        }
        return
    }

    $StartInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument $_ }) -join ' ')
}

function Stop-OwnedProcessTree([Diagnostics.Process]$Process) {
    if ($Process.HasExited) { return }
    if ($script:ForgeIsWindows) {
        $killInfo = [Diagnostics.ProcessStartInfo]::new()
        $killInfo.FileName = "taskkill.exe"
        $killInfo.UseShellExecute = $false
        Set-ProcessStartInfoArguments $killInfo @("/pid", [string]$Process.Id, "/t", "/f")
        $killer = [Diagnostics.Process]::new(); $killer.StartInfo = $killInfo
        [void]$killer.Start()
        if (-not $killer.WaitForExit(5000)) { throw "taskkill timed out while terminating verify-owned process tree $($Process.Id)." }
    } else {
        # Kill(bool) is not available on .NET Framework.  The Windows path
        # above owns tree cleanup through a bounded taskkill invocation.
        $Process.Kill()
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
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
    $startInfo.CreateNoWindow = $true
    Set-ProcessStartInfoArguments $startInfo $Arguments
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $startInfo
    [void]$process.Start()
    $ownedProcesses.Add([ordered]@{ pid = $process.Id; started = $process.StartTime.ToUniversalTime().ToString("o"); description = $Description })
    # Start both reads before waiting so neither pipe can back up a child.  The
    # retained diagnostic tails are bounded; command output itself is relayed
    # only after both streams have completed.
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $completed = $process.WaitForExit($TimeoutSeconds * 1000)
    if (-not $completed) {
        Stop-OwnedProcessTree $process
        $stdoutText = $stdoutTask.GetAwaiter().GetResult(); $stderrText = $stderrTask.GetAwaiter().GetResult()
        throw "$Description timed out after $TimeoutSeconds seconds; only its created process tree was terminated. stderr: $stderrText"
    }
    $process.WaitForExit()
    $stdoutText = $stdoutTask.GetAwaiter().GetResult(); $stderrText = $stderrTask.GetAwaiter().GetResult()
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

function Invoke-Apps {
    Add-Phase "frontend-install"
    if (-not (Test-Path -LiteralPath (Join-Path $repositoryRoot "frontend\node_modules") -PathType Container)) { throw "Missing frontend/node_modules. Run .\scripts\bootstrap.ps1 first." }
    Add-Phase "frontend-build"
    Invoke-CheckedProcess "frontend build Git Status" "node.exe" @("frontend/git-status/build.mjs") 60 | Out-Null
    Invoke-CheckedProcess "frontend build Project Status" "node.exe" @("frontend/project-status/build.mjs") 60 | Out-Null
    Add-Phase "browser-dependency"
    Invoke-CheckedProcess "pinned browser dependency" "node.exe" @("frontend/git-status/browser-dependency.mjs") 60 | Out-Null
    # Keep report metadata free of the raw argv term and values; the probe
    # itself remains a real process-argument preservation test.
    Add-Phase "process-argument-round-trip"
    $argvProbe = Join-Path $repositoryRoot "tests\fixtures\verify_argv_probe.py"
    $argvCases = @("", "plain", "with space", "C:\Program Files\LLVM\bin", 'trailing\', 'quote"inside', 'backslashes\\before\"quote', "--value=a b", "кириллица")
    $argvResult = Invoke-CheckedProcess "argv round-trip probe" $python (@($argvProbe) + $argvCases) 60 -QuietOutput
    try {
        # Windows PowerShell 5.1 returns a JSON array as one pipeline object,
        # so enumerate the decoded value explicitly to retain empty argv[0].
        $decodedArguments = $argvResult.Output.Trim() | ConvertFrom-Json
        $receivedArguments = [Collections.Generic.List[string]]::new()
        foreach ($receivedArgument in $decodedArguments) { $receivedArguments.Add([string]$receivedArgument) }
    } catch { throw "argv round-trip probe returned invalid JSON." }
    if ($receivedArguments.Count -ne $argvCases.Count) { throw "argv round-trip probe returned an unexpected argument count." }
    for ($index = 0; $index -lt $argvCases.Count; $index++) {
        if ([string]$receivedArguments[$index] -cne [string]$argvCases[$index]) { throw "argv round-trip probe changed argument index $index." }
    }
    Add-Phase "frontend-unit"
    $unit = Invoke-CheckedProcess "frontend unit and production browser harness" "node.exe" @("--test", "frontend/git-status/test.mjs", "frontend/project-status/test.mjs") 120
    Add-BrowserPhases $unit.Output
    Add-Phase "asset-validation"
    Invoke-CheckedProcess "production asset freshness" "node.exe" @("frontend/git-status/build.mjs", "--check") 60 | Out-Null
    Invoke-CheckedProcess "Project Status asset freshness" "node.exe" @("frontend/project-status/build.mjs", "--check") 60 | Out-Null
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
    if ($apps.Count -ne 2 -or @($apps | Where-Object { ($_.toolName -eq "git__status" -and $_.resourceUri -eq "ui://forgemcp/git/status") -or ($_.toolName -eq "project__status" -and $_.resourceUri -eq "ui://forgemcp/project/status") }).Count -ne 2 -or @($apps | Where-Object { $_.resourceMimeType -ne "text/html;profile=mcp-app" }).Count -ne 0) { throw "Inspector App inventory is not exactly the Git and Project Status bindings." }
    foreach ($app in $apps) { $csp = $app.csp; if ($null -eq $csp -or @($csp.connectDomains, $csp.resourceDomains, $csp.frameDomains, $csp.baseUriDomains | ForEach-Object { @($_).Count }).Where({ $_ -ne 0 }).Count -ne 0) { throw "Inspector reported unexpected App CSP metadata." } }
    Add-Phase "inspector-tool-call-app-info"
    $toolInfo = Invoke-CheckedProcess "Inspector tools/call git__status --app-info" $inspector ($target + @("--method", "tools/call", "--tool-name", "git__status", "--app-info") + $common) 120 -QuietOutput
    if ($toolInfo.Output -notmatch '"resourceUri":"ui://forgemcp/git/status"') { throw "Inspector tools/call --app-info did not report the exact Git Status App URI." }
    $projectToolInfo = Invoke-CheckedProcess "Inspector tools/call project__status --app-info" $inspector ($target + @("--method", "tools/call", "--tool-name", "project__status", "--app-info") + $common) 120 -QuietOutput
    if ($projectToolInfo.Output -notmatch '"resourceUri":"ui://forgemcp/project/status"') { throw "Inspector tools/call --app-info did not report the exact Project Status App URI." }
    Add-Phase "inspector-resource-read"
    $resource = Invoke-CheckedProcess "Inspector resources/read Git Status App" $inspector ($target + @("--method", "resources/read", "--uri", "ui://forgemcp/git/status") + $common) 120 -QuietOutput
    if ($resource.Output -notmatch 'text/html;profile=mcp-app') { throw "Inspector resource read did not return the Git Status App MIME type." }
    $projectResource = Invoke-CheckedProcess "Inspector resources/read Project Status App" $inspector ($target + @("--method", "resources/read", "--uri", "ui://forgemcp/project/status") + $common) 120 -QuietOutput
    if ($projectResource.Output -notmatch 'text/html;profile=mcp-app') { throw "Inspector resource read did not return the Project Status App MIME type." }
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
    $profiles = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter "forgemcp-git-status-puppeteer-*" -ErrorAction SilentlyContinue)
    if ($profiles.Count -gt 0) { throw "Test-owned Puppeteer profiles remain after verification." }
    $remainingOwned = @($ownedProcesses | Where-Object { $null -ne (Get-Process -Id $_.pid -ErrorAction SilentlyContinue) })
    if ($remainingOwned.Count -gt 0) { throw "Verify-owned child processes remain after verification." }
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
        "Portable" { Invoke-Apps; Add-Phase "portable-pytest"; Invoke-CheckedProcess "portable pytest" $python @("-m", "pytest", "-q", "-ra", "--basetemp", (Join-Path $runDirectory "portable-pytest")) 900 | Out-Null; Add-Phase "compileall"; Invoke-CheckedProcess "compileall" $python @("-m", "compileall", "-q", "src", "tests") 120 | Out-Null }
        "Live" { Invoke-Apps; Add-Phase "live-pytest"; $live = Join-Path $runDirectory "live"; Invoke-CheckedProcess "live acceptance pytest" $python @("-m", "pytest", "-q", "-ra", "--run-forgemcp-live-acceptance", "--basetemp", (Join-Path $runDirectory "live-pytest"), "--forgemcp-live-report-dir", $live) 1800 | Out-Null; Add-Phase "compileall"; Invoke-CheckedProcess "compileall" $python @("-m", "compileall", "-q", "src", "tests") 120 | Out-Null }
    }
    $succeeded = $true
} catch {
    $failure = $_.Exception.Message
    throw
} finally {
    $cleanupFailure = $null
    try {
        Invoke-CleanupAudit
    } catch {
        $cleanupFailure = $_.Exception.Message
        $succeeded = $false
        if ($null -ne $failure) { Write-Error "Cleanup audit failed after verification failure." }
    }
    $endedAt = [DateTime]::UtcNow
    $asset = Join-Path $repositoryRoot "src\forgemcp\apps\assets\git-status.html"
    $report = [ordered]@{
        schema_version = 1; run_id = $runId; mode = $Mode; python_version = (& $python --version 2>&1).ToString().Trim(); node_version = (& node.exe --version).ToString().Trim(); platform = [Runtime.InteropServices.RuntimeInformation]::OSDescription
        phases = @($phaseEvents); exit_state = if ($succeeded) { "success" } else { "failed" }; duration_ms = [int]($endedAt - $startedAt).TotalMilliseconds; pytest = Get-PytestSummary; capability_result = if ($succeeded) { $capabilityResult } else { "failed" }; asset_digest = if (Test-Path -LiteralPath $asset) { (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
    }
    if (-not $succeeded) { $report.failure_category = "verification_failed" }
    $reportPath = Join-Path $reportRoot ("run-" + $runId + ".json")
    $temporaryReport = "$reportPath.tmp"
    $reportJson = $report | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText($temporaryReport, $reportJson, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryReport -Destination $reportPath -Force
    Write-Host "verification report: $reportPath"
    if ($null -ne $cleanupFailure -and $null -eq $failure) { throw "Cleanup audit failed after verification: $cleanupFailure" }
}
