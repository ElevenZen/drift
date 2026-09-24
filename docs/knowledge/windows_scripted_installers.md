# Windows Scriptable Installers: NSIS & Inno Setup Deep Dive

This document provides a comprehensive architectural analysis of **Nullsoft Scriptable Install System (NSIS)** and **Inno Setup**, the two foundational script-driven, self-extracting installer frameworks for the Windows platform. It covers binary construction, packaging pipelines, runtime installation and uninstallation mechanics, metadata schemas, lifecycle hooks, and the historical design constraints that shaped their domain-specific languages.

Companion Reference: See [`docs/knowledge/windows_packaging_mechanisms.md`](windows_packaging_mechanisms.md) for Windows Installer (MSI), MSIX, and cross-platform comparisons with Linux packaging.

---

## 1. Architectural Paradigm: Monolithic Self-Extracting Executables (SFX)

Unlike **Windows Installer (MSI)** (which relies on the elevated `msiserver` service interpreting relational database tables) or **MSIX** (which executes within an OS-level lightweight container virtualization boundary), NSIS and Inno Setup operate on the **Monolithic Self-Extracting Executable (SFX)** model.

* **Standalone Binary Delivery**: All assets—runtime engine, metadata tables, decompression routines, and compressed file payloads—are merged into a single standalone Win32/Win64 Portable Executable (`.exe`).
* **Zero External Dependencies**: The installer executes directly on bare-metal Windows installations without requiring pre-installed frameworks, runtimes, or database services.
* **Process-Level Isolation**: Each installer executes as an independent, standard Win32 process without acquiring global system-wide transaction locks (avoiding MSI Error 1618).

---

## 2. Binary Architecture of Compiled PE Installers

A compiled NSIS or Inno Setup installer consists of three primary layers arranged in sequence:

```text
+-------------------------------------------------------------+
| 1. Win32 PE Executable Header & Stub                        |
|    - Standard MZ/PE header and code sections                |
|    - Embedded UAC XML Manifest (<requestedExecutionLevel>)  |
|    - Win32 UI dialog manager (C/C++ in NSIS, Delphi in Inno)|
|    - Integrated decompression engine (LZMA, Deflate, etc.)  |
+-------------------------------------------------------------+
| 2. Execution Metadata & Instruction Block                   |
|    - Deduplicated string tables (paths, UI text, messages)  |
|    - File catalog, target layout, and permission tables     |
|    - Compiled bytecode / instruction opcode stream          |
+-------------------------------------------------------------+
| 3. Compressed Data Stream (PE Overlay / Sliced Volume)      |
|    - Continuous or chunked compressed payload archives      |
|    - Cryptographic checksums (CRC32, SHA-256)               |
+-------------------------------------------------------------+
```

### The In-Memory Streaming & Overlay Model
1. **OS PE Loader Execution**:
   * When launched, the Windows kernel reads the PE header and maps only the compiled executable stub into memory up to its declared `SizeOfImage`.
2. **Self-Inspection via Win32 API**:
   * The executing stub calls `GetModuleFileName(NULL, ...)` to locate its own binary path on the filesystem.
3. **Overlay Seeking & Chunked Decompression**:
   * The stub opens its own executable file via `CreateFileW` with `FILE_SHARE_READ`, seeks past the PE image directly to the appended overlay stream, and reads metadata and payload chunks.
   * By using fixed-size streaming ring buffers (64 KB – 4 MB), the installer decompresses and writes files directly to disk without loading multi-gigabyte payloads into system RAM.

---

## 3. The Packaging (Creation / Build) Process

During the build phase, developer scripts are parsed, validated, and merged with raw assets to create the standalone installer binary.

### A. NSIS (`makensis.exe`) Build Pipeline
1. **Script Preprocessing & Macro Expansion**:
   * Parses `.nsi` and included `.nsh` files.
   * Expands macros (`!macro`, `!insertmacro`), defines (`!define`), and conditional compiler directives (`!ifdef`).
2. **String Deduplication & String Table Construction**:
   * Gathers all string literals (paths, UI labels, registry keys, error strings).
   * Deduplicates identical strings into an indexed, null-terminated contiguous binary memory block.
