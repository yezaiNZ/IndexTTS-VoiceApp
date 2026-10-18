// Detect only verified installations of this AppId. Never delete directory trees.
const
  UpgradeAppId = '{#UpgradeAppId}';
  UpgradeKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#UpgradeAppId}_is1';

type
  TPreviousInstall = record
    Directory: String;
    Uninstaller: String;
    Version: String;
    PackedVersion: Int64;
    VersionKnown: Boolean;
    MachineInstall: Boolean;
  end;
  TPreviousInstalls = array of TPreviousInstall;
  TSocketAddress = record
    Family: Word;
    Port: Word;
    Address: Cardinal;
    Padding: array[0..7] of Byte;
  end;
  TSocketStartupData = record
    Data: array[0..511] of Byte;
  end;
  // Setup is a 32-bit executable, including on 64-bit Windows.
  TProcessEntry = record
    Size, Usage, ProcessID, DefaultHeapID, ModuleID, Threads, ParentID: Cardinal;
    Priority: Integer;
    Flags: Cardinal;
    ExeFile: array[0..259] of Word;
  end;

var
  PreviousInstalls: TPreviousInstalls;
  DiscoveryDone, UpgradeDone: Boolean;
  UpgradePage: TWizardPage;
  UpgradeConsent: TNewCheckBox;
  UpgradeList: TNewMemo;

function WSAStartup(Version: Word; var Data: TSocketStartupData): Integer;
  external 'WSAStartup@ws2_32.dll stdcall';
function WSACleanup: Integer;
  external 'WSACleanup@ws2_32.dll stdcall';
function WSASocket(Family, Kind, Protocol: Integer): Integer;
  external 'socket@ws2_32.dll stdcall';
function WSABind(Socket: Integer; var Address: TSocketAddress; Size: Integer): Integer;
  external 'bind@ws2_32.dll stdcall';
function WSAClose(Socket: Integer): Integer;
  external 'closesocket@ws2_32.dll stdcall';
