#define MyAppName "Luma Fetch"
#define MyAppVersion "1.9.3"
#define MyAppPublisher "Luma Fetch"
#define MyAppExeName "LumaFetch.exe"

[Setup]
AppId={{E22E3193-0A15-459D-8CBF-AE861EE8C7F0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
SetupIconFile=LumaFetch.ico
VersionInfoVersion=1.9.3.0
VersionInfoProductVersion=1.9.3.0
DefaultDirName={localappdata}\Programs\LumaFetch
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\outputs
OutputBaseFilename=LumaFetch-Setup-1.9.3
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

[Files]
Source: "..\work\dist\LumaFetch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0; Tasks: desktopicon


[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { Restart Manager sends the normal close request first.  This is a fallback
    for a frozen PyInstaller parent/child process that no longer has a window. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1200);
  Result := '';
end;

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Luma Fetch 실행"; Flags: nowait postinstall skipifsilent
