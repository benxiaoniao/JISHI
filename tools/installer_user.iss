; 基石 jishi —— **用户程序**的 Windows 安装程序（Inno Setup 6 / 7）
;
; ⚠️ 这**不是**能直接编译的文件：`jishi 打包 --目标 安装包` 会把「α 加名字加 α」
;    形式的占位符（α 是半角的 at 符号）换成真值，写到临时目录（UTF-8 **带 BOM**、CRLF），
;    再把临时目录交给 ISCC。所以下面 `AppId={{…}` 那种写法故意不是合法 Inno ——
;    免得有人拿它直接编出一个名叫「名字占位符」的程序。占位清单见 [Setup] 之前那段。
;
; 为什么要一份单独的模板（而不用 tools/installer.iss）：
;   那一份是**引擎自己**的安装器（bin/ + share/、固定 AppId、固定图标、固定
;   `--version` 完成页）。用户程序的布局是 app/ + bin/，AppId 每个程序都要**不同**
;   （否则装两个程序会互相顶掉卸载项），完成页要跑**用户自己的程序**。两件事的
;   参数几乎没有交集，硬塞进一个文件只会让两边都难改。
;
; 三处「老参数兼容」（存根版自解压安装器时代的东西）都在 [Code] 里，见注释。

; ---------------------------------------------------------------------------
; 占位清单（`jishi 打包` 替换这些；改模板要同步改 `rust/src/packcmd.rs`
; 与 `oracle/jishi/packcmd.py`。占位符写作 α键α，α 是半角的 at 符号）
;
;   APPID          `AppId=` 的值，形如 `{{7C3F8A21-…}`（`{{` 是 Inno 的转义）
;   名字           应用名（= `--名字`）
;   版本           `--版本`
;   默认标题       默认目录里那级目录名（一般同 名字）
;   出目录         `OutputDir`（`--出` 的绝对路径）
;   基名           `OutputBaseFilename`（`名字-版本-windows-x64-setup`）
;   源目录         解包后的发行目录绝对路径
;   快捷目标       `"…\bin\jishi.exe"` 或 `"…\程序.jsh"`（**带引号**）
;   快捷参数行     `Parameters: "…"; ` 或空
;   图标参数       `IconFilename: "…"; ` 或空
;   卸载图标       `UninstallDisplayIcon` 的值
;   图标行         `SetupIconFile=…` 或空
;   消息文件       ChineseSimplified.isl 的**绝对路径**（临时目录里那份）
;   许可行         `LicenseFile=…` 或空（包里带 `LICENSE` 之类才有许可页）
;   运行段         有自带运行时时是 `[Run]` 整段，否则为空
; ---------------------------------------------------------------------------

[Setup]
; 🔴 AppId 由应用名派生（`jishi 打包` 算好填进来），**同一个名字永远同一个 AppId**：
;    升级时才能认出「这是同一个软件」，而不同应用之间互不干扰。
AppId=@APPID@
AppName=@名字@
AppVersion=@版本@
AppVerName=@名字@ @版本@
AppPublisher=基石 jishi
AppPublisherURL=https://github.com/benxiaoniao/JISHI
; 默认装到用户目录：不需要管理员权限（与存根版一致）
DefaultDirName={code:GetDefaultDir}
DefaultGroupName=@名字@
; 第 3 页：选安装目录（`--目标` 也走这里，见 [Code]）
DisableDirPage=no
; 第 1 页：欢迎页。🔴 `WizardStyle=modern` 时 Inno **默认把它关掉**
; （`DisableWelcomePage` 默认 yes），不写这一行就没有欢迎页 —— 实测踩过：
; 双击直接落在「选择目标位置」，用户不知道自己在装什么
DisableWelcomePage=no
; 第 4 页：开始菜单文件夹
DisableProgramGroupPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
OutputDir=@出目录@
OutputBaseFilename=@基名@
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
UninstallDisplayName=@名字@ @版本@
; 第 2 页：许可页（包里带了许可文件才有；没有就跳过这一页）
@许可行@
UninstallDisplayIcon=@卸载图标@
@图标行@
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "@消息文件@"