3. **Opcode & Bytecode Compilation**:
   * Compiles procedural code blocks (`Section`, `Function`) into a compact array of fixed-size C-struct instructions (`struct entry`).
   * Instructions target a lightweight virtual machine equipped with registers (`$0`–`$9`, `$R0`–`$R9`), a runtime stack (`Push`, `Pop`), arithmetic/string opcodes, and Win32 API wrappers (`CreateDirectory`, `RegSetValue`, `ExecWait`).
4. **Solid Compression Streaming**:
   * When files are declared via `File "app.exe"`, their raw bytes are fed into a single unified compression engine (default: **Solid LZMA**).
   * In solid mode, all files and metadata are compressed as a single continuous byte stream across one large sliding dictionary (up to 64 MB), maximizing compression across redundant binaries and libraries.
5. **Binary Assembly**:
   * Injects resources (application icon, version info, and UAC manifest) into the pre-compiled C++ loader stub (`stub.bin`).
   * Appends the compiled opcode table, string table, language tables, and the compressed payload stream directly to the end of the executable.

### B. Inno Setup (`ISCC.exe`) Build Pipeline
1. **Section Tokenization**:
   * Parses declarative INI sections (`[Setup]`, `[Files]`, `[Dirs]`, `[Registry]`, `[Icons]`, `[Run]`, `[InstallDelete]`).
2. **Pascal Script Compilation**:
   * Compiles procedural Pascal code in `[Code]` into bytecode p-code using the integrated **RemObjects Pascal Script** compiler.
3. **Setup Metadata Record Construction**:
   * Builds binary metadata tables:
     * **File Records**: Source/target path macros (`{app}\bin\app.exe`), file timestamps, version rules, permissions, and component/task associations.
     * **Registry Records**: Root keys, subkeys, data types, values, and overwrite flags.
     * **Directory Records**: Target path creation tree and ACL flags.
4. **Payload Compression & Slicing**:
   * Compresses payload files using **LZMA2 / Deflate / Bzip2**.
   * **Disk Spanning Support**: If `DiskSpanning=yes` is enabled, the compiler partitions compressed output into discrete archive volumes (`setup-1.bin`, `setup-2.bin`) capped at `DiskSliceSize`.
5. **Binary Assembly**:
   * Merges compiled metadata tables and Pascal Script p-code with the Delphi loader stub (`SetupLdr.exe`), generating `setup.exe`.

---

## 4. The Installation (Run-Time) Process

When the user launches `Setup.exe`, the installer executes an in-memory streaming workflow:

```text
[ Target System Installation Workflow ]
1. OS Launch & UAC Elevation (Manifest check -> Token elevation)
        |
        v
2. Stub Self-Inspection (GetModuleFileName -> Seek past PE header)
        |
        v
3. Unpack Metadata & Plugins (Decompress string tables, GUI scripts, DLL plugins)
        |
        v
4. Wizard UI & Pre-flight Hooks (.onInit / InitializeSetup() -> OS & Prereq checks)
        |
        v
5. Streaming Decompression & Target File Extraction (Ring buffer decompress -> WriteFileW)
        |
        v
6. State Journaling / Uninstaller Writing:
   - NSIS:       Writes compiled uninstaller stub to "$INSTDIR\uninstall.exe"
   - Inno Setup: Writes unins000.exe and journals every change into "unins000.dat"
        |
        v
7. Registry & Shortcut Creation (COM IShellLink -> Start Menu / Desktop .lnk)
        |
        v
8. Post-Install Triggers & Cleanup (.onInstSuccess / [Run] -> Flush temp folders)
```

### Detailed Runtime Mechanisms

#### 1. OS Elevation & Privilege Management
* The Windows kernel inspects the embedded PE application manifest (`<requestedExecutionLevel>`).
* If `level="requireAdministrator"`, Windows prompts for UAC elevation before spawning the process.
* If configured for **Dual-Mode / Non-Admin (Per-User)** installation into `%LOCALAPPDATA%`, it executes under the user's standard security token without requiring elevation.

#### 2. Pre-Flight Initialization & Mutex Verification
* Pre-flight callbacks execute before GUI rendering:
  * NSIS executes `.onInit`.
  * Inno Setup executes `InitializeSetup()`.
* Common pre-flight tasks include verifying OS build versions, querying hardware capabilities, checking available disk space, and validating that the target application is not currently running via named Win32 mutexes (`CreateMutexW`).

