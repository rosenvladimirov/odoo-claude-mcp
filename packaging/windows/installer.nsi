; Odoo Connection Manager — NSIS Installer
; Build with: makensis installer.nsi (after build.sh)

!include "MUI2.nsh"

Name "Odoo Connection Manager"
OutFile "OdooConnectSetup.exe"
InstallDir "$PROGRAMFILES\OdooConnect"
RequestExecutionLevel admin

; UI
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"

; Pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "$INSTDIR"

    ; Main executable
    File "..\..\dist\OdooConnect.exe"

    ; Create start menu shortcut
    CreateDirectory "$SMPROGRAMS\Odoo Connection Manager"
    CreateShortCut "$SMPROGRAMS\Odoo Connection Manager\Odoo Connect.lnk" "$INSTDIR\OdooConnect.exe"
    CreateShortCut "$SMPROGRAMS\Odoo Connection Manager\Uninstall.lnk" "$INSTDIR\Uninstall.exe"

    ; Desktop shortcut
    CreateShortCut "$DESKTOP\Odoo Connect.lnk" "$INSTDIR\OdooConnect.exe"

    ; Uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"

    ; Add/Remove Programs entry
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooConnect" \
        "DisplayName" "Odoo Connection Manager"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooConnect" \
        "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooConnect" \
        "Publisher" "BL Consulting"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\OdooConnect.exe"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"

    Delete "$SMPROGRAMS\Odoo Connection Manager\Odoo Connect.lnk"
    Delete "$SMPROGRAMS\Odoo Connection Manager\Uninstall.lnk"
    RMDir "$SMPROGRAMS\Odoo Connection Manager"
    Delete "$DESKTOP\Odoo Connect.lnk"

    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooConnect"
SectionEnd
