#define MyAppName "Luma Fetch"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "Luma Fetch"
#define MyAppExeName "LumaFetch.exe"

[Setup]
AppId={{E22E3193-0A15-459D-8CBF-AE861EE8C7F0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
SetupIconFile=LumaFetch.ico
VersionInfoVersion=1.3.0.0
VersionInfoProductVersion=1.3.0.0
DefaultDirName={localappdata}\Programs\LumaFetch
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\outputs
OutputBaseFilename=LumaFetch-Setup-1.3.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로가기 만들기"; GroupDescription: "추가 바로가기:"

[Files]
Source: "LumaFetch.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0; Tasks: desktopicon

