; Inno Setup script for Chess Player. Normally compiled by build.ps1, which
; passes /DAppVersion=<version from chess_player/__init__.py>.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Chess Player"
#define AppExe  "ChessPlayer.exe"

[Setup]
; Fixed AppId => every new version upgrades the existing install in place.
; Never change this GUID.
AppId={{6E0C2F4B-8D1A-4C7E-9B35-2A9F7D61C3E8}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\ChessPlayer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install: no admin prompt, installs to %LOCALAPPDATA%\Programs\ChessPlayer
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=ChessPlayerSetup-{#AppVersion}
SetupIconFile=..\chess_player\assets\chess_player.ico
UninstallDisplayIcon={app}\{#AppExe}
; lzma2/fast: much faster to build than lzma2/max, for a slightly larger installer
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
; Close a running Chess Player before upgrading
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[InstallDelete]
; Wipe old program files so stale libraries from earlier versions don't linger
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Engines are deliberately NOT packaged — pick them in the app (Browse / history).
Source: "..\dist\ChessPlayer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
