#define MyAppName "Luma Fetch"
#define MyAppVersion "1.12.0"
#define MyAppPublisher "Luma Fetch"
#define MyAppExeName "LumaFetch.exe"
#define VCRedistURL "https://download.visualstudio.microsoft.com/download/pr/ebdab8e5-1d7b-4d9f-a11b-cbb1720c3b12/843068991DAAA1F73AD9F6239BCE4D0F6A07A51F18C37EA2A867E9BECA71295C/VC_redist.x64.exe"
#define VCRedistSHA256 "843068991DAAA1F73AD9F6239BCE4D0F6A07A51F18C37EA2A867E9BECA71295C"

[Setup]
AppId={{E22E3193-0A15-459D-8CBF-AE861EE8C7F0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
SetupIconFile=LumaFetch.ico
VersionInfoVersion=1.12.0.0
VersionInfoProductVersion=1.12.0.0
DefaultDirName={localappdata}\Programs\LumaFetch
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\outputs
OutputBaseFilename=LumaFetch-Setup-1.12.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로가기 만들기"; GroupDescription: "추가 바로가기:"
Name: "vcredist"; Description: "Microsoft Visual C++ 호환성 런타임 자동 설치 (없는 경우 권장)"; GroupDescription: "선행 조건:"; Flags: checkedonce; Check: IsVCRuntimeMissing

[Files]
Source: "..\work\dist\LumaFetch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0; Tasks: desktopicon

[Code]
var
  PrerequisiteDownloadPage: TDownloadWizardPage;

function HasCommandLineSwitch(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), Value) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function IsVCRuntimeInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result :=
    RegQueryDWordValue(
      HKLM64,
      'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
      'Installed', Installed
    ) and (Installed = 1);
end;

function IsVCRuntimeMissing: Boolean;
begin
  Result := not IsVCRuntimeInstalled;
end;

function ShouldAutoRestartApp: Boolean;
begin
  Result := HasCommandLineSwitch('/AUTORESTARTAPP');
end;

function InitializeSetup: Boolean;
begin
  Result := IsWin64;
  if not Result then
    MsgBox('Luma Fetch는 64비트 Windows 10 또는 Windows 11이 필요합니다.', mbError, MB_OK);
end;

procedure InitializeWizard;
begin
  PrerequisiteDownloadPage := CreateDownloadPage(
    '필수 구성 요소 다운로드',
    'Microsoft 공식 서버에서 필요한 런타임을 안전하게 다운로드합니다.',
    nil
  );
  PrerequisiteDownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function DownloadVCRuntime: Boolean;
begin
  Result := False;
  try
    if WizardSilent then
    begin
      DownloadTemporaryFile(
        '{#VCRedistURL}', 'vc_redist.x64.exe', '{#VCRedistSHA256}', nil
      );
    end
    else
    begin
      PrerequisiteDownloadPage.Clear;
      PrerequisiteDownloadPage.Add(
        '{#VCRedistURL}', 'vc_redist.x64.exe', '{#VCRedistSHA256}'
      );
      PrerequisiteDownloadPage.Show;
      try
        PrerequisiteDownloadPage.Download;
      finally
        PrerequisiteDownloadPage.Hide;
      end;
    end;
    Result := True;
  except
    Log('VC++ runtime download failed: ' + GetExceptionMessage);
  end;
end;

function InstallPrerequisites(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  RedistPath: String;
begin
  Result := '';
  if IsVCRuntimeInstalled or not WizardIsTaskSelected('vcredist') then
    Exit;

  if not DownloadVCRuntime then
  begin
    Result := 'Microsoft Visual C++ 런타임을 안전하게 다운로드하지 못했습니다. HTTPS 연결과 SHA-256 검증을 확인하세요.';
    Exit;
  end;

  RedistPath := ExpandConstant('{tmp}\vc_redist.x64.exe');
  if not ShellExec(
    'runas', RedistPath, '/install /quiet /norestart', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) then
  begin
    Result := 'Microsoft Visual C++ 런타임 설치 권한을 승인하지 않았거나 설치를 시작하지 못했습니다.';
    Exit;
  end;

  if ResultCode = 3010 then
    NeedsRestart := True
  else if (ResultCode <> 0) and (ResultCode <> 1638) then
  begin
    Result := Format('Microsoft Visual C++ 런타임 설치가 실패했습니다. 종료 코드: %d', [ResultCode]);
    Exit;
  end;

  if not IsVCRuntimeInstalled then
    Result := 'Microsoft Visual C++ 런타임 설치 결과를 확인할 수 없습니다.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := InstallPrerequisites(NeedsRestart);
  if Result <> '' then
    Exit;

  { Restart Manager requests a normal close first. This fallback only targets
    LumaFetch processes and never terminates the detached installer tree. }
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /IM "{#MyAppExeName}"', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode
  );
  Sleep(1200);
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Luma Fetch 실행"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Parameters: "--update-complete"; Flags: nowait; Check: ShouldAutoRestartApp