[Tasks]
; 第 5 页：桌面快捷方式（默认不勾 —— 桌面是用户的地方，别自作主张）
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
; 包里都有什么：`app/`（用户程序）+ `bin/`（自带运行时的引擎与 wasm 素材）+ 清单.json
Source: "@源目录@\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 快捷方式指**引擎**、用户程序当**参数**（M177 的结论：指到 .jsh 要文件关联，
; 装两个程序就互相覆盖；带参数是每个程序独立的）
Name: "{group}\@名字@"; Filename: @快捷目标@; @快捷参数行@@图标参数@
Name: "{group}\卸载 @名字@"; Filename: "{uninstallexe}"
Name: "{autodesktop}\@名字@"; Filename: @快捷目标@; @快捷参数行@@图标参数@Tasks: desktopicon

@运行段@

[Code]
{ ⚠️ 这一段的标识符**一律是 ASCII**（`GetDefaultDir` / `HasSwitch` …）：
  Inno 的 Pascal Script 编译器对非 ASCII 标识符支持不稳，而字符串和目标机无关，
  所以中文只出现在字符串与注释里。

  📌 `--目标` 没放全局变量：它由「code:GetDefaultDir」这个自定义常量每次现取现算 ——
     Inno 什么时候调用那个函数是它说了算，依赖「之前某处已经赋值」迟早出事。
  🔴 注释里**不能**再写花括号：Pascal 的花括号注释遇到第一个右花括号就结束，
     写个自定义常量进去会被截断，剩下半句被当代码 —— 报错还指向下一行的列号。 }

var
  //: 老参数（`/S`、`--目标`）进来时要**跳完整个向导**，等于静默安装。
  SkipWizard: Boolean;

{ 命令行里第 I 个参数 —— 去掉两端引号（Explorer 传过来的会带） }
function ArgAt(Index: Integer): String;
begin
  Result := RemoveQuotes(ParamStr(Index));
end;

{ 命令行里有这个开关吗？（大小写不敏感；`--目标=X` 这种也算）
  📌 逐个比对而不是 `Pos` 整条命令行：路径里有 `/S` 两个字符的概率不为零，
     整条串搜会把 `--目标 C:\S` 误判成静默。 }
function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
  P: String;
