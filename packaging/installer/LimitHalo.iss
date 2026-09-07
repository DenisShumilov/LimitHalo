#ifndef SourceDir
  #error SourceDir must be supplied by the build script
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by the build script
#endif
#ifndef CandidateId
  #error CandidateId must be supplied by the build script
#endif
#ifndef ProductVersion
  #error ProductVersion must be supplied by the build script
#endif
#ifndef PreflightValidatorPath
  #error PreflightValidatorPath must be supplied by the build script
#endif

#define ProductName "LimitHalo"
#define InternalProductId "AILimitsWidget"
#define ProductPublisher "LimitHalo contributors"
#define ProductExe "AILimitsWidget.exe"
#define AppIdValue "{{A858F5C8-BBD1-4B0F-B5BD-FB812A5EEA73}"

[Setup]
AppId={#AppIdValue}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppVerName={#ProductName} {#ProductVersion}
AppPublisher={#ProductPublisher}
DefaultDirName={localappdata}\Programs\{#InternalProductId}
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=LimitHalo-{#ProductVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousTasks=no
UsePreviousLanguage=no
LanguageDetectionMethod=none
ShowLanguageDialog=yes
Uninstallable=yes
UninstallDisplayName={#ProductName} {#ProductVersion}
UninstallDisplayIcon={app}\versions\{#CandidateId}\{#ProductExe}
AppMutex=Local\AILimitsWidget-HUD-v1
CloseApplications=yes
RestartApplications=no
ChangesAssociations=no
ChangesEnvironment=no
MinVersion=10.0
VersionInfoVersion={#ProductVersion}.0
VersionInfoProductVersion={#ProductVersion}.0
VersionInfoProductName={#ProductName}
VersionInfoCompany={#ProductPublisher}
VersionInfoDescription={#ProductName} per-user setup (unsigned community candidate)
VersionInfoCopyright=Copyright (c) 2026 LimitHalo contributors
SignedUninstaller=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Dirs]
Name: "{app}\versions\{#CandidateId}"

[Files]
Source: "{#SourceDir}\package-manifest.json"; Flags: dontcopy notimestamp; DestName: "LimitHaloPreflightManifest.json"; Check: IsSafeCandidateId('{#CandidateId}')
Source: "{#PreflightValidatorPath}"; Flags: dontcopy notimestamp; DestName: "LimitHaloPreflightValidator.ps1"; Check: IsSafeCandidateId('{#CandidateId}')
Source: "{#SourceDir}\*"; DestDir: "{app}\versions\{#CandidateId}"; Flags: ignoreversion recursesubdirs createallsubdirs notimestamp; Excludes: "package-manifest.json"; Check: IsSafeCandidateId('{#CandidateId}')
Source: "{#SourceDir}\package-manifest.json"; DestDir: "{app}\versions\{#CandidateId}"; Flags: ignoreversion notimestamp; Check: IsSafeCandidateId('{#CandidateId}')

[Run]
Filename: "{app}\versions\{#CandidateId}\{#ProductExe}"; Parameters: "--grant-claude-quota-access"; WorkingDir: "{app}\versions\{#CandidateId}"; Flags: runhidden waituntilterminated; Check: ShouldApplyClaudeConsent
Filename: "{app}\versions\{#CandidateId}\{#ProductExe}"; WorkingDir: "{app}\versions\{#CandidateId}"; Description: "Launch {#ProductName}"; Flags: nowait postinstall; Check: ShouldOfferHudLaunch; BeforeInstall: PrepareHudPostInstallLaunch; AfterInstall: ClearPostInstallLaunchSignal
Filename: "{app}\versions\{#CandidateId}\{#ProductExe}"; Parameters: "--configure"; WorkingDir: "{app}\versions\{#CandidateId}"; Description: "Configure {#ProductName}"; Flags: nowait postinstall; Check: ShouldOfferConfigureLaunch; BeforeInstall: PrepareConfigurePostInstallLaunch

[Code]
const
  REPARSE_POINT_ATTRIBUTE = $400;

var
  RemoveSettingsChoice: Boolean;
  PreviousCandidate: string;
  ExistingConfig: Boolean;
  PreviousStartupPresent: Boolean;
  CandidateHealthPassed: Boolean;
  ClaudePage: TInputOptionWizardPage;
  ClaudeAccessPage: TInputOptionWizardPage;
  CodexPage: TInputOptionWizardPage;
  PreferencesPage: TInputOptionWizardPage;
  AlertsPage: TInputOptionWizardPage;
  ThresholdPage: TInputOptionWizardPage;
  QuietPage: TInputOptionWizardPage;
  QuietTimesPage: TInputQueryWizardPage;
  ReviewPage: TOutputMsgMemoWizardPage;
  ConfirmPage: TInputOptionWizardPage;

function GetFileAttributes(lpFileName: string): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';

function SetEnvironmentVariable(lpName: string; lpValue: string): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function L(EnglishText: string; UkrainianText: string): string;
begin
  if ActiveLanguage = 'ukrainian' then
    Result := UkrainianText
  else
    Result := EnglishText;
end;

function IsTrueParameter(Value: string; DefaultValue: Boolean): Boolean;
begin
  if Value = '1' then Result := True
  else if Value = '0' then Result := False
  else Result := DefaultValue;
end;

function ProviderIndex(Value: string): Integer;
begin
  if Value = 'enabled' then Result := 1
  else if Value = 'disabled' then Result := 2
  else Result := 0;
end;

function ThresholdIndex(Value: string): Integer;
begin
  if Value = '10' then Result := 1
  else if Value = '5' then Result := 2
  else Result := 0;
end;

function IsBoundedLocalTime(Value: string): Boolean;
var
  HourValue: Integer;
  MinuteValue: Integer;
begin
  Result := False;
  if Length(Value) <> 5 then Exit;
  if Value[3] <> ':' then Exit;
  if (Value[1] < '0') or (Value[1] > '2') or
     (Value[2] < '0') or (Value[2] > '9') or
     (Value[4] < '0') or (Value[4] > '5') or
     (Value[5] < '0') or (Value[5] > '9') then Exit;
  HourValue := StrToInt(Copy(Value, 1, 2));
  MinuteValue := StrToInt(Copy(Value, 4, 2));
  Result := (HourValue >= 0) and (HourValue <= 23) and
    (MinuteValue >= 0) and (MinuteValue <= 59);
end;

function ProviderValue(Page: TInputOptionWizardPage): string;
begin
  if Page.SelectedValueIndex = 1 then Result := 'enabled'
  else if Page.SelectedValueIndex = 2 then Result := 'disabled'
  else Result := 'auto';
end;

function ThresholdValue(): Integer;
begin
  if ThresholdPage.SelectedValueIndex = 1 then Result := 10
  else if ThresholdPage.SelectedValueIndex = 2 then Result := 5
  else Result := 20;
end;

function BoolJson(Value: Boolean): string;
begin
  if Value then Result := 'true' else Result := 'false';
end;

function SelectedLocale(): string;
begin
  if ActiveLanguage = 'ukrainian' then Result := 'uk-UA' else Result := 'en-US';
end;

function AlertsEnabled(): Boolean;
begin
  Result := AlertsPage.SelectedValueIndex = 1;
end;

function ClaudeAccessGranted(): Boolean;
begin
  Result := (ClaudePage.SelectedValueIndex <> 2) and ClaudeAccessPage.Values[0];
end;

function ClaudeConsentValue(): string;
begin
  if ClaudeAccessGranted() then Result := 'granted'
  else Result := 'not-asked';
end;

function ClaudeProviderValue(): string;
begin
  if ClaudeAccessGranted() then Result := 'enabled'
  else Result := ProviderValue(ClaudePage);
end;

function BothProvidersDisabled(): Boolean;
begin
  Result := (ClaudePage.SelectedValueIndex = 2) and
    (CodexPage.SelectedValueIndex = 2);
end;

function ExplicitProviderVisible(): Boolean;
begin
  Result := (ClaudePage.SelectedValueIndex = 1) or
    (CodexPage.SelectedValueIndex = 1);
end;

function StartWithWindowsSelected(): Boolean;
begin
  Result := PreferencesPage.Values[0];
end;

function StatusColorsSelected(): Boolean;
begin
  Result := PreferencesPage.Values[1];
end;

function ColorValue(): string;
begin
  if StatusColorsSelected() then Result := 'status' else Result := 'monochrome';
end;

function ConfigPath(): string;
begin
  Result := ExpandConstant('{localappdata}\{#InternalProductId}\config.json');
end;

function StartupShortcutPath(): string;
begin
  Result := ExpandConstant('{userstartup}\{#ProductName}.lnk');
end;

procedure InitializeWizard();
var
  DefaultClaude: string;
  DefaultCodex: string;
  DefaultColors: string;
  DefaultAlert: string;
  DefaultStart: string;
  DefaultEnd: string;
begin
  ExistingConfig := FileExists(ConfigPath());
  PreviousStartupPresent := FileExists(StartupShortcutPath());
  CandidateHealthPassed := False;

  DefaultClaude := ExpandConstant('{param:PLANCLAUDE|auto}');
  DefaultCodex := ExpandConstant('{param:PLANCODEX|auto}');
  DefaultColors := ExpandConstant('{param:PLANCOLORS|monochrome}');
  DefaultAlert := ExpandConstant('{param:PLANALERTS|0}');
  DefaultStart := ExpandConstant('{param:PLANQUIETSTART|22:00}');
  DefaultEnd := ExpandConstant('{param:PLANQUIETEND|07:00}');
  if not IsBoundedLocalTime(DefaultStart) then DefaultStart := '22:00';
  if not IsBoundedLocalTime(DefaultEnd) or (DefaultEnd = DefaultStart) then DefaultEnd := '07:00';

  ClaudePage := CreateInputOptionPage(wpSelectDir,
    L('Claude', 'Claude'),
    L('Choose how Claude should appear.', 'Оберіть, як відображати Claude.'),
    L('Auto detects a trusted client; Use keeps a setup/status lane; Skip removes Claude completely.',
      'Авто шукає довірений клієнт; Використовувати залишає смугу налаштування/стану; Пропустити повністю прибирає Claude.'),
    True, False);
  ClaudePage.Add(L('Auto (recommended)', 'Авто (рекомендовано)'));
  ClaudePage.Add(L('Use Claude', 'Використовувати Claude'));
  ClaudePage.Add(L('Skip Claude', 'Пропустити Claude'));
  ClaudePage.SelectedValueIndex := ProviderIndex(DefaultClaude);

  ClaudeAccessPage := CreateInputOptionPage(ClaudePage.ID,
    L('Claude quota access', 'Доступ до квоти Claude'),
    L('Connect Claude on the first launch.', 'Підключіть Claude під час першого запуску.'),
    L('LimitHalo reads only claudeAiOauth in %USERPROFILE%/.claude/.credentials.json. It contacts api.anthropic.com/api/oauth/usage for limits and platform.claude.com/v1/oauth/token only when token refresh is required; then only that OAuth section is atomically replaced while unrelated bytes are preserved. Chats, prompts, browser data, and logs are never read, and no telemetry is sent.',
      'LimitHalo читає лише розділ claudeAiOauth у %USERPROFILE%/.claude/.credentials.json. Для лімітів програма звертається до api.anthropic.com/api/oauth/usage, а до platform.claude.com/v1/oauth/token — лише коли треба оновити токен; тоді атомарно замінюється лише OAuth-розділ, а всі інші байти зберігаються. Чати, запити, дані браузера й журнали не читаються, телеметрія не надсилається.'),
    False, False);
  ClaudeAccessPage.Add(L('I allow quota-only Claude access.', 'Я дозволяю доступ лише до квоти Claude.'));
  ClaudeAccessPage.Values[0] := False;

  CodexPage := CreateInputOptionPage(ClaudeAccessPage.ID,
    L('Codex', 'Codex'),
    L('Choose how Codex should appear.', 'Оберіть, як відображати Codex.'),
    L('Auto detects a trusted official client; Use keeps a setup/status lane; Skip removes Codex completely.',
      'Авто шукає довірений офіційний клієнт; Використовувати залишає смугу налаштування/стану; Пропустити повністю прибирає Codex.'),
    True, False);
  CodexPage.Add(L('Auto (recommended)', 'Авто (рекомендовано)'));
  CodexPage.Add(L('Use Codex', 'Використовувати Codex'));
  CodexPage.Add(L('Skip Codex', 'Пропустити Codex'));
  CodexPage.SelectedValueIndex := ProviderIndex(DefaultCodex);

  PreferencesPage := CreateInputOptionPage(CodexPage.ID,
    L('Appearance and startup', 'Вигляд і запуск'),
    L('Choose local preferences.', 'Оберіть локальні параметри.'),
    L('Monochrome and alerts off are the safe defaults. Auto providers defer Startup registration until a visible lane is confirmed.',
      'Безбарвний режим і вимкнені сповіщення є безпечними типовими значеннями. Для режиму Авто реєстрація запуску відкладається до підтвердження видимої смуги.'),
    False, False);
  PreferencesPage.Add(L('Start with Windows', 'Запускати разом із Windows'));
  PreferencesPage.Add(L('Use status colors', 'Використовувати кольори стану'));
  PreferencesPage.Values[0] := IsTrueParameter(ExpandConstant('{param:PLANAUTOSTART|1}'), True);
  PreferencesPage.Values[1] := DefaultColors = 'status';

  AlertsPage := CreateInputOptionPage(PreferencesPage.ID,
    L('Low-quota notifications', 'Сповіщення про низьку квоту'),
    L('Choose whether Windows notifications are enabled.', 'Оберіть, чи вмикати сповіщення Windows.'),
    L('Notifications are off by default and never include an account identity or raw provider response.',
      'Сповіщення типово вимкнені й ніколи не містять ідентифікатор облікового запису або необроблену відповідь постачальника.'),
    True, True);
  AlertsPage.Add(L('Off (recommended)', 'Вимкнено (рекомендовано)'));
  AlertsPage.Add(L('On', 'Увімкнено'));
  if DefaultAlert = '1' then AlertsPage.SelectedValueIndex := 1
  else AlertsPage.SelectedValueIndex := 0;

  ThresholdPage := CreateInputOptionPage(AlertsPage.ID,
    L('Low-limit warning', 'Попередження про малий залишок'),
    L('When should the widget warn you?', 'Коли попереджати про малий залишок?'),
    L('The widget will show one notification when the selected amount remains. It can notify you again after the limit resets.',
      'Віджет один раз покаже сповіщення, коли залишиться вибрана кількість ліміту. Після оновлення ліміту він зможе попередити знову.'),
    True, True);
  ThresholdPage.Add(L('20% remaining (recommended)', '20% залишилось (рекомендовано)'));
  ThresholdPage.Add(L('10% remaining', '10% залишилось'));
  ThresholdPage.Add(L('5% remaining', '5% залишилось'));
  ThresholdPage.SelectedValueIndex := ThresholdIndex(ExpandConstant('{param:PLANTHRESHOLD|20}'));

  QuietPage := CreateInputOptionPage(ThresholdPage.ID,
    L('Quiet hours', 'Тихі години'),
    L('Silence notifications during a bounded local-time interval.', 'Вимикайте сповіщення протягом обмеженого інтервалу місцевого часу.'),
    L('The saved interval remains visible even while notifications are off.',
      'Збережений інтервал залишається видимим, навіть коли сповіщення вимкнено.'),
    False, False);
  QuietPage.Add(L('Enable quiet hours', 'Увімкнути тихі години'));
  QuietPage.Values[0] := IsTrueParameter(ExpandConstant('{param:PLANQUIETHOURS|0}'), False);

  QuietTimesPage := CreateInputQueryPage(QuietPage.ID,
    L('Quiet-hours interval', 'Інтервал тихих годин'),
    L('Use 24-hour local time.', 'Використовуйте місцевий час у 24-годинному форматі.'),
    L('Start and end must be different HH:MM values.', 'Початок і кінець мають бути різними значеннями ГГ:ХХ.'));
  QuietTimesPage.Add(L('Start:', 'Початок:'), False);
  QuietTimesPage.Add(L('End:', 'Кінець:'), False);
  QuietTimesPage.Values[0] := DefaultStart;
  QuietTimesPage.Values[1] := DefaultEnd;

  ReviewPage := CreateOutputMsgMemoPage(QuietTimesPage.ID,
    L('Review the install plan', 'Перевірте план встановлення'),
    L('Claude quota access is granted only when you explicitly selected it. Installation never performs authentication.',
      'Доступ до квоти Claude надається лише тоді, коли ви явно його вибрали. Встановлення ніколи не виконує автентифікацію.'),
    L('Review every choice before continuing.', 'Перевірте кожен параметр перед продовженням.'), '');

  ConfirmPage := CreateInputOptionPage(ReviewPage.ID,
    L('Human confirmation', 'Підтвердження людиною'),
    L('Confirm the visible plan.', 'Підтвердьте видимий план.'),
    L('An AI agent may prefill choices, but it must not select this confirmation for you.',
      'AI-агент може попередньо заповнити параметри, але не має права вибирати це підтвердження замість вас.'),
    False, False);
  ConfirmPage.Add(L('I reviewed these choices and want to install.', 'Я перевірив(-ла) ці параметри й хочу встановити програму.'));
  ConfirmPage.Values[0] := False;
end;

function BuildPlanSummary(): string;
var
  ExistingLine: string;
begin
  if ExistingConfig then
  begin
    Result := L(
      'Existing preferences, provider selections, consent state, language, notification settings, and position will be preserved unchanged.',
      'Наявні параметри, вибір постачальників, стан згоди, мова, налаштування сповіщень і позиція будуть збережені без змін.') + #13#10#13#10 +
      L('The candidate will be staged, validated, health-checked offline, and activated only if all checks pass.',
        'Кандидат буде підготовлений, перевірений і протестований офлайн та активований лише після успішного проходження всіх перевірок.');
  end
  else
  begin
    ExistingLine := L('Existing preferences: none; this plan will initialize them', 'Наявні параметри: відсутні; цей план створить їх');
    Result :=
      L('Language: ', 'Мова: ') + SelectedLocale() + #13#10 +
      'Claude: ' + ProviderValue(ClaudePage) + #13#10 +
      L('Claude quota access: ', 'Доступ до квоти Claude: ') + BoolJson(ClaudeAccessGranted()) + #13#10 +
      'Codex: ' + ProviderValue(CodexPage) + #13#10 +
      L('Start with Windows: ', 'Запускати з Windows: ') + BoolJson(StartWithWindowsSelected()) + #13#10 +
      L('Colors: ', 'Кольори: ') + ColorValue() + #13#10 +
      L('Notifications: ', 'Сповіщення: ') + BoolJson(AlertsEnabled()) + #13#10 +
      L('Threshold: ', 'Поріг: ') + IntToStr(ThresholdValue()) + '%' + #13#10 +
      L('Quiet hours: ', 'Тихі години: ') + BoolJson(QuietPage.Values[0]) + ' ' + QuietTimesPage.Values[0] + '-' + QuietTimesPage.Values[1] + #13#10 +
      ExistingLine + #13#10#13#10 +
      L('If quota access is allowed and Claude is already signed in, the first widget launch connects immediately. Otherwise the widget shows one visible Connect action.',
        'Якщо доступ до квоти дозволено й вхід у Claude уже виконано, віджет підключиться одразу під час першого запуску. Інакше віджет покаже одну видиму дію «Підключити».');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = ReviewPage.ID then
    ReviewPage.RichEditViewer.Text := BuildPlanSummary();
end;

function SilentDefaultsAccepted(): Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:ACCEPTDEFAULTS|0}') = '1');
end;

function PostInstallOpenPermitted(): Boolean;
begin
  Result := (not WizardSilent) or
    (SilentDefaultsAccepted() and (ExpandConstant('{param:OPENAFTERINSTALL|0}') = '1'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = QuietTimesPage.ID then
  begin
    if not IsBoundedLocalTime(QuietTimesPage.Values[0]) or
       not IsBoundedLocalTime(QuietTimesPage.Values[1]) or
       (QuietTimesPage.Values[0] = QuietTimesPage.Values[1]) then
    begin
      MsgBox(L('Enter two different local times in HH:MM format.', 'Введіть два різні значення місцевого часу у форматі ГГ:ХХ.'), mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Normal installation uses sensible defaults and asks only for the one
    permission that cannot be inferred.  Advanced choices remain available
    through the widget Settings surface. }
  Result := WizardSilent or
    (PageID = ClaudePage.ID) or
    (PageID = CodexPage.ID) or
    (PageID = PreferencesPage.ID) or
    (PageID = AlertsPage.ID) or
    (PageID = ThresholdPage.ID) or
    (PageID = QuietPage.ID) or
    (PageID = QuietTimesPage.ID) or
    (PageID = ReviewPage.ID) or
    (PageID = ConfirmPage.ID);
end;

function IsHexCharacter(Value: Char): Boolean;
begin
  Result := ((Value >= '0') and (Value <= '9')) or ((Value >= 'a') and (Value <= 'f'));
end;

function IsSafeCandidateId(Value: string): Boolean;
var
  Index: Integer;
begin
  Result := Length(Value) = 64;
  if not Result then Exit;
  for Index := 1 to Length(Value) do
  begin
    if not IsHexCharacter(Value[Index]) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function IsReparsePoint(PathValue: string): Boolean;
var
  Attributes: Cardinal;
begin
  Attributes := GetFileAttributes(PathValue);
  Result := (Attributes <> $FFFFFFFF) and ((Attributes and REPARSE_POINT_ATTRIBUTE) <> 0);
end;

function RunOwnedShortcut(ActionValue: string; KindValue: string; Candidate: string): Boolean;
var
  PowerShellPath: string;
  ShortcutScript: string;
  Parameters: string;
  ResultCode: Integer;
begin
  Result := False;
  if not IsSafeCandidateId(Candidate) then Exit;
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ShortcutScript := ExpandConstant('{app}\versions\') + Candidate + '\Invoke-OwnedShortcut.ps1';
  if not FileExists(ShortcutScript) or IsReparsePoint(ShortcutScript) then Exit;
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ShortcutScript + '"' +
    ' -Action ' + ActionValue +
    ' -Kind ' + KindValue +
    ' -ProductRoot "' + ExpandConstant('{app}') + '"' +
    ' -AppDataRoot "' + ExpandConstant('{userappdata}') + '"';
  if (ActionValue = 'Ensure') or (ActionValue = 'Probe') then
    Parameters := Parameters + ' -CandidateId ' + Candidate;
  Result := Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and
    (ResultCode = 0);
  if not Result then Log('LIMIT_HALO_SHORTCUT_PRESERVED_OR_UNAVAILABLE ' + KindValue);
end;

procedure RunManifestTool(Mode: string);
var
  PowerShellPath: string;
  CleanupScript: string;
  Parameters: string;
  ResultCode: Integer;
begin
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  CleanupScript := ExpandConstant('{app}\versions\{#CandidateId}\Invoke-ManifestOwnedCleanup.ps1');
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + CleanupScript + '"' +
    ' -AllowedParentRoot "' + ExpandConstant('{localappdata}\Programs') + '"' +
    ' -ProductRoot "' + ExpandConstant('{app}') + '"';
  if Mode = 'delete-all' then
    Parameters := Parameters + ' -DeleteAll'
  else if Mode = 'cleanup' then
    Parameters := Parameters + ' -UseCandidateMarkers'
  else if Mode = 'validate' then
    Parameters := Parameters + ' -ValidateCandidate {#CandidateId}'
  else if Mode = 'delete-candidate' then
    Parameters := Parameters + ' -DeleteCandidate {#CandidateId}'
  else
    RaiseException('Unknown manifest tool mode.');
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    RaiseException('Manifest-owned payload operation failed closed.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PowerShellPath: string;
  ValidatorPath: string;
  ManifestPath: string;
  Parameters: string;
  ResultCode: Integer;
begin
  Result := '';
  if WizardSilent and not SilentDefaultsAccepted() then
  begin
    Log('LIMIT_HALO_SILENT_DEFAULTS_ACCEPTANCE_REQUIRED');
    Result := 'Silent installation requires the explicit ACCEPTDEFAULTS=1 parameter.';
    Exit;
  end;
  ExtractTemporaryFile('LimitHaloPreflightManifest.json');
  ExtractTemporaryFile('LimitHaloPreflightValidator.ps1');
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ValidatorPath := ExpandConstant('{tmp}\LimitHaloPreflightValidator.ps1');
  ManifestPath := ExpandConstant('{tmp}\LimitHaloPreflightManifest.json');
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ValidatorPath + '"' +
    ' -ManifestPath "' + ManifestPath + '" -ExpectedCandidateId {#CandidateId}';
  if not Exec(PowerShellPath, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    Log('LIMIT_HALO_PREFLIGHT_VALIDATION_FAILED');
    Result := 'Package staging validation failed. No product files were changed.';
  end
  else
    Log('LIMIT_HALO_PREFLIGHT_VALIDATION_PASS');
end;

function ReadCandidateMarker(PathValue: string): string;
var
  Content: AnsiString;
begin
  Result := '';
  if IsReparsePoint(PathValue) or not LoadStringFromFile(PathValue, Content) then Exit;
  Content := Trim(String(Content));
  if IsSafeCandidateId(String(Content)) then Result := String(Content);
end;

procedure WriteCandidateMarker(PathValue: string; Candidate: string);
begin
  if IsSafeCandidateId(Candidate) then SaveStringToFile(PathValue, Candidate + #13#10, False);
end;

function BuildInitialConfig(): string;
begin
  Result :=
    '{' + #13#10 +
    '  "schemaVersion": 3,' + #13#10 +
    '  "locale": "' + SelectedLocale() + '",' + #13#10 +
    '  "providers": {' + #13#10 +
    '    "claude": "' + ClaudeProviderValue() + '",' + #13#10 +
    '    "codex": "' + ProviderValue(CodexPage) + '"' + #13#10 +
    '  },' + #13#10 +
    '  "claudeConsent": "' + ClaudeConsentValue() + '",' + #13#10 +
    '  "autostart": ' + BoolJson(StartWithWindowsSelected()) + ',' + #13#10 +
    '  "colors": "' + ColorValue() + '",' + #13#10 +
    '  "alerts": {' + #13#10 +
    '    "enabled": ' + BoolJson(AlertsEnabled()) + ',' + #13#10 +
    '    "thresholdPercent": ' + IntToStr(ThresholdValue()) + ',' + #13#10 +
    '    "quietHours": {' + #13#10 +
    '      "enabled": ' + BoolJson(QuietPage.Values[0]) + ',' + #13#10 +
    '      "start": "' + QuietTimesPage.Values[0] + '",' + #13#10 +
    '      "end": "' + QuietTimesPage.Values[1] + '"' + #13#10 +
    '    }' + #13#10 +
    '  },' + #13#10 +
    '  "topmost": true,' + #13#10 +
    '  "view": "compact",' + #13#10 +
    '  "x": null,' + #13#10 +
    '  "y": null' + #13#10 +
    '}' + #13#10;
end;

procedure WriteInitialConfigIfAbsent();
var
  SettingsRoot: string;
  TargetPath: string;
  TemporaryPath: string;
begin
  TargetPath := ConfigPath();
  if FileExists(TargetPath) then Exit;
  SettingsRoot := ExtractFileDir(TargetPath);
  if IsReparsePoint(SettingsRoot) or IsReparsePoint(TargetPath) then
    RaiseException('Settings path contains a reparse point.');
  if not ForceDirectories(SettingsRoot) then
    RaiseException('Unable to create the settings directory.');
  if IsReparsePoint(SettingsRoot) then
    RaiseException('Settings directory became a reparse point.');
  TemporaryPath := TargetPath + '.{#CandidateId}.tmp';
  DeleteFile(TemporaryPath);
  if not SaveStringToFile(TemporaryPath, BuildInitialConfig(), False) then
    RaiseException('Unable to stage the initial settings.');
  if not RenameFile(TemporaryPath, TargetPath) then
  begin
    DeleteFile(TemporaryPath);
    RaiseException('Unable to atomically activate the initial settings.');
  end;
end;

function RunCandidateHealthCheck(): Boolean;
var
  CandidateRoot: string;
  ExecutablePath: string;
  ResultCode: Integer;
begin
  CandidateRoot := ExpandConstant('{app}\versions\{#CandidateId}');
  ExecutablePath := AddBackslash(CandidateRoot) + '{#ProductExe}';
  Result := FileExists(ExecutablePath) and
    Exec(ExecutablePath, '--health-check --offline --no-provider-start', CandidateRoot,
      SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure WriteOwnedShortcuts(Candidate: string; InstallStartup: Boolean; ShowHud: Boolean;
  var ActualStartup: Boolean);
begin
  if not IsSafeCandidateId(Candidate) then RaiseException('Shortcut candidate identity is invalid.');
  { Settings lives in the HUD menu; remove an older redundant owned shortcut. }
  RunOwnedShortcut('Remove', 'Configure', Candidate);
  if ShowHud then
    RunOwnedShortcut('Ensure', 'Hud', Candidate)
  else
    RunOwnedShortcut('Remove', 'Hud', Candidate);
  if InstallStartup then
    ActualStartup := RunOwnedShortcut('Ensure', 'Startup', Candidate)
  else
  begin
    RunOwnedShortcut('Remove', 'Startup', Candidate);
    ActualStartup := False;
  end;
end;

function HudTargetSelected(): Boolean;
begin
  Result := ExistingConfig or not BothProvidersDisabled();
end;

function ShouldApplyClaudeConsent(): Boolean;
begin
  Result := CandidateHealthPassed and ClaudeAccessGranted();
end;

procedure PrepareHudPostInstallLaunch();
begin
  Log('LIMIT_HALO_POSTINSTALL_ROUTE=HUD');
  if not SetEnvironmentVariable('LIMIT_HALO_POSTINSTALL_OPEN', '1') then
    RaiseException('Unable to prepare the one-time visible HUD launch.');
end;

procedure ClearPostInstallLaunchSignal();
begin
  if SetEnvironmentVariable('LIMIT_HALO_POSTINSTALL_OPEN', '0') then
    Log('LIMIT_HALO_POSTINSTALL_SIGNAL=CLEARED')
  else
    Log('LIMIT_HALO_POSTINSTALL_SIGNAL=CLEAR_FAILED');
end;

procedure PrepareConfigurePostInstallLaunch();
begin
  Log('LIMIT_HALO_POSTINSTALL_ROUTE=CONFIGURE');
  if not SetEnvironmentVariable('LIMIT_HALO_POSTINSTALL_OPEN', '0') then
    RaiseException('Unable to isolate the Configure launch from the one-time HUD signal.');
end;

function ShouldOfferHudLaunch(): Boolean;
begin
  Result := CandidateHealthPassed and PostInstallOpenPermitted() and HudTargetSelected();
end;

function ShouldOfferConfigureLaunch(): Boolean;
begin
  Result := CandidateHealthPassed and PostInstallOpenPermitted() and not HudTargetSelected();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ActiveMarker: string;
  RollbackMarker: string;
  InstallStartup: Boolean;
  ActualStartup: Boolean;
  ShowHud: Boolean;
begin
  ActiveMarker := ExpandConstant('{app}\active-candidate.txt');
  RollbackMarker := ExpandConstant('{app}\rollback-candidate.txt');
  if CurStep = ssInstall then
    PreviousCandidate := ReadCandidateMarker(ActiveMarker);
  if CurStep = ssPostInstall then
  begin
    RunManifestTool('validate');
    if not RunCandidateHealthCheck() then
    begin
      RunManifestTool('delete-candidate');
      RaiseException('The staged candidate failed its offline health check. The previous active version remains unchanged.');
    end;
    CandidateHealthPassed := True;
    if (PreviousCandidate <> '{#CandidateId}') and IsSafeCandidateId(PreviousCandidate) then
      WriteCandidateMarker(RollbackMarker, PreviousCandidate)
    else
      PreviousCandidate := ReadCandidateMarker(RollbackMarker);
    WriteCandidateMarker(ActiveMarker, '{#CandidateId}');
    ShowHud := ExistingConfig or not BothProvidersDisabled();
    if ExistingConfig then
      InstallStartup := PreviousStartupPresent
    else
      InstallStartup := StartWithWindowsSelected() and not BothProvidersDisabled();
    WriteOwnedShortcuts('{#CandidateId}', InstallStartup, ShowHud, ActualStartup);
    if not ExistingConfig then
    begin
      PreferencesPage.Values[0] := ActualStartup;
      WriteInitialConfigIfAbsent();
    end;
    RunManifestTool('cleanup');
  end;
end;

function InitializeUninstall(): Boolean;
begin
  RemoveSettingsChoice := False;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigFilePath: string;
  SettingsRoot: string;
  ParameterChoice: string;
  ActiveCandidate: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    ParameterChoice := ExpandConstant('{param:REMOVESETTINGS|0}');
    if UninstallSilent then
      RemoveSettingsChoice := ParameterChoice = '1'
    else
      RemoveSettingsChoice := SuppressibleMsgBox(
        'Remove local LimitHalo settings? Claude and Codex credentials are never touched.',
        mbConfirmation, MB_YESNO, IDNO) = IDYES;
    ActiveCandidate := ReadCandidateMarker(ExpandConstant('{app}\active-candidate.txt'));
    if IsSafeCandidateId(ActiveCandidate) then
    begin
      RunOwnedShortcut('Remove', 'Hud', ActiveCandidate);
      RunOwnedShortcut('Remove', 'Configure', ActiveCandidate);
      RunOwnedShortcut('Remove', 'Startup', ActiveCandidate);
    end;
    RunManifestTool('delete-all');
    DeleteFile(ExpandConstant('{app}\active-candidate.txt'));
    DeleteFile(ExpandConstant('{app}\rollback-candidate.txt'));
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if RemoveSettingsChoice then
    begin
      SettingsRoot := ExpandConstant('{localappdata}\{#InternalProductId}');
      ConfigFilePath := AddBackslash(SettingsRoot) + 'config.json';
      if not IsReparsePoint(SettingsRoot) and not IsReparsePoint(ConfigFilePath) then
      begin
        DeleteFile(ConfigFilePath);
        DeleteFile(AddBackslash(SettingsRoot) + 'install-plan.json');
        RemoveDir(SettingsRoot);
      end;
    end;
  end;
end;
