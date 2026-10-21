; Svpn — установщик для коллег. Собирается makensis (NSIS 3).
; Ставит в %LOCALAPPDATA%\Programs\Svpn без прав администратора.
; Права администратора Svpn попросит сам при первом запуске (перехватчик).
Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!ifndef VERSION
  !define VERSION "1.1.0"
!endif
!define APP "Svpn"
!define UNKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Svpn"
!define RUNEXE "$INSTDIR\runtime\Svpn.exe"

Name "${APP}"
Caption "${APP} ${VERSION} — установка"
OutFile "Svpn-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Svpn"
RequestExecutionLevel user
ShowInstDetails nevershow
ShowUninstDetails nevershow
BrandingText "${APP} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "FileDescription" "Установка Svpn"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" " "

!include "MUI2.nsh"
!define MUI_ICON "payload\ui\svpn.ico"
!define MUI_UNICON "payload\ui\svpn.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "Установка Svpn ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Svpn пускает через VPN только нужные рабочие программы и сайты — Фотошоп, генеративную заливку, Gemini, Claude, Телеграм. Всё остальное идёт напрямую.$\r$\n$\r$\nПонадобится ссылка на твою подписку VPN — вставишь её после установки.$\r$\n$\r$\nЕсли Svpn уже стоит, он закроется и обновится, настройки сохранятся."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "Svpn установлен"
!define MUI_FINISHPAGE_TEXT "При первом запуске Windows спросит разрешение на изменения — нажми «Да»: так Svpn настроит перехват программ.$\r$\n$\r$\nПотом откроется окно Svpn — вставь ссылку на подписку и нажми «Сохранить и проверить».$\r$\n$\r$\nДальше Svpn живёт в трее (у часов) и обновляется сам."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Запустить Svpn"
!define MUI_FINISHPAGE_RUN_FUNCTION RunSvpn
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

Function RunSvpn
  Exec '"${RUNEXE}" "$INSTDIR\app.py"'
FunctionEnd

!macro StopSvpn
  ; закрыть запущенный Svpn: перехватчик, окно и VPN-движок
  nsExec::Exec 'schtasks /End /TN "Svpn Interceptor"'
  Pop $0
  nsExec::Exec 'taskkill /F /IM Svpn.exe'
  Pop $0
  Sleep 800
  nsExec::Exec 'taskkill /F /IM xray.exe'
  Pop $0
  Sleep 700
!macroend

Section "Svpn" SecMain
  SetShellVarContext current
  !insertmacro StopSvpn

  ; старую среду Python убираем целиком, чтобы не осталось лишних файлов
  RMDir /r "$INSTDIR\runtime"
  SetOutPath "$INSTDIR"
  File /r "payload\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateShortcut "$DESKTOP\Svpn.lnk" "${RUNEXE}" '"$INSTDIR\app.py"' "$INSTDIR\ui\svpn.ico" 0 SW_SHOWNORMAL "" "Svpn — VPN для рабочих программ"
  CreateShortcut "$SMPROGRAMS\Svpn.lnk" "${RUNEXE}" '"$INSTDIR\app.py"' "$INSTDIR\ui\svpn.ico" 0 SW_SHOWNORMAL "" "Svpn — VPN для рабочих программ"

  WriteRegStr HKCU "${UNKEY}" "DisplayName" "Svpn"
  WriteRegStr HKCU "${UNKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNKEY}" "DisplayIcon" "$INSTDIR\ui\svpn.ico"
  WriteRegStr HKCU "${UNKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  nsExec::Exec 'taskkill /F /IM Svpn.exe'
  Pop $0
  ; Svpn сам уберёт автозапуск, задачу перехватчика и правило брандмауэра
  ; (Windows спросит разрешение)
  ExecWait '"${RUNEXE}" "$INSTDIR\app.py" --uninstall'
  !insertmacro StopSvpn

  Delete "$DESKTOP\Svpn.lnk"
  Delete "$SMPROGRAMS\Svpn.lnk"
  DeleteRegKey HKCU "${UNKEY}"
  RMDir /r "$INSTDIR"

  MessageBox MB_YESNO|MB_ICONQUESTION "Удалить и настройки Svpn — подписку и списки сайтов и программ?$\r$\nЕсли собираешься поставить Svpn заново, нажми «Нет»." /SD IDNO IDNO keep
    RMDir /r "$LOCALAPPDATA\Svpn"
  keep:
SectionEnd
