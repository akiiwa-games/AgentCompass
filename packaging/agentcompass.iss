; Per-user AgentCompass tray installer. No admin.
; The three environment names must match tray/deploy.py wanted_env():
;   PRO_CONSULT_LAUNCHER, PRO_CONSULT_LAUNCHER_ARGS, CODEX_WEB_GPT_EXTERNAL_TRAY
; Version is passed by packaging/build.ps1 as /DMyAppVersion=...
; This script does not sign binaries. See packaging/SIGNING.md.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

#define MyAppName "AgentCompass"
#define MyAppPublisher "akiiwa-games"
#define MyAppURL "https://github.com/akiiwa-games/AgentCompass"
#define MyAppExeName "AgentCompass.exe"

[Setup]
AppId={{A7B3E2C1-4F58-4C9A-9D2E-1B6A8C0D5E77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\AgentCompass
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=AgentCompass-tray-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName} tray

[Files]
Source: "..\dist\AgentCompass\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Register-ScheduledTask -TaskName 'Route Manager' -Description 'Route Manager tray' -Action (New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force | Out-Null"""; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; RunOnceId: "AcStopTray"; Flags: runhidden waituntilterminated
Filename: "cmd.exe"; Parameters: "/D /C agentcompass uninstall"; RunOnceId: "AcUninstallCli"; Flags: runhidden waituntilterminated
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""Route Manager"" /F"; RunOnceId: "AcDeleteTask"; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""foreach ($n in 'PRO_CONSULT_LAUNCHER','PRO_CONSULT_LAUNCHER_ARGS','CODEX_WEB_GPT_EXTERNAL_TRAY') {{ Remove-ItemProperty -Path 'HKCU:\Environment' -Name $n -ErrorAction SilentlyContinue }}"""; RunOnceId: "AcRemoveEnv"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: files; Name: "{userstartup}\Route Manager.lnk"
Type: files; Name: "{userstartup}\AgentCompass.lnk"