#### 3. Streaming File Extraction
* Files are extracted using a fixed-size streaming buffer (64 KB – 4 MB):
  1. Creates target directories if missing (`CreateDirectoryW`).
  2. Decompresses bytes sequentially from the payload stream and writes them directly to the destination (`CreateFileW` $\rightarrow$ `WriteFileW`).
  3. Restores original file timestamps (`SetFileTime`) and permissions/attributes (`SetFileAttributesW`).

#### 4. Handling Locked / In-Use Files (`restartreplace`)
* If an existing `.dll` or `.exe` is locked by a running process:
  1. The installer extracts the updated file under a temporary name (e.g., `app.exe.tmp`).
  2. Calls the Win32 API:
     ```c
     MoveFileExW(tempPath, targetPath, MOVEFILE_DELAY_UNTIL_REBOOT);
     ```
  3. Windows records this swap in:
     ```text
     HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations
     ```
  4. The installer flags a system reboot requirement upon exit, and `smss.exe` executes the queued file swap during the next system boot cycle.

#### 5. Uninstaller Registration
* **NSIS**: Executes `WriteUninstaller "$INSTDIR\uninstall.exe"`, writing a dedicated uninstallation executable containing the compiled `Section "Uninstall"` bytecode.
* **Inno Setup**: Copies `unins000.exe` into `{app}` and flushes the binary audit trail into `{app}\unins000.dat`.
* Registers uninstallation metadata and display properties under:
  ```text
  HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\<AppGuidOrName>
  ```

---

## 5. The Uninstallation Process

Uninstallation requires resolving a fundamental Windows operating system rule: **an active running executable cannot delete itself from disk** (`ERROR_ACCESS_DENIED`).

```text
[ Uninstallation Workflow ]
1. User triggers Uninstall (Control Panel / Settings / uninstall.exe)
        |
        v
2. Self-Cloning Trampoline:
   - Copies uninstall.exe -> %TEMP%\~uninst.exe (or %TEMP%\is-XXXXX.tmp\unins000.exe)
   - Launches Temp clone with target directory argument
   - Original uninstaller process terminates immediately (unlocking $INSTDIR)
        |
        v
3. Teardown Execution:
   - NSIS:       Executes procedural Section "Uninstall" opcodes
   - Inno Setup: Replays unins000.dat in REVERSE order (restoring old registry values)
        |
        v
4. File & Registry Removal:
   - Deletes installed files (DeleteFileW)
   - Prunes empty parent directories (RemoveDirectoryW)
   - Deletes Add/Remove Programs registry key
        |
        v
5. Delayed Self-Cleanup:
   - Temp uninstaller schedules itself for deletion on reboot (MoveFileExW)
     OR spawns a detached cmd.exe process loop to delete the temp folder
```

### Detailed Teardown Mechanisms

#### 1. The Self-Cloning "Trampoline" Workaround
1. When launched from `$INSTDIR\uninstall.exe`, the uninstaller detects that its binary resides in the target application directory.
2. It generates a unique directory in `%TEMP%` (e.g., `%TEMP%\~uninst.exe` or `%TEMP%\is-12345.tmp\unins000.exe`).
3. It copies its own binary into this temporary directory.
4. It launches the cloned temporary binary via `CreateProcessW`, passing the installation directory path as a parameter:
   ```text
   %TEMP%\~uninst.exe _?=$INSTDIR
   ```
5. The original `$INSTDIR\uninstall.exe` terminates immediately, completely unlocking the installation directory.

#### 2. Teardown Execution: Procedural vs. Journaled

* **NSIS (Procedural Execution)**:
  * The temporary uninstaller reads its embedded `Section "Uninstall"` opcode stream.
  * Deletes files explicitly declared in the script (`Delete "$INSTDIR\app.exe"`).
  * Removes registry keys (`DeleteRegKey HKLM "Software\MyApp"`).
  * Calls `RMDir "$INSTDIR"`. By default, `RemoveDirectoryW` only deletes directories if they are empty, preventing accidental deletion of user-generated files unless recursive deletion (`RMDir /r`) is explicitly specified.

