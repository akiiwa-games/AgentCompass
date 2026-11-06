; Per-user AgentCompass installer: the tray and the `agentcompass` command line. No admin, no Python needed.
; {app}\cli\agentcompass.exe goes on the user PATH (HKCU\Environment\Path); uninstall takes it off again.
; Router setup (skills, global rules, the opt-in gate hook) is never run silently by default: the tray offers it on
; first launch. Scripted installs choose it on the command line:
;   AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks     setup --yes --hooks    (skills, rules and the gate hook)
;   AgentCompass-tray-setup.exe /VERYSILENT /SETUP=nohooks   setup --yes --no-hooks (skills and rules only)
; The setup output goes to %USERPROFILE%\.agent-compass\installer-setup.log.
; The three environment names must match tray/deploy.py wanted_env():
;   PRO_CONSULT_LAUNCHER, PRO_CONSULT_LAUNCHER_ARGS, CODEX_WEB_GPT_EXTERNAL_TRAY
; Version is passed by packaging/build.ps1 as /DMyAppVersion=...
; This script does not sign binaries. See packaging/SIGNING.md.

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by packaging/build.ps1
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
CloseApplications=force
RestartApplications=no
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
; How a user finds the tray again after quitting it; a second launch opens the running tray's window.
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
; Files a newer build dropped must not linger in the bundle; the folder itself keeps its path (skill links point into it).
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\cli\_internal"
Type: filesandordirs; Name: "{app}\app\current"
Type: filesandordirs; Name: "{app}\app\previous"
Type: filesandordirs; Name: "{app}\app\packages"

[Dirs]
; The /SETUP log goes here. [Run] entries run before ssPostInstall, so the folder must exist by then; the user's state stays on uninstall.
Name: "{%USERPROFILE}\.agent-compass"; Flags: uninsneveruninstall

[Files]
Source: "..\dist\AgentCompass\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{cmd}"; Parameters: "/D /C ""if not exist ""{app}\cli\_internal\agentcompass\data"" mkdir ""{app}\cli\_internal\agentcompass\data"" & if exist ""{app}\cli\_internal\agentcompass\data\skills"" (rmdir ""{app}\cli\_internal\agentcompass\data\skills"" >nul 2>&1 & if exist ""{app}\cli\_internal\agentcompass\data\skills"" rmdir /S /Q ""{app}\cli\_internal\agentcompass\data\skills"" >nul 2>&1) & mklink /J ""{app}\cli\_internal\agentcompass\data\skills"" ""{app}\app\current\skills"" >nul 2>&1 & if exist ""{app}\cli\_internal\agentcompass\data\setup"" (rmdir ""{app}\cli\_internal\agentcompass\data\setup"" >nul 2>&1 & if exist ""{app}\cli\_internal\agentcompass\data\setup"" rmdir /S /Q ""{app}\cli\_internal\agentcompass\data\setup"" >nul 2>&1) & mklink /J ""{app}\cli\_internal\agentcompass\data\setup"" ""{app}\app\current\setup"" >nul 2>&1"""; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Register-ScheduledTask -TaskName 'AgentCompass' -Description 'AgentCompass tray' -Action (New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force | Out-Null; Unregister-ScheduledTask -TaskName 'Route Manager' -Confirm:$false -ErrorAction SilentlyContinue"""; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/D /C """"{app}\cli\agentcompass.exe"" setup --yes {code:SetupHookFlag} >> ""{%USERPROFILE}\.agent-compass\installer-setup.log"" 2>&1"""; Check: SetupRequested; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Flags: nowait

[Code]
const
  EnvKey = 'Environment';

function CliDir(): String;
begin
  Result := ExpandConstant('{app}\cli');
end;

function PathHas(Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

{ First in the user PATH, so a new terminal runs this agentcompass before an older uv/pip copy in the user PATH. }
procedure AddCliToPath();
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    Paths := '';
  if PathHas(Paths, CliDir()) then
    exit;
  if Paths = '' then
    Paths := CliDir()
  else
    Paths := CliDir() + ';' + Paths;
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths);
end;

procedure RemoveCliFromPath();
var
  Paths, Needle: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  Needle := ';' + Uppercase(CliDir()) + ';';
  P := Pos(Needle, Uppercase(Paths));
  while P > 0 do
  begin
    Delete(Paths, P, Length(Needle) - 1);
    P := Pos(Needle, Uppercase(Paths));
  end;
  while (Length(Paths) > 0) and (Paths[1] = ';') do
    Delete(Paths, 1, 1);
  while (Length(Paths) > 0) and (Paths[Length(Paths)] = ';') do
    Delete(Paths, Length(Paths), 1);
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths);
end;

function SetupMode(): String;
begin
  Result := Lowercase(ExpandConstant('{param:SETUP|}'));
end;

function SetupRequested(): Boolean;
begin
  Result := (SetupMode() = 'hooks') or (SetupMode() = 'nohooks');
end;

function SetupHookFlag(Param: String): String;
begin
  if SetupMode() = 'hooks' then
    Result := '--hooks'
  else
    Result := '--no-hooks';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    AddCliToPath();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveCliFromPath();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; RunOnceId: "AcStopTray"; Flags: runhidden waituntilterminated
Filename: "{app}\cli\agentcompass.exe"; Parameters: "uninstall --yes"; RunOnceId: "AcUninstallCli"; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""AgentCompass"" /F"; RunOnceId: "AcDeleteTask"; Flags: runhidden waituntilterminated
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""Route Manager"" /F"; RunOnceId: "AcDeleteLegacyTask"; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""foreach ($n in 'PRO_CONSULT_LAUNCHER','PRO_CONSULT_LAUNCHER_ARGS','CODEX_WEB_GPT_EXTERNAL_TRAY') {{ Remove-ItemProperty -Path 'HKCU:\Environment' -Name $n -ErrorAction SilentlyContinue }"""; RunOnceId: "AcRemoveEnv"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: files; Name: "{userstartup}\Route Manager.lnk"
Type: files; Name: "{userstartup}\AgentCompass.lnk"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\cli\_internal\agentcompass"
Type: files; Name: "{app}\manifest.json"
Type: files; Name: "{app}\runtime-info.json"