function GetDriveType(Path: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';
function FileAttributes(Path: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function ProcessSnapshot(Flags, ProcessID: Cardinal): Integer;
  external 'CreateToolhelp32Snapshot@kernel32.dll stdcall';
function FirstProcess(Snapshot: Integer; var Entry: TProcessEntry): Boolean;
  external 'Process32FirstW@kernel32.dll stdcall';
function NextProcess(Snapshot: Integer; var Entry: TProcessEntry): Boolean;
  external 'Process32NextW@kernel32.dll stdcall';
function CloseProcessSnapshot(Snapshot: Integer): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function VoiceAppIsStopped: Boolean;
var
  Snapshot, I: Integer;
  Entry: TProcessEntry;
  Name: String;
begin
  Result := False;
  Snapshot := ProcessSnapshot(2, 0);
  if Snapshot = -1 then Exit;
  try
    Entry.Size := 556;
    if not FirstProcess(Snapshot, Entry) then Exit;
    repeat
      Name := '';
      for I := 0 to 259 do begin
        if Entry.ExeFile[I] = 0 then Break;
        Name := Name + Chr(Entry.ExeFile[I]);
      end;
      if CompareText(Name, 'IndexTTSVoiceApp.exe') = 0 then Exit;
    until not NextProcess(Snapshot, Entry);
    Result := True;
  finally
    CloseProcessSnapshot(Snapshot);
  end;
end;

function VoicePortIsFree: Boolean;
var
  Data: TSocketStartupData;
  Address: TSocketAddress;
  Socket: Integer;
begin
  Result := False;
  if WSAStartup($0202, Data) <> 0 then Exit;
  try
    Socket := WSASocket(2, 1, 6);
    if Socket = -1 then Exit;
    try
      Address.Family := 2;
      Address.Port := ((7862 mod 256) * 256) + (7862 div 256);
      Address.Address := $0100007F;
      Result := WSABind(Socket, Address, 16) = 0;
    finally
      WSAClose(Socket);
    end;
  finally
    WSACleanup;
  end;
end;

function ParseAppVersion(Value: String; var Packed: Int64): Boolean;
var
  Parts: array[0..3] of Integer;
  I, Dot: Integer;
  Item: String;
begin
  Result := False;
  for I := 0 to 3 do Parts[I] := 0;
  I := 0;
  while (Value <> '') and (I < 4) do begin
    Dot := Pos('.', Value);
    if Dot = 0 then begin Item := Value; Value := ''; end
    else begin Item := Copy(Value, 1, Dot - 1); Delete(Value, 1, Dot); end;
    Parts[I] := StrToIntDef(Item, -1);
    if (Parts[I] < 0) or (Parts[I] > 65535) then Exit;
    I := I + 1;
  end;
  if (I < 3) or (Value <> '') then Exit;
  Packed := PackVersionComponents(Parts[0], Parts[1], Parts[2], Parts[3]);
  Result := True;
end;

function IsSafeProgramDirectory(Directory: String): Boolean;
begin
  Directory := RemoveBackslashUnlessRoot(ExpandFileName(Directory));
  Result := (Length(Directory) > 3) and (Length(ExtractFileDrive(Directory)) = 2) and
    ((not DirExists(Directory)) or ((FileAttributes(Directory) and $400) = 0)) and
    (CompareText(Directory, RemoveBackslashUnlessRoot(GetEnv('USERPROFILE'))) <> 0) and
    (not FileExists(AddBackslash(Directory) + 'pyproject.toml')) and
    (not DirExists(AddBackslash(Directory) + 'indextts')) and
    (not DirExists(AddBackslash(Directory) + 'checkpoints'));
end;

function VerifiedUninstaller(Directory: String): String;
var
  Find: TFindRec;
  ProgramFile, LogFile: String;
  Header: AnsiString;
begin
  Result := '';
  if not IsSafeProgramDirectory(Directory) then Exit;
  if not FileExists(AddBackslash(Directory) + 'IndexTTSVoiceApp.exe') then Exit;
  if FindFirst(AddBackslash(Directory) + 'unins*.exe', Find) then begin
    try
      repeat
        ProgramFile := AddBackslash(Directory) + Find.Name;
        LogFile := ChangeFileExt(ProgramFile, '.dat');
        if FileExists(LogFile) and LoadStringFromFile(LogFile, Header) then begin
          // Fail closed if a future Inno version changes the uninstall header.
          if (Copy(Header, 1, Length('Inno Setup Uninstall Log (b)')) = 'Inno Setup Uninstall Log (b)') and
             (CompareText(String(Copy(Header, 65, Length(UpgradeAppId))), UpgradeAppId) = 0) then begin
            Result := ProgramFile;
            Exit;
          end;
        end;
      until not FindNext(Find);
    finally
      FindClose(Find);
    end;
  end;
end;

function InstalledWheelVersion(Directory: String): String;
var
  Find: TFindRec;
  Version: String;
  Packed, Best: Int64;
begin
  Result := '';
  Best := 0;
  if FindFirst(AddBackslash(Directory) + 'index_tts_voice_app-*-py3-none-any.whl', Find) then begin
    try
      repeat
        Version := Copy(Find.Name, Length('index_tts_voice_app-') + 1,
          Length(Find.Name) - Length('index_tts_voice_app-') - Length('-py3-none-any.whl'));
        if ParseAppVersion(Version, Packed) and (ComparePackedVersion(Packed, Best) >= 0) then begin
          Result := Version;
          Best := Packed;
        end;
      until not FindNext(Find);
    finally
      FindClose(Find);
    end;
  end;
end;

procedure AddPreviousInstall(Directory, Version: String; MachineInstall: Boolean);
var
  I, N: Integer;
  Uninstaller, WheelVersion: String;
  RegistryPacked, WheelPacked: Int64;
begin
  if Directory = '' then Exit;
  Directory := RemoveBackslashUnlessRoot(ExpandFileName(Directory));
  Uninstaller := VerifiedUninstaller(Directory);
  if Uninstaller = '' then Exit;
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do begin
    if CompareText(PreviousInstalls[I].Directory, Directory) = 0 then begin
      // A duplicate per-user entry must not hide a machine-wide install.
      if MachineInstall then PreviousInstalls[I].MachineInstall := True;
      Exit;
    end;
  end;
  WheelVersion := InstalledWheelVersion(Directory);
  if ParseAppVersion(WheelVersion, WheelPacked) then begin
    if not ParseAppVersion(Version, RegistryPacked) then Version := WheelVersion
    else if ComparePackedVersion(WheelPacked, RegistryPacked) > 0 then Version := WheelVersion;
  end;
  N := GetArrayLength(PreviousInstalls);
  SetArrayLength(PreviousInstalls, N + 1);
  PreviousInstalls[N].Directory := Directory;
  PreviousInstalls[N].Uninstaller := Uninstaller;
  PreviousInstalls[N].Version := Version;
  PreviousInstalls[N].VersionKnown := ParseAppVersion(Version, PreviousInstalls[N].PackedVersion);
  PreviousInstalls[N].MachineInstall := MachineInstall;
  Log('Verified previous Voice App: ' + Directory + ' version=' + Version);
end;

procedure ReadRegisteredInstall(Root: Integer; MachineInstall: Boolean);
var
  Directory, Version: String;
begin
  Directory := '';
  Version := '';
  if not RegQueryStringValue(Root, UpgradeKey, 'Inno Setup: App Path', Directory) then
    RegQueryStringValue(Root, UpgradeKey, 'InstallLocation', Directory);
  RegQueryStringValue(Root, UpgradeKey, 'DisplayVersion', Version);
  AddPreviousInstall(Directory, Version, MachineInstall);
end;

procedure ScanInstallParent(Parent: String);
var
  Find: TFindRec;
begin
  if not DirExists(Parent) then Exit;
  if FindFirst(AddBackslash(Parent) + 'IndexTTS*Voice*App*', Find) then begin
    try
      repeat
        // Bounded discovery; portable copies and source trees are never removed.
        if ((Find.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
           ((Find.Attributes and $400) = 0) then
          AddPreviousInstall(AddBackslash(Parent) + Find.Name, '', False);
      until not FindNext(Find);
    finally
      FindClose(Find);
    end;
  end;
end;

procedure DiscoverPreviousInstalls;
var
  I, Drive: Integer;
  Root: String;
begin
  if DiscoveryDone then Exit;
  DiscoveryDone := True;
#ifdef InstallerTest
  ScanInstallParent('{#UpgradeTestRoot}');
  ReadRegisteredInstall(HKCU32, False);
#else
  ReadRegisteredInstall(HKCU32, False);
  ReadRegisteredInstall(HKLM32, True);
  if IsWin64 then begin
    ReadRegisteredInstall(HKCU64, False);
    ReadRegisteredInstall(HKLM64, True);
  end;
  ScanInstallParent(ExpandConstant('{localappdata}\Programs'));
  for Drive := Ord('C') to Ord('Z') do begin
    Root := Chr(Drive) + ':\';
    if GetDriveType(Root) = 3 then ScanInstallParent(Root);
  end;
#endif
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do
    ScanInstallParent(ExtractFileDir(PreviousInstalls[I].Directory));
end;

function DefaultInstallDirectory(Param: String): String;
var
  I, Best: Integer;
begin
  DiscoverPreviousInstalls;
  Best := -1;
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do begin
    if Best = -1 then Best := I
    else if PreviousInstalls[I].VersionKnown and
      (ComparePackedVersion(PreviousInstalls[I].PackedVersion, PreviousInstalls[Best].PackedVersion) > 0) then Best := I;
  end;
  if Best <> -1 then Result := AddBackslash(ExtractFileDir(PreviousInstalls[Best].Directory)) + 'IndexTTS Voice App v{#BuildVersion}'
  else Result := ExpandConstant('{localappdata}\Programs\IndexTTS Voice App v{#BuildVersion}');
end;

function PreviousInstallSummary: String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do
    Result := Result + PreviousInstalls[I].Version + '  ' + PreviousInstalls[I].Directory + #13#10;
end;

procedure InitializeUpgradePage;
begin
  DiscoverPreviousInstalls;
  UpgradePage := CreateCustomPage(wpWelcome, '发现已安装的 Voice App', '确认升级：只替换程序，保留语音引擎、模型和个人配置。');
  UpgradeList := TNewMemo.Create(UpgradePage);
  UpgradeList.Parent := UpgradePage.Surface;
  UpgradeList.SetBounds(0, 0, UpgradePage.SurfaceWidth, ScaleY(145));
  UpgradeList.ReadOnly := True;
  UpgradeList.ScrollBars := ssVertical;
  UpgradeList.Text := PreviousInstallSummary + #13#10 +
    '模型、角色、配置和生成音频会保留。请先关闭正在运行的 Voice App。';
  UpgradeConsent := TNewCheckBox.Create(UpgradePage);
  UpgradeConsent.Parent := UpgradePage.Surface;
  UpgradeConsent.SetBounds(0, ScaleY(160), UpgradePage.SurfaceWidth, ScaleY(64));
  UpgradeConsent.Caption := '我同意卸载以上旧版程序，再安装 v{#BuildVersion}。';
  UpgradeConsent.Checked := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (UpgradePage <> nil) and (PageID = UpgradePage.ID) and
    (GetArrayLength(PreviousInstalls) = 0);
end;

function NextButtonClick(PageID: Integer): Boolean;
begin
  Result := True;
  if (UpgradePage <> nil) and (PageID = UpgradePage.ID) and
     (GetArrayLength(PreviousInstalls) > 0) and (not WizardSilent) and (not UpgradeConsent.Checked) then begin
    MsgBox('请勾选确认升级，或取消安装。旧版文件尚未改动。', mbInformation, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I, Code, WaitCount: Integer;
  Current: Int64;
  Parameters: String;
begin
  Result := '';
  if UpgradeDone then Exit;
  if not IsSafeProgramDirectory(WizardDirValue) then begin
    Result := '请使用专门的程序子目录，不能选择磁盘根目录、用户目录、源码或引擎目录。';
    Exit;
  end;
  if FileExists(AddBackslash(WizardDirValue) + 'IndexTTSVoiceApp.exe') and
    (VerifiedUninstaller(WizardDirValue) = '') then begin
    Result := '目标目录已有无法验证的程序或便携版，请选择一个新的程序目录；现有文件未改动。';
    Exit;
  end;
  if GetArrayLength(PreviousInstalls) > 0 then begin
    if WizardSilent then begin
      if ExpandConstant('{param:UPGRADE|0}') <> '1' then begin
        Result := '发现旧版。静默升级必须明确指定 /UPGRADE=1；旧版未改动。';
        Exit;
      end;
    end else if not UpgradeConsent.Checked then begin
      Result := '尚未确认升级，旧版未改动。'; Exit;
    end;
  end;
  ParseAppVersion('{#BuildVersion}', Current);
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do begin
    if PreviousInstalls[I].VersionKnown and
      (ComparePackedVersion(PreviousInstalls[I].PackedVersion, Current) > 0) then begin
      Result := '发现比本安装包更新的版本，已阻止降级。旧版未改动。'; Exit;
    end;
    if VerifiedUninstaller(PreviousInstalls[I].Directory) <> PreviousInstalls[I].Uninstaller then begin
      Result := '旧版卸载信息发生变化，请重新启动安装器；未执行清理。'; Exit;
    end;
    if (CompareText(AddBackslash(WizardDirValue), AddBackslash(PreviousInstalls[I].Directory)) <> 0) and
      (Pos(Lowercase(AddBackslash(PreviousInstalls[I].Directory)), Lowercase(AddBackslash(WizardDirValue))) = 1) then begin
      Result := '新版本不能安装到旧版程序目录的内部，请选择相邻的新目录。'; Exit;
    end;
  end;
  if not VoicePortIsFree then begin
    Result := '本地端口 7862 正在使用。请先关闭 Voice App 或独立接口服务，再重试；不会自动结束其他程序。'; Exit;
  end;
  if not VoiceAppIsStopped then begin
    Result := 'Voice App 界面仍在运行。请先退出（包括正在准备引擎的窗口），再点击重试。旧版程序尚未卸载。'; Exit;
  end;
  for I := 0 to GetArrayLength(PreviousInstalls) - 1 do begin
    WizardForm.StatusLabel.Caption := '正在卸载旧版程序（保留模型和个人数据）…';
    Parameters := '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';
    if PreviousInstalls[I].MachineInstall then begin
      if not ShellExec('runas', PreviousInstalls[I].Uninstaller, Parameters, PreviousInstalls[I].Directory, SW_SHOWNORMAL, ewWaitUntilTerminated, Code) then Code := -1;
    end else begin
      if not Exec(PreviousInstalls[I].Uninstaller, Parameters, PreviousInstalls[I].Directory, SW_HIDE, ewWaitUntilTerminated, Code) then Code := -1;
    end;
    // Inno's original uninstaller can exit just before its temporary clone
    // finishes deleting files. Wait briefly without racing the new file copy.
    WaitCount := 0;
    while (Code = 0) and (WaitCount < 100) and
      (FileExists(PreviousInstalls[I].Uninstaller) or
       FileExists(AddBackslash(PreviousInstalls[I].Directory) + 'IndexTTSVoiceApp.exe')) do begin
      Sleep(100);
      WaitCount := WaitCount + 1;
    end;
    if (Code <> 0) or FileExists(PreviousInstalls[I].Uninstaller) or
      FileExists(AddBackslash(PreviousInstalls[I].Directory) + 'IndexTTSVoiceApp.exe') then begin
      Result := '旧版卸载未完成，请关闭旧程序后重试；如 Windows 提示重启，请先重启。保留的模型和个人数据不受影响。'; Exit;
    end;
  end;
  UpgradeDone := True;
end;

function InitializeSetup: Boolean;
var
  Path: String;
  Lines: TArrayOfString;
begin
  DiscoverPreviousInstalls;
  Path := ExpandConstant('{param:UPGRADEPLAN|}');
  if Path <> '' then begin
    // Read-only planning mode used for diagnostics. No uninstall or installation.
    if not FileExists(Path) then begin
      SetArrayLength(Lines, 3);
      Lines[0] := 'count=' + IntToStr(GetArrayLength(PreviousInstalls));
      Lines[1] := 'default=' + DefaultInstallDirectory('');
      Lines[2] := PreviousInstallSummary;
      SaveStringsToUTF8File(Path, Lines, False);
    end;
    Result := False;
  end else Result := True;
end;
