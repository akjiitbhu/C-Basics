; Inno Setup script: builds EasyTally-Setup.exe from the PyInstaller output in dist\EasyTally
#define AppName "EasyTally"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F1B7C52-4E0A-4D7B-9B7E-3C2A5E8D1F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=EasyTally
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=EasyTally-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: checkedonce

[Files]
Source: "..\dist\EasyTally\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\EasyTally.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\EasyTally.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EasyTally.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; Customer data lives in the user's profile (Documents-independent): %USERPROFILE%\EasyTally.
; It is deliberately NOT removed on uninstall so accounting data is never lost.
