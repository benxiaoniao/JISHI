; 基石 jishi —— Windows 安装程序（Inno Setup 6）
;
; 编译（由 tools/build_release.py 调用，也可手动）：
;   iscc /DSourceDir=<发行目录> /DAppVersion=<版本> /DOutputDir=<输出目录> \
;        tools\installer.iss
;
; 设计：
; - 用户级安装（PrivilegesRequired=lowest），默认装到 %LOCALAPPDATA%\Programs\jishi，
;   不需要管理员权限；
; - 简体中文向导界面（语言文件随仓库携带，见 tools/inno/ChineseSimplified.isl）；
; - 可选把 {app}\bin\jishi 追加到用户 PATH；卸载时自动移除；
; - 提供开始菜单与可选桌面快捷方式，注册标准卸载程序。

#ifndef SourceDir
  #define SourceDir "..\dist\release\jishi-0.1.0-windows-x64"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\release"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define MyAppName "基石 jishi"
#define MyAppPublisher "benxiaoniao"
#define MyAppURL "https://github.com/benxiaoniao/JISHI"
#define MyAppExeName "jishi.exe"

[Setup]
; AppId 一旦发布不可更改（决定升级/卸载的识别）
AppId={{7C3F8A21-4B5D-4E9A-9F2C-1D6E8B3A5C74}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\jishi
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=jishi-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
UninstallDisplayName={#MyAppName} {#AppVersion}
UninstallDisplayIcon={app}\bin\jishi\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
InfoBeforeFile=
LicenseFile=

[Languages]
Name: "chinese"; MessagesFile: "inno\ChineseSimplified.isl"

[Tasks]
Name: "addtopath"; Description: "把 jishi 加入 PATH（推荐，可在任意终端直接运行）"; GroupDescription: "环境配置："; Flags: checkedonce
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
; 打包整个发行目录（bin/、share/、README 等）
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\基石 jishi"; Filename: "{app}\bin\jishi\{#MyAppExeName}"; Comment: "基石中文编程语言"
Name: "{group}\卸载 基石 jishi"; Filename: "{uninstallexe}"
Name: "{autodesktop}\基石 jishi"; Filename: "{app}\bin\jishi\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; 用户 PATH 追加（仅当勾选 addtopath 且尚未包含时）
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}\bin\jishi"; Tasks: addtopath; Check: NeedsAddPath('{app}\bin\jishi')

[Run]
Filename: "{app}\bin\jishi\{#MyAppExeName}"; Parameters: "--version"; \
    Description: "查看版本，确认安装成功"; Flags: postinstall nowait skipifsilent

[Code]
const
  EnvKey = 'Environment';

{ 用户 PATH 是否已包含该目录（不区分大小写、按 ; 分段比较） }
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', OrigPath) then
  begin
    Result := True;
    Exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';',
                ';' + Uppercase(OrigPath) + ';') = 0;
end;

{ 从用户 PATH 中移除一段（卸载时调用） }
procedure RemoveFromPath(PathToRemove: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths) then
    Exit;
  P := Pos(';' + Uppercase(PathToRemove) + ';',
           ';' + Uppercase(Paths) + ';');
  if P = 0 then
    Exit;
  Delete(Paths, P, Length(PathToRemove) + 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Paths);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}\bin\jishi'));
end;
