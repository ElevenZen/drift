# Missing File Handling in Common File Stream Editors & Processors

This document provides a comprehensive technical reference for how command-line stream editors, text processors, and scripting utilities (**Perl**, **GNU `sed`**, **AWK / `gawk`**, **`grep`**, and **Python `fileinput`**) handle missing or unreadable files. It details their internal filehandle mechanics, default exit codes, multi-file stream semantics, and patterns for enforcing fatal vs. non-fatal behaviors.

---

## 1. Executive Summary & Comparison Matrix

When tools process filenames passed via command-line arguments (`ARGV`), their default behaviors diverge significantly based on their historical origins:
* **Perl (`-pe`/`-ne`)**: Designed for resilient batch text processing; treats unreadable files in `<>` as non-fatal warnings and **exits with `0`** by default.
* **`sed` (GNU `sed`)**: Attempts to process all arguments; logs errors for missing files and **exits with `2`**.
* **`awk` (POSIX / `gawk`)**: Treats unreadable input files as fatal errors by default; halts immediately and **exits with `2`** (unless intercepted via `BEGINFILE`).
* **`grep`**: Fails with **exit code `2`** on missing files (reserving `1` for "no match found" and `0` for "match found").

| Tool | Invocation Pattern | Missing File Default Behavior | Default Exit Code | How to Enforce Fatal Failure (Non-zero Exit) | How to Gracefully Skip / Ignore Missing Files |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Perl** | `perl -pe '...' file` | Emits non-fatal warning, skips file | **`0`** | `perl -Mwarnings=FATAL,all -pe '...'` | *(Default behavior)* |
| **GNU `sed`** | `sed '...' file` | Prints error to stderr, processes remaining files | **`2`** | *(Default behavior)* | `[ -f "$f" ] && sed ...` or `2>/dev/null \|\| true` |
| **GNU `awk`** | `awk '...' file` | Fatal error, aborts pipeline immediately | **`2`** | *(Default behavior)* | `BEGINFILE { if (ERRNO) nextfile }` |
| **`grep`** | `grep 'pat' file` | Prints error, continues remaining files | **`2`** | *(Default behavior)* | `grep -s 'pat' file` (suppresses message, still exits `2` if no match) |
| **Python** | `python3 -m fileinput` | Raises `FileNotFoundError` exception | **`1`** | *(Default behavior)* | `try: ... except FileNotFoundError:` |

---

## 2. Perl (`perl -pe`, `perl -ne`, and Scripts)

### A. The Diamond Operator (`<>`) Mechanism
When invoking Perl with `-p` (print loop) or `-n` (non-printing loop), the interpreter wraps code in an implicit `while (<>)` loop:

```perl
# Conceptual internal expansion of: perl -pe 's/foo/bar/' file1.txt missing.txt
LINE: while (defined($_ = <ARGV>)) {
    s/foo/bar/;
} continue {
    print $_;
}
```

1. **Implicit `open`**: As `<ARGV>` iterates through `@ARGV`, it attempts to open each file.
2. **Non-Fatal Warning**: If `open(ARGV, $ARGV[0])` fails (e.g. `ENOENT: No such file or directory`), Perl emits a runtime warning via `warn`:
   ```text
   Can't open missing.txt: No such file or directory.
   ```
3. **Clean Termination**: Perl shifts the failed filename off `@ARGV` and moves to the next file (or terminates the loop if empty). Because no fatal exception (`die`) was raised, the process terminates normally with **exit code `0`**.

### B. Enforcing Fatal Failure on Missing Files

#### 1. Turn Warnings into Fatal Exceptions (Recommended for One-Liners)
Use `-Mwarnings=FATAL,all` to escalate all warnings to fatal runtime errors:

```bash
perl -Mwarnings=FATAL,all -pe 's/foo/bar/' missing.txt
# Stderr: Can't open missing.txt: No such file or directory.
# Exit Code: 2
```

#### 2. Convert Warnings to `die` via Signal Handler
```bash
perl -e 'BEGIN { $SIG{__WARN__} = sub { die @_ } }' -pe 's/foo/bar/' missing.txt
# Exit Code: 2
```

#### 3. In Standalone Perl Scripts
In standalone scripts, standard `open` returns false on failure without throwing an exception. Use `or die` or `use autodie`:

```perl
# Idiomatic explicit check
open(my $fh, '<', $filename) or die "Cannot open '$filename': $!\n";

# Modern automatic exception escalation
use autodie;
open(my $fh, '<', $filename); # Throws autodie exception with non-zero exit on failure
```

---

## 3. Stream Editor (`sed` / GNU `sed`)

### A. Execution Semantics
GNU `sed` processes all input files specified on the command line sequentially.

