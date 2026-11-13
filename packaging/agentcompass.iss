; Per-user AgentCompass installer: the tray and the `agentcompass` command line. No admin, no Python needed.
; {app}\cli\agentcompass.exe goes on the user PATH (HKCU\Environment\Path); uninstall takes it off again.
; Router setup (skills, global rules, the opt-in gate hook) is never run silently on a machine that was not set up: the
; tray offers it on first launch. Scripted installs choose it on the command line:
;   AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks     setup --yes --hooks    (skills, rules and the gate hook)
;   AgentCompass-tray-setup.exe /VERYSILENT /SETUP=nohooks   setup --yes --no-hooks (skills and rules only)
; On a machine that was set up before (%USERPROFILE%\.agent-compass\setup-manifest.json exists), an install without
; /SETUP applies that setup again with the new files (setup --yes --no-usage: the hook stays as it is).
; The setup output goes to %USERPROFILE%\.agent-compass\installer-setup.log.
; The install needs the running tray gone. It stops the tray, waits for its files to be released, and gives up before
; changing anything when they are not; a tray it stopped is started again when the install does not finish.
; The tray is always started through its scheduled task, so it never runs as a child of Setup.
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
; Setup (Inno Setup 6.7 and later) turns on Windows' RedirectionGuard for itself and its child processes inherit it:
; they cannot follow a junction a non-elevated process made. This installer never elevates (PrivilegesRequired=lowest),
; so the guard protects nothing here, and "agentcompass setup" run below must follow the skill links it made earlier.
RedirectionGuard=no

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
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Register-ScheduledTask -TaskName 'AgentCompass' -Description 'AgentCompass tray' -Action (New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force | Out-Null"""; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/D /C """"{app}\cli\agentcompass.exe"" setup --yes {code:SetupArguments} >> ""{%USERPROFILE}\.agent-compass\installer-setup.log"" 2>&1"""; Check: SetupRequested; Flags: runhidden waituntilterminated

[Code]
const
  EnvKey = 'Environment';
  ExitWaitMs = 60000;

var
  TrayStopped, InstallFinished: Boolean;

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

{ A machine that ran "agentcompass setup" keeps this file until "agentcompass uninstall". }
function SetUpBefore(): Boolean;
begin
  Result := FileExists(ExpandConstant('{%USERPROFILE}\.agent-compass\setup-manifest.json'));
end;

function SetupRequested(): Boolean;
begin
  Result := (SetupMode() = 'hooks') or (SetupMode() = 'nohooks') or ((SetupMode() = '') and SetUpBefore());
end;

function SetupArguments(Param: String): String;
begin
  if SetupMode() = 'hooks' then
    Result := '--hooks'
  else if SetupMode() = 'nohooks' then
    Result := '--no-hooks'
  else
    Result := '--no-usage';
end;

{ The tray is started through its scheduled task, by the Task Scheduler service, never as a child of Setup. A child
  inherits the process mitigations of Setup and of whatever started Setup (the tray's own updater, itself started by an
  earlier installer): under RedirectionGuard the tray cannot follow the skill links. A machine where the task could not
  be registered gets the tray started directly. }
procedure StartTray();
var
  ResultCode: Integer;
begin
  if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
    exit;
  if Exec('schtasks.exe', '/Run /TN "AgentCompass"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
    exit;
  Exec(ExpandConstant('{app}\{#MyAppExeName}'), '', ExpandConstant('{app}'), SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

{ [Run] entries (the scheduled task, setup) have run by ssPostInstall. }
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    AddCliToPath();
    InstallFinished := True;
    StartTray();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveCliFromPath();
end;

{ A running program keeps its own file closed to writers: the file opens for writing only when no process runs it. }
function Replaceable(FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := True;
  if not FileExists(FileName) then
    exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := False;
  end;
end;

function ProgramsReleased(): Boolean;
begin
  Result := Replaceable(ExpandConstant('{app}\{#MyAppExeName}')) and Replaceable(ExpandConstant('{app}\cli\agentcompass.exe'));
end;

{ A killed process can take a while to leave. Copying files before it has left ends in "Setup was unable to
  automatically close all applications" and a rolled-back install, so wait here and stop before any change. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode, Waited: Integer;
begin
  Result := '';
  TrayStopped := not Replaceable(ExpandConstant('{app}\{#MyAppExeName}'));
  Exec('taskkill.exe', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while (not ProgramsReleased()) and (Waited < ExitWaitMs) do
  begin
    Sleep(500);
    Waited := Waited + 500;
  end;
  if not ProgramsReleased() then
    Result := 'AgentCompass is still closing and its files are in use. Nothing was changed. Run the installer again in a minute.';
end;

{ A finished install starts the tray at ssPostInstall. An install that stopped the tray and did not finish starts it here. }
procedure DeinitializeSetup();
begin
  if TrayStopped and (not InstallFinished) then
    StartTray();
end;

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; RunOnceId: "AcStopTray"; Flags: runhidden waituntilterminated
Filename: "{app}\cli\agentcompass.exe"; Parameters: "uninstall --yes"; RunOnceId: "AcUninstallCli"; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""AgentCompass"" /F"; RunOnceId: "AcDeleteTask"; Flags: runhidden waituntilterminated
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""foreach ($n in 'PRO_CONSULT_LAUNCHER','PRO_CONSULT_LAUNCHER_ARGS','CODEX_WEB_GPT_EXTERNAL_TRAY') {{ Remove-ItemProperty -Path 'HKCU:\Environment' -Name $n -ErrorAction SilentlyContinue }"""; RunOnceId: "AcRemoveEnv"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: files; Name: "{userstartup}\AgentCompass.lnk"
Type: filesandordirs; Name: "{app}\app"
Type: files; Name: "{app}\manifest.json"
Type: files; Name: "{app}\runtime-info.json"