begin
  Result := False;
  for I := 1 to ParamCount do
  begin
    P := ArgAt(I);
    if CompareText(P, Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
    if CompareText(Copy(P, 1, Length(Name) + 1), Name + '=') = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

{ 取开关后面那个值：`--目标 X` 与 `--目标=X` 两种写法都认 }
function ValueOf(const Name: String): String;
var
  I: Integer;
  P: String;
begin
  Result := '';
  for I := 1 to ParamCount do
  begin
    P := ArgAt(I);
    if CompareText(P, Name) = 0 then
    begin
      if I < ParamCount then
        Result := ArgAt(I + 1);
      Exit;
    end;
    if CompareText(Copy(P, 1, Length(Name) + 1), Name + '=') = 0 then
    begin
      Result := Copy(P, Length(Name) + 2, Length(P));
      Exit;
    end;
  end;
end;

{ 相对路径按 **setup.exe 所在目录** 展开。
  📌 与存根版（按 cwd 展开）差这一点：双击时 cwd 是安装包所在目录、从
     快捷方式/脚本调时 cwd 是别处 —— 按 exe 走才有稳定预期。 }
function AbsPath(const P: String): String;
begin
  Result := P;
  if Result = '' then
    Exit;
  if (Length(Result) < 2) or (Result[2] <> ':') then
  begin
    if Copy(Result, 1, 2) <> '\\' then
      Result := ExtractFileDir(ExpandConstant('{srcexe}')) + '\' + Result;
  end;
  Result := ExpandFileName(Result);
end;

{ 第 3 页的默认值。三种给法都认（优先级从上到下）：
    ① `--目标 X`（存根版时代的写法，老脚本在用）
    ② `/DIR=X`（Inno 原生写法）
    ③ `%LOCALAPPDATA%\Programs\<名字>`
  🔴 `/DIR=` 必须在这里自己读一遍：Inno 的 `/DIR=` 只是**覆盖默认值**，而本函数的
     返回值会在稍后被求值（自定义常量是延迟求值的），实测会把 `/DIR=` 顶掉 ——
     安装就静默落到默认目录（装完才发现装错地方）。自己读一遍两边都一致，就没这个
     顺序问题。
  🔴 注释里再出现花括号会截断注释（见上面那条）—— 这里连行内代码都别写。 }
function GetDefaultDir(Param: String): String;
var
  T: String;
begin
  T := AbsPath(ValueOf('--目标'));
  if T = '' then
    T := AbsPath(ValueOf('/DIR'));
  if T <> '' then
    Result := T
  else
    Result := ExpandConstant('{localappdata}\Programs\@默认标题@');
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  // ---- 兼容 ①：`--查看` / `--校验` / `--解析快捷方式` ----------------------
  //
  // 这三个是**自解压存根**时代的查错动作：存根要在自己的尾部找「载荷索引」
  // （`[存根][包][偏移][长度][JISHIPK1]`），所以才有「看清单」「校验内容」。
  // Inno 包里**没有载荷索引**（文件由 [Files] 段直接铺开），这两件事无从谈起。
  //
  // 所以这里不装作支持：明确说一句、退出去。要查快捷方式指向，
  // 发行包里带着 `bin\jishi-pack-stub.exe`（它读 .lnk 那段与存根共用）。
  if HasSwitch('--查看') or HasSwitch('--校验') or HasSwitch('--解析快捷方式') then
  begin
    MsgBox('这个安装包是 Inno 向导版，里面没有「载荷清单」可以查看或校验。' + #13#10 + #13#10 +
           '要看安装了什么：双击本安装包走一遍向导即可。' + #13#10 +
           '要查快捷方式指向哪：bin\jishi-pack-stub.exe --解析快捷方式 <文件.lnk>',
           mbInformation, MB_OK);
    Result := False;
    Exit;
  end;

  // ---- 兼容 ②：老脚本传的 `/S`、`--目标`（存根版时代就是**静默**的）------
  //
  // 🔴 Inno **不认识** `/S`（它的静默开关是 `/SILENT` / `/VERYSILENT`），而且
  //    「静默」这件事**没法从脚本里打开**（`WizardSilent` 是只读的）。
  //
  //    先试过「用 /VERYSILENT 重新拉起自己再退掉」：本机实测 `Exec` 直接
  //    返回错误 5（拒绝访问），也就是**不一定能起来** —— 起不来的后果是弹向导
  //    等人点，无人值守的脚本就挂在那儿。不能押这种运气。
  //
  //    改用 Inno 自己的机制：`ShouldSkipPage` 把**每一页**都跳掉。
  //    安装流程（复制文件 / 写注册表 / 快捷方式 / [Run]）照跑，只是没人点。
  //    窗口在 InitializeWizard 里藏起来，看起来就是「运行完就结束」。
  SkipWizard := HasSwitch('/S') or (ValueOf('--目标') <> '');
end;

{ 跳过向导里「要人看 / 要人点」的页 —— 老参数 = 静默安装。

  🔴 **不能连 wpPreparing / wpInstalling 一起跳**：那两页就是**真正的安装过程**，
     跳掉的后果是 `rc=0`、一个文件都没落（实测踩过）—— 而且不报错，最难查。
     所以这里按「页号」排掉它们。 }
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := SkipWizard and (PageID <> wpPreparing) and (PageID <> wpInstalling);
end;

{ 完成页那个「立即运行」要不要出现。
  🔴 必须判：`skipifsilent` 只看 `/SILENT` / `/VERYSILENT`，而我们跳页那种「静默」
     它不认 —— 不判的话 `--目标` 装完会**把用户的程序拉起来**，无人值守的脚本
     就被挂在那儿了（老存根版是装完就退，不跑程序）。 }
function ShouldRun(): Boolean;
begin
  Result := not SkipWizard;
end;

procedure InitializeWizard();
begin
  if SkipWizard then
    WizardForm.Visible := False;
end;
