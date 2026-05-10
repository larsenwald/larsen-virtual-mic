#define MyAppName "Larsenwald Virtual Mic"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Larsenwald"
#define MyAppExeName "LarsenwaldVM.exe"
#define MyAppID "{{A7F3C2D1-4B8E-4F9A-B2C3-D4E5F6A7B8C9}"

[Setup]
AppId={#MyAppID}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\LarsenwaldVM
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=Install
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Main app exe — index.html bundled inside
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; Driver folder — installed alongside exe so app and uninstaller can find it
Source: "driver\*"; DestDir: "{app}\driver"; Flags: ignoreversion recursesubdirs createallsubdirs
; PowerShell scripts
Source: "scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start menu shortcut
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; Start menu uninstall shortcut
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Optional desktop shortcut
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Register elevated scheduled task at logon — no UAC prompt on subsequent launches
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -Command ""$action = New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}' -Argument '--minimized'; $trigger = New-ScheduledTaskTrigger -AtLogOn; $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive; $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0; Register-ScheduledTask -TaskName 'LarsenwaldVMStartup' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null"""; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "Registering startup task..."

; Create AppData folder for config
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -Command ""New-Item -ItemType Directory -Force -Path '$env:APPDATA\LarsenwaldVM' | Out-Null"""; \
  Flags: runhidden waituntilterminated; \
  StatusMsg: "Creating app data folder..."

; Optionally launch the app after install
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
; Remove scheduled task on uninstall
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -Command ""Unregister-ScheduledTask -TaskName 'LarsenwaldVMStartup' -Confirm:$false -ErrorAction SilentlyContinue"""; \
  Flags: runhidden waituntilterminated; \
  RunOnceId: "RemoveStartupTask"

; Kill app if running before uninstall
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -Command ""Get-Process -Name 'LarsenwaldVM' -ErrorAction SilentlyContinue | Stop-Process -Force"""; \
  Flags: runhidden waituntilterminated; \
  RunOnceId: "KillApp"

; Uninstall VB-Audio driver
Filename: "{app}\driver\VBCABLE_Setup_x64.exe"; \
  Parameters: "-u -h"; \
  Flags: runhidden waituntilterminated; \
  RunOnceId: "UninstallDriver"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{userappdata}\LarsenwaldVM"
