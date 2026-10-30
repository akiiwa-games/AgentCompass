# AgentCompass EasyTier supervisor. Source copy: tray/easytier_supervisor.ps1.
# easytier_setup.py installs it into easytier\bin (writable by SYSTEM and Administrators only); the SYSTEM
# task "AgentCompass EasyTier" runs that installed copy, never this one. No window: the task runs in session 0.
# Layout, relative to this script: ..\private\node.toml (host identity, private), ..\private\pre-start.ps1
# (optional host policy hook), ..\logs\core.log. Exit code is the core's, so Task Scheduler restarts on failure.
$ErrorActionPreference = 'Stop'
$bin = $PSScriptRoot
$home_ = Split-Path -Parent $bin
$core = Join-Path $bin 'easytier-core.exe'
$config = Join-Path $home_ 'private\node.toml'
$hook = Join-Path $home_ 'private\pre-start.ps1'
$logs = Join-Path $home_ 'logs'
$coreLog = Join-Path $logs 'core.log'
$superLog = Join-Path $logs 'supervisor.log'

function Write-Log([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $superLog -Value $line -Encoding UTF8
}

try {
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    foreach ($path in @($coreLog, $superLog)) {
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 4MB) {
            Move-Item -LiteralPath $path -Destination "$path.1" -Force
        }
    }
    if (-not (Test-Path -LiteralPath $core)) { Write-Log "missing $core"; exit 3 }
    if (-not (Test-Path -LiteralPath $config)) { Write-Log "missing $config"; exit 4 }

    # One core per host: any easytier-core that is not this task's own child blocks the start, visibly.
    $others = @(Get-CimInstance Win32_Process -Filter "Name = 'easytier-core.exe'" -ErrorAction SilentlyContinue)
    if ($others.Count -gt 0) {
        Write-Log ('another easytier-core is running (pid {0}); not starting a second one' -f (($others | ForEach-Object ProcessId) -join ','))
        exit 5
    }

    if (Test-Path -LiteralPath $hook) {
        try { & $hook } catch { Write-Log "pre-start hook failed: $($_.Exception.Message)" }
    }

    $check = Start-Process -FilePath $core -ArgumentList @('--config-file', $config, '--check-config', '--disable-env-parsing') `
        -WorkingDirectory $bin -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $logs 'check.out.log') -RedirectStandardError (Join-Path $logs 'check.err.log')
    if ($check.ExitCode -ne 0) { Write-Log "config check failed, exit $($check.ExitCode)"; exit 6 }

    $arguments = @(
        '--config-file', $config,
        '--disable-env-parsing',
        '--console-log-level', 'warn',
        '--file-log-level', 'off',
        '--rpc-portal', $(if ($env:AGENTCOMPASS_PORT_EASYTIER) { "127.0.0.1:$($env:AGENTCOMPASS_PORT_EASYTIER)" } else { '127.0.0.1:15888' }),
        '--rpc-portal-whitelist', '127.0.0.1/32,::1/128'
    )
    $process = Start-Process -FilePath $core -ArgumentList $arguments -WorkingDirectory $bin -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'core.out.log') -RedirectStandardError $coreLog
    Write-Log "started core pid $($process.Id)"
    Wait-Process -Id $process.Id
    $process.Refresh()
    Write-Log "core exited with code $($process.ExitCode)"
    exit $process.ExitCode
} catch {
    try { Write-Log ('fatal: ' + $_.Exception.Message + ' ' + $_.InvocationInfo.PositionMessage) } catch { }
    exit 1
}
