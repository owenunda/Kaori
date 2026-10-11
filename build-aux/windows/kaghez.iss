; Inno Setup script for the Windows installer. Build the app with
; build.sh first, then run: ISCC.exe build-aux\windows\kaghez.iss
; Paths are relative to this file. Set KAGHEZ_VERSION to stamp a version.

#define AppVersion GetEnv("KAGHEZ_VERSION")
#if AppVersion == ""
  #define AppVersion "0.9.0"
#endif
#define BuildDir "..\..\_build-windows"

[Setup]
AppId={{6B0E3C8B-5E2B-4F4B-9C7E-2A1D9E4B7C31}
AppName=Kaori
AppVersion={#AppVersion}
AppPublisher=Kaori
AppPublisherURL=https://github.com/riyaniii/Kaori
DefaultDirName={autopf}\Kaori
DefaultGroupName=Kaori
DisableProgramGroupPage=yes
LicenseFile=..\..\COPYING
OutputDir={#BuildDir}\installer
OutputBaseFilename=Kaori-{#AppVersion}-setup-x64
SetupIconFile={#BuildDir}\kaghez.ico
UninstallDisplayIcon={app}\Kaori.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install by default (no admin prompt); users can choose all-users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
; Kaori.exe keeps the bundled java.exe alive; make sure both are closed
; before files are replaced.
CloseApplications=force

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Drop the previous version's bundle so old jars/DLLs don't pile up.
Type: filesandordirs; Name: "{app}\_internal"
; Leftovers from before the app was renamed from Kaghez. Same AppId, so
; upgrades land in the old folder.
Type: files; Name: "{app}\Kaghez.exe"
Type: files; Name: "{autoprograms}\Kaghez.lnk"
Type: files; Name: "{autodesktop}\Kaghez.lnk"

[Files]
Source: "{#BuildDir}\dist\Kaori\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Kaori"; Filename: "{app}\Kaori.exe"
Name: "{autodesktop}\Kaori"; Filename: "{app}\Kaori.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Kaori.exe"; Description: "{cm:LaunchProgram,Kaori}"; Flags: nowait postinstall skipifsilent