* **Inno Setup (Journaled Rollback Execution)**:
  * The temporary uninstaller opens `{app}\unins000.dat`.
  * Verifies the CRC checksum and deserializes all recorded installation actions.
  * **Replays records in reverse chronological order**:
    * **Registry**: Restores previous values for modified keys or deletes created keys.
    * **Shortcuts**: Deletes `.lnk` files from Start Menu and Desktop.
    * **Files**: Calls `DeleteFileW` for every logged file. If a file was modified by the user after installation, safety rules can preserve the file or prompt the user.
    * **Directories**: Recursively removes created directory trees if empty.
  * Deletes the original `{app}\unins000.exe` and `{app}\unins000.dat`.

#### 3. Cleanup of the Temporary Uninstaller
* **Detached Process Deletion**: Before exiting, the temporary process spawns a detached shell command:
  ```cmd
  cmd.exe /c "timeout /t 1 >nul & rmdir /s /q %TEMP%\is-12345.tmp"
  ```
* **Reboot Cleanup**: Calls `MoveFileExW(tempExePath, NULL, MOVEFILE_DELAY_UNTIL_REBOOT)` to have `smss.exe` remove the leftover temporary binary on next boot.

---

## 6. Metadata Syntax: `.nsi` vs. `.iss`

### A. NSIS Metadata (`.nsi`)
NSIS uses top-level compiler directives and standard library macros (`MUI2.nsh`):

```nsis
; === General Metadata ===
Name "Drift CLI"
OutFile "drift_setup.exe"
InstallDir "$PROGRAMFILES64\Drift"
InstallDirRegKey HKLM "Software\Drift" "InstallDir"
RequestExecutionLevel admin

; === PE Version Info Resources ===
VIProductVersion "1.4.0.0"
VIAddVersionKey "ProductName" "Drift CLI"
VIAddVersionKey "ProductVersion" "1.4.0"
VIAddVersionKey "CompanyName" "ElevenZen Corp"
VIAddVersionKey "FileDescription" "Drift Declarative Configuration Engine"
VIAddVersionKey "LegalCopyright" "© 2026 ElevenZen"

; === Compression Settings ===
SetCompressor /SOLID lzma
SetCompressorDictSize 64

; === UI & Page Navigation ===
!include "MUI2.nsh"
!define MUI_ICON "assets\app.ico"
!define MUI_UNICON "assets\app.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
```

### B. Inno Setup Metadata (`.iss`)
Inno Setup uses declarative INI sections:

```ini
[Setup]
; === General App Metadata ===
AppName=Drift CLI
AppVersion=1.4.0
AppPublisher=ElevenZen Corp
AppPublisherURL=https://github.com/ElevenZen/drift
DefaultDirName={autopf}\Drift
DefaultGroupName=Drift
OutputBaseFilename=drift_setup
OutputDir=.\dist

; === Privilege & Architecture Constraints ===
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; === Compression Settings ===
Compression=lzma2/max
SolidCompression=yes

; === Version & UI Resources ===
VersionInfoVersion=1.4.0.0
VersionInfoCompany=ElevenZen Corp
VersionInfoDescription=Drift Declarative Configuration Engine
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\drift.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
```

---

## 7. Requirements & Prerequisites

### A. OS Version & Mutex Checks

#### Inno Setup (Declarative + Pascal Guard)
```ini
[Setup]
AppMutex=DriftRunningInstanceMutex
MinVersion=10.0.19041
```

```pascal
[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if RegKeyExists(HKLM, 'Software\ConflictingEngine') then
  begin
    MsgBox('A conflicting configuration manager was detected. Please uninstall it first.', mbError, MB_OK);
    Result := False; // Aborts installation
    Exit;
  end;
end;
```

#### NSIS (Imperative `.onInit` Callback)
```nsis
!include "WinVer.nsh"
!include "x64.nsh"

Function .onInit
  ; Architecture Check
  ${IfNot} ${IsNativeAMD64}
    MessageBox MB_ICONSTOP "This application requires a 64-bit operating system."
    Abort
  ${EndIf}

  ; OS Version Check
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Drift CLI requires Windows 10 or higher."
    Abort
  ${EndIf}

  ; Running App Mutex Check
  System::Call 'kernel32::CreateMutex(i 0, i 0, t "DriftRunningInstanceMutex") i .r0 ?e'
  Pop $R0
  ${If} $R0 == 183 ; ERROR_ALREADY_EXISTS
    MessageBox MB_ICONEXCLAMATION "Drift is currently running. Please close it before proceeding."
    Abort
  ${EndIf}
FunctionEnd
```

