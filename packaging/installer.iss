#ifndef BuildVersion
  #error BuildVersion must be supplied by build_release.ps1
#endif

#ifdef InstallerTest
  #define UpgradeAppId "{64FC1CE2-89FD-451C-B145-8CFBF7BCF6D0}"
  #define InstallAppName "IndexTTS Voice App Upgrade Test"
#else
  #define UpgradeAppId "{8256E0CB-4A91-4AC2-A955-174464D90A20}"
  #define InstallAppName "IndexTTS Voice App"
#endif

[Setup]
AppId={#"{" + UpgradeAppId}
AppName={#InstallAppName}
AppVersion={#BuildVersion}
DefaultDirName={code:DefaultInstallDirectory}
UsePreviousAppDir=no
DefaultGroupName=IndexTTS Voice App
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
WizardStyle=modern
DisableWelcomePage=no
CloseApplications=yes
UninstallDisplayIcon={app}\IndexTTSVoiceApp.exe

#ifdef InstallerTest
OutputDir={#UpgradeTestRoot}\packages
OutputBaseFilename=Upgrade-Test-{#BuildVersion}
CreateUninstallRegKey=yes
UninstallDisplayName=IndexTTS Voice App Upgrade Test
#else
OutputDir=..\release\IndexTTS-VoiceApp-v{#BuildVersion}
OutputBaseFilename=IndexTTSVoiceApp-Setup-{#BuildVersion}
#endif

[Files]
#ifdef InstallerTest
Source: "{#UpgradeTestRoot}\payload\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
Source: "..\release\IndexTTS-VoiceApp-v{#BuildVersion}\IndexTTSVoiceApp\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
#ifndef InstallerTest
Name: "{group}\IndexTTS Voice App"; Filename: "{app}\IndexTTSVoiceApp.exe"
Name: "{autodesktop}\IndexTTS Voice App"; Filename: "{app}\IndexTTSVoiceApp.exe"; Tasks: desktopicon
#endif

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
#ifndef InstallerTest
Filename: "{app}\IndexTTSVoiceApp.exe"; Description: "Start IndexTTS Voice App"; Flags: nowait postinstall skipifsilent; Check: EngineIsPrepared
#endif

[Code]
var
  EnginePrepared: Boolean;

#include "upgrade.iss"

procedure InitializeWizard;
begin
  InitializeUpgradePage;
end;

function EngineIsPrepared: Boolean;
begin
  Result := EnginePrepared;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
#ifdef InstallerTest
    EnginePrepared := True;
#else
    WizardForm.StatusLabel.Caption := 'Preparing Python, CUDA inference libraries and voice models...';
    if not Exec(ExpandConstant('{app}\IndexTTSVoiceApp.exe'), '--install',
      ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('Voice engine preparation could not start. Open Voice App to retry automatically.', mbError, MB_OK);
    end
    else if ResultCode <> 0 then
    begin
      MsgBox('Voice engine preparation is not complete. Downloaded files are retained. Open Voice App to resume installation or repair.', mbInformation, MB_OK);
    end
    else EnginePrepared := True;
#endif
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not EnginePrepared then
    WizardForm.FinishedLabel.Caption := 'The application files are installed, but the voice engine is not ready yet. Open Voice App to resume installation. Downloaded files are retained.';
end;
