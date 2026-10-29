# Build the one-folder tray and, when Inno Setup is installed, the per-user installer.
# Does not launch AgentCompass.exe. Signing runs only when a certificate is configured.
param(
    [switch]$SkipPyInstaller,
    [switch]$SkipInno,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Init = Join-Path $Root "agentcompass\__init__.py"
$Text = Get-Content -Raw -Encoding UTF8 $Init
if ($Text -notmatch '__version__\s*=\s*[''"]([^''"]+)[''"]') {
    throw "Could not read __version__ from $Init"
}
$Version = $Matches[1]
Write-Host "AgentCompass $Version"

if (-not $SkipPyInstaller) {
    $Spec = Join-Path $PSScriptRoot "agentcompass-tray.spec"
    $Work = Join-Path $PSScriptRoot ".build"
    $Dist = Join-Path $Root "dist"
    & $Python -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work $Spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
}

function Find-Iscc {
    if ($env:ISCC -and (Test-Path -LiteralPath $env:ISCC)) { return $env:ISCC }
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

if ($SkipInno) {
    Write-Host "Inno Setup skipped."
} else {
    $Iscc = Find-Iscc
    if (-not $Iscc) {
        Write-Host "ISCC was not found. The tray folder was built without an installer. Install Inno Setup 6 or set ISCC."
    } else {
        & $Iscc "/DMyAppVersion=$Version" (Join-Path $PSScriptRoot "agentcompass.iss")
        if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
    }
}

$SignTool = $env:AGENTCOMPASS_SIGNTOOL
$Thumb = $env:AGENTCOMPASS_CERT_THUMBPRINT
$Pfx = $env:AGENTCOMPASS_CERT_PFX
if ($SignTool -and (Test-Path -LiteralPath $SignTool) -and ($Thumb -or $Pfx)) {
    $targets = @(
        (Join-Path $Root "dist\AgentCompass\AgentCompass.exe"),
        (Join-Path $Root "dist\AgentCompass-tray-setup.exe")
    )
    foreach ($target in $targets) {
        if (-not (Test-Path -LiteralPath $target)) { continue }
        if ($Thumb) {
            & $SignTool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /sha1 $Thumb $target
        } else {
            & $SignTool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /f $Pfx /p $env:AGENTCOMPASS_CERT_PASSWORD $target
        }
        if ($LASTEXITCODE -ne 0) { throw "signtool failed for $target ($LASTEXITCODE)" }
    }
} else {
    Write-Host "Unsigned build. Windows SmartScreen will warn on first launch. See packaging/SIGNING.md."
}