### B. Installing Prerequisites (e.g., Visual C++ Redistributable)

#### Inno Setup (`Check` Parameter & `[Run]`)
```ini
[Files]
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsVCRedist

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
    Flags: waituntilterminated; Check: NeedsVCRedist

[Code]
function NeedsVCRedist(): Boolean;
var
  InstalledVersion: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', InstalledVersion) then
  begin
    if InstalledVersion = 1 then
      Result := False;
  end;
end;
```

#### NSIS (Conditional Section Execution)
```nsis
Section "-InstallVCRedist"
  ReadRegDWORD $0 HKLM "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" "Installed"
  ${If} $0 != 1
    DetailPrint "Installing Visual C++ Redistributable..."
    InitPluginsDir
    File /oname=$PLUGINSDIR\vcredist.exe "redist\vc_redist.x64.exe"
    ExecWait '"$PLUGINSDIR\vcredist.exe" /install /quiet /norestart' $1
    DetailPrint "VC++ Redistributable exited with code: $1"
  ${EndIf}
SectionEnd
```

---

## 8. Lifecycle Hooks & Event Functions

### Lifecycle Hook Equivalence Matrix

| Lifecycle Stage | Inno Setup (`[Code]` Event Function) | NSIS (`.nsi` Callback Function) |
| :--- | :--- | :--- |
| **Pre-Flight Initialization** | `InitializeSetup(): Boolean` | `Function .onInit` |
| **GUI Ready / Wizard Init** | `InitializeWizard()` | `Function .onGUIInit` |
| **Directory Verification** | `NextButtonClick(wpSelectDir): Boolean` | `Function .onVerifyInstDir` |
| **Pre-File Extraction** | `CurStepChanged(ssPreInstall)` | `Function Callback / First Section` |
| **Post-File Extraction** | `CurStepChanged(ssPostInstall)` | `Function .onInstSuccess` |
| **Setup Aborted / Failed** | `DeinitializeSetup()` | `Function .onInstFailed` |
| **Uninstall Pre-Flight** | `InitializeUninstall(): Boolean` | `Function un.onInit` |
| **Uninstall Teardown** | `CurUninstallStepChanged(usUninstall)` | `Section "Uninstall"` |
| **Uninstall Complete** | `CurUninstallStepChanged(usPostUninstall)` | `Function un.onUninstSuccess` |

### Concrete Implementation Examples

#### Inno Setup Lifecycle Hooks
```pascal
[Code]
function InitializeSetup(): Boolean;
begin
  Log('Setup engine initialized.');
  Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if Pos(' ', ExpandConstant('{app}')) > 0 then
      MsgBox('Warning: Installing into a path with spaces is discouraged.', mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  case CurStep of
    ssPreInstall:
      Exec('net.exe', 'stop DriftService', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    ssPostInstall:
      Exec('net.exe', 'start DriftService', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    DelTree(ExpandConstant('{userappdata}\Drift\cache'), True, True, True);
end;
```

#### NSIS Lifecycle Hooks
```nsis
Function .onInit
  DetailPrint "Initializing installer pre-flight checks..."
FunctionEnd

Function .onInstSuccess
  MessageBox MB_OK "Drift CLI has been successfully installed!"
FunctionEnd

Function .onInstFailed
  MessageBox MB_ICONSTOP "Installation failed. Reverting changes..."
FunctionEnd

Function un.onInit
  MessageBox MB_YESNO "Are you sure you want to completely remove Drift CLI?" IDYES +2
  Abort
FunctionEnd

Function un.onUninstSuccess
  MessageBox MB_OK "Drift CLI was successfully removed from your computer."
FunctionEnd

Section "Uninstall"
  Delete "$INSTDIR\drift.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$LOCALAPPDATA\Drift\cache"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Drift"
  DeleteRegKey HKLM "Software\Drift"
SectionEnd
```

---

## 9. Historical Rationale: Why Custom Scripting Engines?

The decision to create custom domain-specific languages (DSLs) and embedded bytecode engines—rather than embedding mainstream scripting languages like Python, Perl, VBScript, or Lua—was dictated by late-1990s hardware and network constraints.