```bash
sed 's/old/new/' missing.txt
# Stderr: sed: can't read missing.txt: No such file or directory
# Exit Code: 2
```

#### Multi-File Batch Processing:
When provided multiple files (e.g., `sed -i 's/foo/bar/g' fileA.txt missing.txt fileB.txt`):
1. `sed` successfully edits `fileA.txt`.
2. `sed` reports `can't read missing.txt: No such file or directory` to `stderr`.
3. `sed` continues and edits `fileB.txt`.
4. At process termination, `sed` returns **exit code `2`** because an error occurred during execution.

### B. Gracefully Skipping Missing Files in `sed`
Because `sed` lacks internal conditional exception-handling constructs, missing-file filtering should be handled in the calling shell:

```bash
# Pattern 1: Shell existence test
[ -f "$file" ] && sed -i 's/foo/bar/' "$file"

# Pattern 2: Null redirection with fallback
sed 's/foo/bar/' "$file" 2>/dev/null || true

# Pattern 3: Using find to guarantee only existing files are passed
find ./src -type f -name "*.conf" -exec sed -i 's/foo/bar/' {} +
```

---

## 4. AWK / GNU `awk` (`gawk`)

### A. Default Behavior (Immediate Fatal Abort)
Standard POSIX `awk` and GNU `awk` treat unreadable command-line files as fatal errors:

```bash
awk '{print $1}' missing.txt
# Stderr: awk: fatal: cannot open file `missing.txt' for reading: No such file or directory
# Exit Code: 2
```
Unlike `sed`, `awk` **immediately aborts** execution upon encountering the first missing file and will not process subsequent files in the argument list.

### B. Catching and Skipping Missing Files in GNU `awk` (`gawk`)
GNU `awk` supports the `BEGINFILE` rule and the `ERRNO` variable, allowing scripts to inspect file-open errors and skip unreadable files cleanly using `nextfile`:

```awk
awk '
BEGINFILE {
    if (ERRNO) {
        # Log warning to stderr and cleanly advance to next file without fatal exit
        print "Warning: Skipping unreadable file: " FILENAME > "/dev/stderr"
        nextfile
    }
}
{
    # Regular record processing
    print $0
}' file1.txt missing.txt file2.txt
```

**Execution Result**:
* `file1.txt` is processed.
* `missing.txt` is caught and skipped.
* `file2.txt` is processed.
* Script terminates with **exit code `0`**.

---

## 5. Pattern Matching (`grep` / `ripgrep`)

### A. `grep` (GNU grep)
`grep` uses distinct exit status codes:
* `0`: Selected lines were found.
* `1`: No lines were selected.
* `2`: An error occurred (e.g., file not found, permission denied).

```bash
grep "pattern" missing.txt
# Stderr: grep: missing.txt: No such file or directory
# Exit Code: 2

# Suppressing error messages with -s / --no-messages
grep -s "pattern" missing.txt
# Stderr: (silent)
# Exit Code: 2 (if file missing and no matches found)
```

### B. `ripgrep` (`rg`)
`ripgrep` outputs warnings to `stderr` on missing files but continues searching other targets:
```bash
rg "pattern" missing.txt
# Stderr: missing.txt: No such file or directory (os error 2)
# Exit Code: 2 (if unreadable files encountered and no match found)

# Suppressing error messages
rg --no-messages "pattern" missing.txt
```

---

## 6. Shell Pipeline & Automation Integration Best Practices

In automated systems (such as **Drift** lifecycle hooks or CI/CD pipelines), unchecked non-zero exit codes or false zero exits can cause silent pipeline corruption.

### A. Strict Shell Pipeline Flags
Always enforce strict error checking in bash hook scripts:

```bash
#!/usr/bin/env bash
set -euo pipefail

# -e: Exit immediately if a command exits with a non-zero status.
# -u: Treat unset variables as an error.
# -o pipefail: Pipeline exit code reflects the rightmost non-zero exit.
```

### B. Safe Stream Editing Checklist

1. **When using Perl for in-place replacements in hooks**:
   ```bash
   # UNSAFE: Exits 0 if $TARGET_FILE is missing
   perl -pi -e 's/foo/bar/' "$TARGET_FILE"

   # SAFE: Fails fast with exit code 2 if $TARGET_FILE is missing
   perl -Mwarnings=FATAL,all -pi -e 's/foo/bar/' "$TARGET_FILE"
   ```

2. **When using `sed -i`**:
   ```bash
   # SAFE under 'set -e': Fails with exit code 2 if file is missing
   sed -i 's/foo/bar/' "$TARGET_FILE"
   ```

3. **When using `awk`**:
   ```bash
   # SAFE: Aborts with exit code 2 on missing file
   awk '{print $1}' "$TARGET_FILE"
   ```
