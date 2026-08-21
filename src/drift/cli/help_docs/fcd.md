# 📁 Fully-Controlled Directories (FCDs): Tracking Active File Creation

A **Fully-Controlled Directory (FCD)** is a target-system subdirectory declared in your 
package configuration where Drift has absolute, bidirectional tracking governance.

## 1. Why do we need FCDs? (The Purpose)
By default, traditional dotfile managers only track files that you explicitly declare 
in your source repository. If an application (like a web browser, a GUI editor, or a BitTorrent 
client) dynamically creates new directories, plug-ins, or local preset files in the 
destination directory, they remain untracked.

This creates several issues:
1.  **Orphaned Accumulation**: When you delete or disable a package, these dynamically created files 
    remain on your host, leaving stale debris on your system.
2.  **No Visibility**: You have no idea if an application modified your configuration folder 
    structure by creating new files or presets.

Drift's FCD engine solves this by turning specific directories into fully-audited, 
total-governance zones.

## 2. Declaring FCDs
To declare an FCD, use the `fully_controlled_dirs` option under `[package]` in your `package.toml`:
```toml
# src/qbittorrent/package.toml
[package]
name = "qbittorrent"
install_method = "copy"
target_directory = "~/.config/qBittorrent"
fully_controlled_dirs = [
    "themes",
    "categories"
]
```

## 3. How FCDs Work (The Data Flow Scenario)

### Step A: Dynamic File Creation on Host
Imagine you open your qBittorrent application, and download a custom dark theme. The GUI 
automatically writes a brand-new file onto your system:
`~/.config/qBittorrent/themes/dark.qbtheme`

### Step B: Tracking via Reverse-Sync (F1)
When you run `drift status` or `drift reverse-sync`, Drift detects that `themes/` is a 
Fully-Controlled Directory:
*   It identifies that `themes/dark.qbtheme` does not exist in your source repository but 
    exists on your system.
*   **Reverse-Sync action**: It automatically reverse-copies `dark.qbtheme` back to your 
    local state base `install/qbittorrent/themes/dark.qbtheme` as an uncommitted change!

### Step C: Interactive Adoption (F12)
When you run `drift adopt qbittorrent --interactive` or `-i`, Drift presents you with 
an interactive reconciliation prompt:
```bash
Found untracked file addition inside Fully-Controlled Directory: ~/.config/qBittorrent/themes/dark.qbtheme
------------------------------------------------------------
New physical file created on host system.
------------------------------------------------------------

Reconciliation options:
[1] Adopt and copy into source package (Creates src/qbittorrent/dot-config/qBittorrent/themes/dark.qbtheme)
[2] Ignore file (Unlinks from install/, appends 'themes/dark.qbtheme' to .drift_ignore to prevent future reverse-sync)
[3] Discard file (Stages file to install/ database so it is deleted from host on next deployment)
[4] Skip file
```

#### Choose Option [1] Adopt:
*   Drift copies the file into `src/qbittorrent/themes/dark.qbtheme`, making it part of your 
    declarative, version-controlled source tree!

#### Choose Option [2] Ignore:
*   Drift removes the file from `install/`, and appends the pattern `themes/dark.qbtheme` to 
    `.drift_ignore`. It stays on your host, but is never reverse-copied or tracked again.

#### Choose Option [3] Discard:
*   Drift keeps the file staged as tracked in `install/` but absent from the rendered templates. 
    On your next `drift deploy` or `drift stage`, the engine automatically recognizes it as 
    an orphaned file and **deletes it from the host system**, restoring pristine alignment!

👉 Run `drift help ignore` to learn more about the PCRE ignore patterns syntax.
