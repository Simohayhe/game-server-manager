; Game Server Manager インストーラ定義(Inno Setup)。
; ビルド: ISCC.exe /DMyAppVersion=3.1.0 installer.iss
;   本体(GameServerManager.exe)を Program Files\GameServerManager へインストール、
;   デスクトップ+スタートメニューにショートカット、アンインストーラ付き。
;   ユーザーデータ(config/状態)は %LOCALAPPDATA%\GameServerManager に置かれる(アプリ側で管理)。

#define MyAppName "Game Server Manager"
#define MyAppExeName "GameServerManager.exe"
#define MyAppPublisher "Simohayhe"
#define MyAppURL "https://github.com/Simohayhe/game-server-manager"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
; AppId は全バージョン共通(=上書きアップグレードされる)。変更しないこと。
AppId={{6F9A2B84-3C1D-4E77-9A2E-5B8D1C0F4A21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\GameServerManager
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Program Files へ入れるので管理者権限(UAC)が必要
PrivilegesRequired=admin
OutputDir=installer_out
OutputBaseFilename=GameServerManager-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "dist\GameServerManager.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// インストール前に、稼働中のGSM(GUI+サービス)を止める(ファイルロック回避)。
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/IM GameServerManager.exe /F', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

// アンインストール前にも同様に止める。
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/IM GameServerManager.exe /F', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Result := True;
end;