### 1. Network & Memory Realities (1997–2002)
* **56k Dial-Up Modems**: Download speeds averaged **3–5 KB/s**. The entire Winamp application (for which NSIS was created) was **1.5 MB**. Adding 500 KB of installer overhead was unacceptable.
* **Hardware Footprints**: Target machines had **32 MB to 128 MB of RAM** and single-core Pentium II/III CPUs. Dynamic language interpreters added unacceptable startup latency and memory overhead.
* **Zero Dependency Invariant**: An installer must execute reliably on an unpatched Windows 95/98/NT system before any runtime libraries or updates are installed.

### 2. Why Mainstream Languages Were Infeasible

* **Python / Perl / Tcl**:
  * Added **5 MB to 15 MB** of runtime DLLs and standard library files, multiplying the total download size by 4x to 10x.
  * Relied on specific Visual C++ runtime versions (`MSVCRT.DLL`) that were frequently absent on clean systems.
* **VBScript / JScript (Windows Script Host)**:
  * Not pre-installed on Windows 95 or initial releases of Windows NT 4.0 (required Internet Explorer 4.0+).
  * System administrators routinely disabled WSH (`wscript.exe` / `cscript.exe`) to mitigate late-90s macro viruses (*ILOVEYOU*, *Melissa*).
  * Could not invoke raw Win32 APIs without registering external COM DLLs (a circular dependency for an installer).
* **Lua**:
  * In 1999 (Lua 3.2/4.0), Lua lacked built-in Win32 GUI dialog bindings, Unicode support, and Windows registry primitives. Writing a C glue layer for Win32 was equivalent to building a dedicated installer VM.
* **Java**:
  * Required a 30–50 MB JRE. Java-based installers were slow, memory-intensive, and lacked native Win32 UI fidelity.

### 3. The Design Strengths of NSIS & Inno Setup

* **NSIS (Nullsoft Demoscene Philosophy)**:
  * Packed the entire runtime stub—LZMA decompressor, Win32 GUI manager, registry engine, and stack-based bytecode VM—into **less than 34 KB**.
  * Linked directly to the four baseline Windows DLLs (`kernel32.dll`, `user32.dll`, `advapi32.dll`, `gdi32.dll`), guaranteeing zero runtime dependency errors.
* **Inno Setup (Delphi & RemObjects Pascal Script)**:
  * Borland Delphi produced standalone native Win32 executables without requiring runtime support DLLs.
  * Integrated **RemObjects Pascal Script**, a type-safe bytecode interpreter adding only **~150 KB** overhead.
  * Provided strong compile-time type safety for installation event handlers and native access to Delphi GUI components.

---

## 10. Comprehensive Comparison: NSIS vs. Inno Setup vs. MSI vs. MSIX

| Dimension | NSIS | Inno Setup | Windows Installer (MSI) | MSIX |
| :--- | :--- | :--- | :--- | :--- |
| **Package Format** | Monolithic PE (`.exe`) | Monolithic PE (`.exe` + `.bin`) | Relational Database (`.msi`) | ZIP64 Container (`.msix`) |
| **Engine Footprint** | ~34 KB stub | ~500 KB–1 MB stub | OS Service (`msiserver.exe`) | Kernel VFS Driver |
| **Scripting Model** | Low-level Stack Bytecode | INI Tables + Pascal Script | SQL Tables + C/C++ CustomActions | Declarative XML (`AppxManifest.xml`) |
| **Compression** | Solid / Non-Solid (LZMA, Bzip2) | Solid / Non-Solid (LZMA2, Deflate) | Cabinet (`.cab`) MSZIP / LZX | Deflate + 64 KB Block Maps |
| **State Tracking** | Scripted procedural removal | Automatic Binary Log (`unins000.dat`) | Host MSI Database + Component GUIDs | Native OS Container Teardown |
| **Disk Spanning** | Plugin-dependent | Native multi-volume slicing | Native multi-CAB spanning | HTTP Byte-Range Streaming |
| **Process Concurrency** | Independent processes | Independent processes | Single global install mutex (1618) | Concurrent container deployment |
| **Atomic Rollback** | Manual error handling | Reverses `unins000.dat` records | Two-Phase Transaction (`.rbf` files) | Atomic directory cleanup |
