# 🚫 Drift Ignore Engine: Syntax and Integration

Drift includes a robust, PCRE-based file ignore engine to prevent system directories, 
transient runtime logs, or junk files from cluttering your repository state.

## 1. Syntax of `.drift_ignore`
Each package can have exactly one `.drift_ignore` file placed at its root:
*   **Format**: One Perl-Compatible Regular Expression (PCRE) pattern per line. Blank lines and lines starting with `#` are skipped.
*   **Enforcement**: Drift strictly blocks multiple or nested ignore files (e.g., subfolder-specific ignores) to maintain a single, transparent source of ignore truth.
*   **Match Timing**: Patterns are evaluated against relative package paths *before* prefix expansion is applied (e.g. matching `dot-bashrc` instead of `.bashrc`). This guarantees that rename and prefix translation engines do not bypass matching rules.

Example `.drift_ignore`:
```pcre
# Ignore all vim/neovim swap, backup, and undo files
.*\\.sw[p-z]$
.*~
.*\\.un~$

# Ignore temporary local logs
logs/.*\\.log
```

## 2. Install Ignore Logic
During compilation and deployment (`drift deploy` / `drift stage` / `drift apply` / `drift status` / `drift diff`):
*   Any files under `src/` matching `.drift_ignore` patterns are **skipped** during rendering.
*   They are never copied or symlinked onto your active host system.
*   They are excluded from change staging, meaning they will not generate additions or deletions.

## 3. Integration with Fully-Controlled Directory (FCD) Ignore Mechanism
A Fully-Controlled Directory (FCD) is a directory where Drift has total tracking governance, meaning files deleted on the host inside an FCD are cleaned up. 

When untracked files are created on the host inside an FCD:
1.  **Detection & Reverse-Sync (F1)**:
    *   Normally, untracked host files are completely ignored by dotfile managers.
    *   Inside FCDs, however, `reverse-sync` automatically tracks *every single untracked file addition* and reverse-copies it back to your `install/` state base.
    *   **However, if the file matches any pattern inside `.drift_ignore`, it is completely skipped during `reverse-sync`**. It is not reverse-copied to `install/`.
2.  **Adoption Integration (F12)**:
    *   During interactive drift adoption (`drift adopt <pkg> --interactive` or `-i`), if you choose **Option [2] Ignore** on an untracked FCD file, Drift will:
        *   **State Unlink**: Remove the file from the `install/` base (restoring Git state database cleanliness).
        *   **Pattern Append**: Automatically append the relative file path pattern to the package's `.drift_ignore` file.
    *   Because the pattern is appended to `.drift_ignore`, in all future `reverse-sync` runs, the ignore engine sees that the host file is ignored.
    *   As a result, the file is safely kept on your active host system but is completely ignored by the FCD tracking, leaving your repository pristine!

👉 Run `drift help fcd` to learn more about the purpose and scenarios of Fully-Controlled Directories.
