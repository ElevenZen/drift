# Package Lifecycle & Hook Systems: Comparative Analysis of DEB, RPM, ALPM (ZST), and Drift

This document provides a comprehensive architectural analysis of **package lifecycle hooks, scriptlets, and trigger mechanisms** across the three major Linux distribution package systems (**DEB**, **RPM**, **ALPM / Arch ZST**) and the **Drift declarative state engine**.

It explores their internal execution lifecycles, event dispatch models, context binding, batch aggregation strategies, performance characteristics, advantages, and limitations.

---

## 1. Executive Summary & Paradigm Overview

Package managers must execute auxiliary actions during software installation, upgrades, and removals—such as updating font caches, registering shared libraries, compiling schemas, restarting systemd daemons, and configuring user environments.

Each packaging ecosystem developed a distinct paradigm to address these needs:

* **Defensive State Machine (DEB)**: `dpkg` / `apt` maintainer scripts (`preinst`, `postinst`, `prerm`, `postrm`) with multi-phase transaction journaling, Debconf configuration separation, and high-level APT invoke hooks.
* **Multi-Layer Enterprise Triggers (RPM)**: `rpm` / `dnf` scriptlets (`%pre`, `%post`), cross-package triggers (`%triggerin`), batched transaction file triggers (`%transfiletriggerin`), and embedded in-process Lua (`-p <lua>`).
* **Passive Declarative Event Triggers (ALPM)**: `pacman` / Arch ZST declarative `.hook` files matching filesystem path globs or package names, consolidated at `PostTransaction`.
* **Host-Adaptive Phased Lifecycle (Drift)**: Declarative `[hooks]` inside `drift_hooks/`, multi-engine hook templating, host fact injection, capability `probe` gates, and continuous `health` diagnostics.

| Dimension | DEB (`dpkg` / `apt`) | RPM (`rpm` / `dnf`) | ALPM (`pacman` / `zst`) | Drift (`src/<pkg>/`) |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Paradigm** | Imperative scriptlets + sequential triggers | Multi-layered scriptlets & triggers | Passive `.hook` event triggers | Phased pipeline lifecycle |
| **Trigger Mechanism** | Maintainer scripts + `activate` triggers | Scriptlets, `%triggerin`, `%transfiletriggerin` | Path / package glob matching | Phased lifecycle events (`[hooks]`) |
| **Batch Aggregation** | Partial (multi-phase trigger cycles) | Native (`%transfiletriggerin -P`) | Native (`PostTransaction` + `stdin`) | Package-scoped (`.drift/hooks/`) |
| **Runtime Environment** | `/bin/sh` or `/bin/dash` subshells | Shell or in-process Lua (`-p <lua>`) | External binaries / scripts | Scripts, binaries, Python (`drift_*.py`) |
| **Hook Templating** | ❌ Static scriptlets | ❌ Static spec macros | ❌ Static text files | ✅ Multi-engine (`.envst`, `.j2`, `.mustache`) |
| **Context Binding** | Positional args (`$1` action, `$2` ver) | Positional arg (`$1` instance count) | Target paths via `stdin` (`NeedsTargets`) | `$drift_*` facts & 6-tier `[env.*]` |
| **Execution Safety** | No timeouts; `fsync` barriers | No timeouts; priority order (`-P`) | No timeouts; lexicographical order | Per-hook timeouts, fail-fast, `--dry-run` |
| **Core Sweet Spot** | LTS server upgrades & `debconf` | Enterprise multi-pkg & plugin orchestration | Fast rolling-release batch updates | Host-adaptive configs & dotfiles |

* **Trigger & Batching Notes**:
  * **DEB**: Combines per-package maintainer scripts with explicit `interest`/`activate` triggers processed across intermediate `dpkg` cycles.
  * **RPM**: Supports package-to-package reactive hooks (`%triggerin`), path-glob triggers (`%transfiletriggerin` ordered by `-P` priority), and zero-fork embedded Lua (`-p <lua>`).
  * **ALPM**: Consolidates matched path/package targets across the entire transaction and streams them once via `stdin` (`NeedsTargets`) at `PostTransaction`.
  * **Drift**: Compiles hook templates inside `drift_hooks/` into `.drift/hooks/`, injecting host/package facts and enforcing per-hook timeouts across phased lifecycle gates (`probe`, `pre_source`, `post_render`, `pre/post_install`, `health`).

---

## 2. Structural & Execution Timeline Comparison

### 1. DEB / Dpkg Execution Timeline
* `APT::Update / Pre-Invoke` $\rightarrow$ Download `.deb` & `DPkg::Pre-Install-Pkgs`
* $\rightarrow$ Execute `preinst`
* $\rightarrow$ Unpack data files to `/`
* $\rightarrow$ Execute `postinst` / Debconf
* $\rightarrow$ Run pending Dpkg trigger cycles (sequential)
* $\rightarrow$ `DPkg::Post-Invoke`

### 2. RPM Execution Timeline
* Evaluate `%pretrans` $\rightarrow$ Execute `%pre`
* $\rightarrow$ Unpack payload files to `/`
* $\rightarrow$ Execute `%post` & `%triggerin`
* $\rightarrow$ Execute `%transfiletriggerin` (aggregated via `stdin`)
* $\rightarrow$ Execute `%posttrans` (end of transaction)

### 3. Arch ALPM (Pacman) Timeline
* `libarchive` stream unpack to `/`
* $\rightarrow$ Match modified paths/packages against `.hook` catalog
* $\rightarrow$ Execute `PostTransaction` hooks (batched once via `stdin`)

### 4. Drift Lifecycle Pipeline
* Evaluate `[requirements]` & `probe` hook
* $\rightarrow$ Trigger `pre_source` (optional dynamic compilation)
* $\rightarrow$ Render templates to `render/` (`render_package`)
* $\rightarrow$ Trigger `post_render` hook
* $\rightarrow$ Stage to `install/` (Git commit snapshot)
* $\rightarrow$ Trigger `pre_install` / `pre_update`
* $\rightarrow$ Deploy target (Native symlink / copy)
* $\rightarrow$ Trigger `post_install` / `post_update`
* $\rightarrow$ Continuous `health` diagnostics

---

## 3. Debian (`.deb` / `dpkg` / `apt`) Hook Architecture

Debian’s package management ecosystem operates across two distinct layers: low-level package operations managed by `dpkg`, and high-level transaction orchestration managed by `apt`.

### A. Taxonomy & Hook Classification

1. **Category 1: Package Maintainer Scripts (`dpkg`)**
   * **Installation & Configuration**:
     * `preinst`: Runs before files are unpacked from `data.tar.xz`.
     * `postinst`: Configures the package after files are unpacked to `/`.
   * **Removal & Decommissioning**:
     * `prerm`: Prepares package for removal or upgrade before files are deleted.
     * `postrm`: Cleans up state after files are removed (supports `purge` to erase configuration files).

2. **Category 2: Declarative Dpkg Triggers**
   * **Path Subscriptions**: `interest`, `interest-noawait`, `interest-await` (subscribes to changes in directory paths like `/usr/share/man`).
   * **Explicit Activations**: `activate`, `activate-noawait`, `activate-await` (manually signals a named trigger).

3. **Category 3: High-Level APT Transaction Hooks (`/etc/apt/apt.conf.d/`)**
   * **Transaction Envelopes**: `DPkg::Pre-Invoke` and `DPkg::Post-Invoke` (e.g. running `etckeeper` or Snapper/Timeshift filesystem snapshots before/after package transactions).
   * **Package Stream Pipelines**: `DPkg::Pre-Install-Pkgs` (streams lists of `.deb` files via pipes to tools like `apt-listchanges` or `apt-listbugs`).
   * **Repository Update Hooks**: `APT::Update::Pre-Invoke` and `APT::Update::Post-Invoke`.

4. **Category 4: Interactive Configuration Management Layer**
   * **`debconf`**: Standardized database separating user configuration prompts (`config`, `templates`) from maintainer script execution.

### B. Dpkg Triggers: Declarative File & Named Subscriptions

Prior to Dpkg Triggers (introduced in dpkg 1.14), every package updating shared system resources (e.g. `update-initramfs`, `ldconfig`, `install-info`, `update-mime-database`, `gtk-update-icon-cache`) imperatively ran those tools in its own `postinst`. During an upgrade of 100 packages, `update-initramfs` or `ldconfig` would run 100 consecutive times.

Dpkg Triggers solve this by decoupling the **activator** from the **handler**, allowing dpkg to defer execution and aggregate runs.

#### 1. Directives in `debian/triggers`
A package registers its trigger subscriptions or activations in a `triggers` control file:

```text
# --- CONSUMER (Interested in changes) ---
# Subscribes to changes under /usr/share/icons without blocking the activating package:
interest-noawait /usr/share/icons

# Explicit named trigger subscription:
interest-noawait update-initramfs

# --- PRODUCER (Activates a trigger) ---
# Explicitly signals an external trigger without waiting:
activate-noawait update-initramfs
```

#### 2. The Critical Distinction: `await` vs. `noawait`
* **`interest-await` / `activate-await` (Historical Default)**:
  * When Package A modifies a path matching an `await` trigger in Package B, Package A **cannot transition to the `installed` state** until Package B has successfully executed its trigger and transitioned to `installed`.
  * *The Architectural Flaw*: If Package A and Package B depend on each other, or if a third package depends on Package A being fully installed, `await` triggers frequently caused circular dependency deadlocks during major distribution upgrades (`apt dist-upgrade`).
* **`interest-noawait` / `activate-noawait` (Modern Standard)**:
  * Package A is permitted to transition immediately to `installed` as soon as its own files are extracted.
  * Package B is marked as `triggers-pending` and its handler runs asynchronously in a deferred batch at the end of the transaction cycle.

#### 3. Execution Protocol: `postinst triggered`
When dpkg processes pending triggers for a package, it invokes that package's `postinst` script with `$1 = triggered` and `$2 = "<space-separated-list-of-activated-triggers>"`:

```bash
#!/bin/sh
# /var/lib/dpkg/info/shared-mime-info.postinst
set -e

case "$1" in
    configure)
        # Standard initial installation / upgrade configuration
        update-mime-database /usr/share/mime
        ;;
    triggered)
        # Invoked by dpkg trigger processing
        for trigger in $2; do
            case "$trigger" in
                /usr/share/mime)
                    update-mime-database /usr/share/mime
                    ;;
            esac
        done
        ;;
    abort-upgrade|abort-remove|abort-deconfigure)
        ;;
    *)
        echo "postinst called with unknown argument \`$1'" >&2
        exit 1
        ;;
esac
```

#### 4. Dpkg State Machine Integration
Dpkg tracks trigger processing directly in `/var/lib/dpkg/status` through dedicated status states:
* `triggers-pending`: The package has pending triggers to execute.
* `triggers-awaited`: The package is waiting for another package to process its triggers.

---

### C. Debconf: Separation of User Interaction, Configuration & State

In traditional UNIX packaging, scriptlets interacted with users via raw shell `read` prompts. This broke unattended automation, made graphical installers impossible, and prevented automated configuration validation.

**Debconf** solves this by establishing a strict, 3-tier architectural separation:

| Component | Role & Responsibility |
| :--- | :--- |
| **1. `templates` (Schema)** | Declarative question definitions, data types, priorities, and translations. |
| **2. `config` (Controller)** | Probes environment and prompts questions via the Debconf IPC protocol. |
| **3. State Database** | Centralized key-value storage (`/var/cache/debconf/config.dat`). |
| **4. `postinst` (Writer)** | Reads answers from the database and writes actual configuration files. |

#### 1. Declarative Templates (`debian/templates`)
Packages declare questions with explicit data types, default values, and internationalized descriptions:

```text
Template: postfix/main_mailer_type
Type: select
Choices: No configuration, Internet Site, Internet with smarthost, Satellite system, Local only
Default: Internet Site
Description: General type of mail configuration:
 Please select the mail server configuration type that best meets your needs.
```

#### 2. The Debconf IPC Protocol (Controller <-> Frontend)
The `config` script communicates with the active Debconf frontend over standard input and output using a text-based protocol:

```bash
#!/bin/sh
# debian/config
set -e
. /usr/share/debconf/confmodule

# 1. Ask question with priority 'high'
db_input high postfix/main_mailer_type || true
db_go

# 2. Retrieve user response into $RET
db_get postfix/main_mailer_type
MAILER_TYPE="$RET"

# 3. Dynamically ask follow-up question if 'Internet Site' was selected
if [ "$MAILER_TYPE" = "Internet Site" ]; then
    db_input high postfix/mailname || true
    db_go
fi
```

#### 3. Pluggable Frontend Drivers
Because question logic is completely decoupled from presentation, Debconf dynamically adapts to the execution environment:
* **`dialog`**: Interactive ncurses terminal interface (used in server SSH sessions).
* **`readline`**: Plain text serial console prompt.
* **`gnome` / `kde`**: Native GTK / Qt modal dialogs in graphical desktop environments.
* **`noninteractive`**: Completely silent headless mode. Questions with defaults are accepted automatically; questions without defaults use preseeded database values or fallbacks.

#### 4. Headless Cloud Preseeding & Infrastructure as Code
In automated enterprise deployments (Cloud-Init, Docker, Ansible, Terraform), administrators can inject answers into the Debconf database *prior* to package installation:

```bash
# Pre-seed configuration values
debconf-set-selections <<EOF
postfix postfix/main_mailer_type select Internet Site
postfix postfix/mailname string mail.example.com
EOF

# Install completely unattended with zero prompts
DEBIAN_FRONTEND=noninteractive apt-get install -y postfix
```

During package configuration, `postfix.postinst` executes `db_get postfix/mailname`, reads `mail.example.com`, and writes `/etc/postfix/main.cf` without user intervention.

---

### D. Advantages
1. **Unrivaled Transactional Resilience**: Rigid state machine transitions and multi-phase journaling enable seamless recovery (`dpkg --configure -a`) from mid-transaction power loss or kernel crashes without database corruption.
2. **Standardized Headless Preseeding (`debconf`)**: Decouples user interaction from script execution, providing a clean database for automated enterprise provisioning (`DEBIAN_FRONTEND=noninteractive`).
3. **Precise Lifecycle State Guards**: Granular actions (`prerm remove`, `postrm purge`) allow distinct logic for package upgrades versus complete data purges.

### E. Limitations & Disadvantages
1. **Subprocess Fork Storms**: Spawns hundreds of individual `/bin/sh` or `/bin/dash` processes sequentially during large multi-package updates, creating severe CPU bottlenecks.
2. **Heavy I/O Sync Barriers**: Defensive per-file `fsync()` calls introduce massive disk I/O stalls and write amplification on mechanical and flash storage.
3. **Fragmented Trigger Batches**: Dpkg triggers run in multiple intermediate sub-cycles throughout an upgrade, frequently restarting daemons or reloading indexes repeatedly.

---

## 4. Red Hat RPM (`.rpm` / `dnf`) Hook Architecture

RPM features a multi-tiered, highly structured hook architecture spanning package-level lifecycle scriptlets, inter-package cross-triggers, batched transaction file triggers, an embedded in-process Lua engine, and high-level DNF transaction plugins.

### A. Taxonomy & Hook Classification

1. **Category 1: Package-Level Lifecycle Scriptlets**
   * **Per-Package Unpack/Erase Phase**:
     * `%pre`: Runs immediately before package files are unpacked (receives `$1` instance count).
     * `%post`: Runs immediately after package files are unpacked.
     * `%preun`: Runs before package files are removed during uninstall or upgrade.
     * `%postun`: Runs after package files are removed.
   * **Global Transaction Envelope Phase**:
     * `%pretrans`: Runs before the entire RPM transaction begins (before any packages unpack, `$1 = 0`).
     * `%posttrans`: Runs after the entire RPM transaction completes (after all packages, scriptlets, and file triggers finish, `$1 = 0`).

2. **Category 2: Inter-Package Triggers (Cross-Package Inversion of Control)**
   * **Addon Awakening & Synchronization**:
     * `%triggerin -- <pkg>`: Runs when foreign `<pkg>` is installed or upgraded while this package is present on the system.
     * `%triggerprein -- <pkg>`: Runs before foreign `<pkg>` is installed.
   * **Addon Cleanup & Detachment**:
     * `%triggerun -- <pkg>`: Runs before foreign `<pkg>` is uninstalled.
     * `%triggerpostun -- <pkg>`: Runs after foreign `<pkg>` has been uninstalled.

3. **Category 3: Declarative File & Path Triggers**
   * **Per-Package File Triggers**: `%filetriggerin`, `%filetriggerun`, `%filetriggerpostun` (fire per package modifying matching paths).
   * **Transaction-Level Aggregated File Triggers**: `%transfiletriggerin`, `%transfiletriggerun`, `%transfiletriggerpostun` (batched once across the entire transaction with `-P <priority>`).

4. **Category 4: Embedded In-Process Interpreters**
   * **`-p <lua>`**: Executes scriptlets in-process inside `librpm` process memory with native POSIX bindings, bypassing shell forks and external binary dependencies.

5. **Category 5: High-Level DNF / YUM Transaction Plugins (`/etc/dnf/plugins/`)**
   * **Python Transaction Plugins**: Hooks implementing `pre_transaction()`, `transaction()`, and `post_transaction()` methods.
   * **Action Plugins**: `dnf-plugin-post-transaction-actions` (executes external commands mapped to package/file pattern rules).

---

### B. Package Lifecycle Scriptlets & The Instance Count Protocol (`$1`)

Unlike systems where upgrades are performed by explicitly replacing an existing package in a single pass, RPM implements upgrades by **installing the new version first and removing the old version second**.

To allow scriptlets to distinguish between initial installations, upgrades, and final erasures, RPM passes an **instance counter** as the first positional argument (`$1`).

#### 1. Positional Argument Semantics (`$1`)

| Phase | Scriptlet | `$1` Value | Lifecycle Meaning & Action Context |
| :--- | :--- | :--- | :--- |
| **Initial Install** | `%pre` | `1` | First instance being installed |
| **Initial Install** | `%post` | `1` | Files unpacked; initial config / service enablement |
| **Upgrade (New Pkg)** | `%pre` | `2` (or `>1`) | New version unpacking alongside old version |
| **Upgrade (New Pkg)** | `%post` | `2` (or `>1`) | New version unpacked; reload service / migrate schema |
| **Upgrade (Old Pkg)** | `%preun` | `1` | Old version removal prep (**do NOT stop service**) |
| **Upgrade (Old Pkg)** | `%postun` | `1` | Old files removed; `try-restart` service if needed |
| **Complete Erase** | `%preun` | `0` | Complete removal; stop & disable services |
| **Complete Erase** | `%postun` | `0` | Files erased; purge runtime caches & user state |

#### 2. The Overlapping Upgrade Execution Timeline

The following sequence illustrates what occurs during `rpm -Uvh <pkg>` (or `dnf upgrade <pkg>`):

```text
1. [NEW PKG] %pretrans (0)       -> Runs before transaction starts
2. [NEW PKG] %pre (2)            -> Pre-install checks for new version
3. [NEW PKG] [UNPACK FILES]      -> New files extracted to filesystem
4. [NEW PKG] %post (2)           -> New configuration applied
5. [NEW PKG] %triggerin          -> Addon triggers activated for new package
6. [OLD PKG] %triggerun          -> Addon triggers preparing for old package removal
7. [OLD PKG] %preun (1)          -> Old version cleanup (guarded against stopping services)
8. [OLD PKG] [REMOVE FILES]      -> Old files deleted (files shared with new pkg are preserved)
9. [OLD PKG] %postun (1)         -> Old version final cleanup
10. [OLD PKG] %triggerpostun     -> Addon triggers finalized
11. [NEW PKG] %posttrans (0)     -> Final consolidated transaction hook (e.g. systemctl restart)
```

#### 3. Why the `$1` Guard is Architecturally Critical

Because the new package's files are already on disk and the service may already be running the new code when the old package's `%preun` and `%postun` execute, failing to check `$1` can cause severe outages:

```bash
# Inside package.spec
%preun
if [ $1 -eq 0 ]; then
    # ONLY executed during complete removal, NOT during package upgrade:
    /usr/bin/systemctl --no-reload disable --now my_service.service >/dev/null 2>&1 || :
fi

%postun
if [ $1 -ge 1 ]; then
    # ONLY executed during upgrade: restart the daemon with new binaries:
    /usr/bin/systemctl try-restart my_service.service >/dev/null 2>&1 || :
fi
```

---

### C. Inter-Package Triggers: Inversion of Control for Modular Addons

In traditional packaging, if Package A (e.g. `mod_ssl`) adds functionality to Package B (e.g. `httpd`), either Package B must know about all possible addons, or Package A must execute code whenever Package B is touched.

RPM solves this via **Inter-Package Triggers (`%triggerin`, `%triggerun`)**, establishing an **Inversion of Control** model where the addon package declares hooks that awaken whenever the target package is modified.

#### 1. The Addon Awakening Lifecycle (`%triggerin`)
The addon package embeds the trigger in its own `.spec` file. RPM registers this trigger in the central RPM database (`/var/lib/rpm`). Whenever the target package (`httpd`) is installed or upgraded, RPM queries the database, identifies all registered triggers, and executes them.

#### 2. Trigger Positional Arguments (`$1` and `$2`)
* `$1`: Number of instances of the **triggering package** (the addon) that will remain installed after the transaction.
* `$2`: Number of instances of the **target package** that will remain installed after the transaction.

#### 3. Complete Spec Implementation: Apache SSL Plugin

```spec
Name: mod_ssl
Summary: SSL/TLS module for the Apache HTTP Server
Requires: httpd >= 2.4.0

%description
This package provides SSL/TLS support for the Apache HTTP Server.

# Normal scriptlets for mod_ssl itself:
%post
if [ $1 -eq 1 ]; then
    # Initial install of mod_ssl: generate certificates if absent
    /usr/libexec/httpd-ssl-gencerts >/dev/null 2>&1 || :
fi

# INTER-PACKAGE TRIGGER: Fires whenever httpd is installed or updated:
%triggerin -- httpd
# $1 = mod_ssl instances, $2 = httpd instances
# Automatically test configuration and restart httpd with the new SSL module
if [ $1 -ge 1 ] && [ $2 -ge 1 ]; then
    /usr/sbin/httpd -t >/dev/null 2>&1 && \
    /usr/bin/systemctl try-restart httpd.service >/dev/null 2>&1 || :
fi

# INTER-PACKAGE TRIGGER: Fires when httpd is being uninstalled:
%triggerun -- httpd
# If httpd is being completely removed ($2 == 0), safely stop SSL listeners
if [ $2 -eq 0 ]; then
    /usr/bin/systemctl stop httpd.service >/dev/null 2>&1 || :
fi
```

---

### D. Declarative File Triggers & Transaction-Level Batching

File triggers allow packages to subscribe to filesystem path modifications caused by *any* package in a transaction, eliminating the need for individual packages to call subsystem tools (`fc-cache`, `ldconfig`, `update-desktop-database`).

#### 1. File Triggers vs. Transaction File Triggers

| Directive | Execution Scope | `stdin` Stream | Primary Use Case |
| :--- | :--- | :--- | :--- |
| **`%filetriggerin`** | Per package | Matched paths from that package | Security labeling / immediate compile |
| **`%transfiletriggerin`** | Once per transaction | Deduplicated paths across all packages | Global cache rebuilds (`fc-cache`, `ldconfig`) |

#### 2. Priority Ordering (`-P <priority>`)
Transaction file triggers accept a priority flag `-P <integer>` (default is `1000000`). Triggers with **higher priority values execute before triggers with lower priority values**.

This guarantees that core system indexers (e.g. shared library dynamic links) complete before higher-level user cache updates execute:
* `-P 2000000`: `ldconfig` (registers shared library paths first).
* `-P 1000000`: `systemd-sysusers` and `systemd-tmpfiles`.
* `-P 500000`: Subsystem caches (`gtk-update-icon-cache`, `fc-cache`, `update-mime-database`).

#### 3. Stdin Path Streaming Protocol & Spec Implementation

When RPM executes a file trigger, it streams the absolute paths of all modified matching files to the scriptlet's standard input:

```spec
# Inside fontconfig.spec
Name: fontconfig
...

# Global Transaction Trigger for any font updates:
%transfiletriggerin -P 500000 -- /usr/share/fonts /usr/share/X11/fonts
# Matched paths are streamed via stdin; fc-cache scans the directories:
/usr/bin/fc-cache -s >/dev/null 2>&1 || :

# Inside systemd.spec
# Consumes matching paths line-by-line from stdin:
%transfiletriggerin -P 1000000 -- /usr/lib/sysusers.d
while read -r path; do
    /usr/bin/systemd-sysusers "$path" >/dev/null 2>&1 || :
done
```

---

### E. Embedded In-Process Lua Interpreters (`-p <lua>`)

RPM embeds a native Lua 5.x interpreter directly inside `librpm`. By specifying the `-p <lua>` interpreter flag on any scriptlet, RPM executes the code directly in `librpm` process memory.

#### 1. Architectural Motivations
* **Zero Subprocess Spawning**: Eliminates `fork()` and `exec()` overhead, accelerating container and rootfs builds.
* **Hermetic Minimal Environments**: Allows running lifecycle hooks in minimal root filesystems, bootstrap containers, and immutable OS builders (`rpm-ostree`, Fedora CoreOS, MicroOS) that do not have `/bin/sh`, Bash, or Coreutils installed.
* **Atomic Filesystem Operations**: Utilizes direct POSIX C-level system calls via Lua bindings.

#### 2. Built-in Lua POSIX Bindings in RPM
`librpm` exposes a `posix` module and an `rpm` module with functions including:
* `posix.stat(path)` / `posix.lstat(path)`
* `posix.symlink(oldpath, newpath)` / `posix.unlink(path)`
* `posix.mkdir(path, mode)` / `posix.rmdir(path)`
* `posix.chmod(path, mode)` / `posix.chown(path, uid, gid)`
* `rpm.execute(path, arg1, ...)`
* `rpm.vercmp(version1, version2)`

#### 3. Complete In-Process Lua Spec Example

```spec
Name: micro-service
...

%post -p <lua>
-- Pure in-process execution with zero external process dependencies
local link_target = "/etc/micro-service/active.conf"
local default_conf = "/etc/micro-service/default.conf"

local stat = posix.stat(link_target)
if not stat then
    local ok, err = posix.symlink(default_conf, link_target)
    if not ok then
        print("Failed to create symlink: " .. tostring(err))
    end
end
```

---

### F. Advantages
1. **Inversion of Control for Modular Addons (`%triggerin`)**: Allows plugins (`mod_ssl`, `php-pdo`) to automatically awaken and reconfigure when their parent (`httpd`, `php`) upgrades, without polluting the parent package.
2. **Zero-Subprocess In-Process Execution (`-p <lua>`)**: Embedded Lua runs within `librpm` memory, avoiding shell fork overhead and enabling script execution in minimal container environments lacking `/bin/sh`.
3. **Multi-Tiered Granularity with Explicit Priorities**: Spans single-package (`%post`), cross-package (`%triggerin`), and prioritized transaction-level batching (`%transfiletriggerin -P`).
4. **Resilient Non-Disruptive Upgrades**: Installing new binaries before removing old versions ensures services experience zero executable downtime during software updates.

### G. Limitations & Disadvantages
1. **High Conceptual Complexity**: Layering traditional scriptlets, instance counts (`$1`), inter-package triggers, file triggers, and trans-file triggers creates intricate execution matrices where subtle ordering bugs can be hard to trace.
2. **Lack of Standardized User Interaction Database**: Unlike Debian's `debconf`, RPM lacks a standardized configuration prompting layer, leading to ad-hoc scripts or reliance on external orchestration tools.
3. **Silent Scriptlet Failures**: Non-fatal exit codes or missing error guards in `%post` can leave packages marked as installed even when underlying service initialization failed.

---

## 5. Arch Linux ALPM (`.pkg.tar.zst` / `pacman`) Hook Architecture

Arch Linux’s `libalpm` (Arch Linux Package Manager) library introduced **declarative, centralized, passive event hooks**, deliberately decoupling passive data packages from central subsystem handlers.

### A. Taxonomy & Hook Classification

1. **Category 1: Declarative ALPM Event Hooks (`.hook` files)**
   * **Trigger Conditions (`[Trigger]`)**:
     * Filesystem Path Triggers: `Type = Path`, `Target = <glob>` (e.g. `usr/share/fonts/*`).
     * Package Name Triggers: `Type = Package`, `Target = <pkgname>` (e.g. `systemd`, `linux`).
     * Operation Filters: `Operation = Install`, `Operation = Upgrade`, `Operation = Remove`.
   * **Action Handlers (`[Action]`)**:
     * Execution Timing: `When = PreTransaction` (before changes commit) or `When = PostTransaction` (after all packages extract).
     * Execution Command: `Exec = /path/to/binary [args...]`.
     * Stream Piping: `NeedsTargets` (streams all matching paths/packages via `stdin`).
     * Failure Abort Gate: `AbortOnFail` (aborts transaction on `PreTransaction` failure).
     * Dependency Guard: `Depends = <pkgname>` (ensures required runtime binaries exist).

2. **Category 2: Legacy Package Scriptlets (`.INSTALL`)**
   * Package-bound shell scripts implementing: `pre_install`, `post_install`, `pre_upgrade`, `post_upgrade`, `pre_remove`, `post_remove`.
   * *Status in Arch Linux*: Formally deprecated for shared subsystem maintenance; reserved exclusively for bespoke version upgrade notices or one-off data migrations.

3. **Category 3: System Administrator Local Overrides & Masking (`/etc/pacman.d/hooks/`)**
   * High-priority hook directory allowing sysadmins to override package hooks or create custom filesystem snapshot hooks.

---

### B. Declarative ALPM Hook Specification (`.hook` Format)

ALPM hooks are simple INI-formatted text files placed in hook directories. Each hook file defines exactly two sections: `[Trigger]` (specifying matching criteria) and `[Action]` (specifying execution parameters).

#### 1. ALPM Directive Reference

| Section | Directive | Valid Values | Purpose |
| :--- | :--- | :--- | :--- |
| **`[Trigger]`** | `Type` | `Path` \| `Package` | Match filesystem paths or package names |
| **`[Trigger]`** | `Operation` | `Install` \| `Upgrade` \| `Remove` | Package operations activating trigger (repeatable) |
| **`[Trigger]`** | `Target` | Path glob or package name | Pattern (e.g. `usr/share/fonts/*`) or pkg name (repeatable) |
| **`[Action]`** | `Description` | String | Console progress message during execution |
| **`[Action]`** | `When` | `PreTransaction` \| `PostTransaction` | Timing boundary relative to archive extraction |
| **`[Action]`** | `Exec` | Absolute binary path + args | Executable binary command (no shell built-ins) |
| **`[Action]`** | `NeedsTargets` | *(Flag)* | Pipes matched targets line-by-line to `stdin` |
| **`[Action]`** | `AbortOnFail` | *(Flag)* | Aborts transaction if `PreTransaction` hook fails |
| **`[Action]`** | `Depends` | Package name | Requires dependency package to be installed |

---

### C. Libalpm Internal Matching & Batch Execution Engine

The core innovation of ALPM hooks lies in how `libalpm` evaluates triggers during package transactions:

* **Phase 1: Stream Ingestion (`libarchive`)**:
  * Download `.pkg.tar.zst` archives $\rightarrow$ `libarchive` extracts files into `/`.
  * In-memory `fnmatch()` glob check against `[Trigger]` catalog.
* **Phase 2: Transaction Consolidation**:
  * Queue matched hooks (deduplicated).
  * Accumulate matched target paths into hook stream buffer.
* **Phase 3: PostTransaction Execution**:
  * Sort matched hooks lexicographically.
  * Execute hook `Exec` binary (stream target paths via `stdin`).

#### 1. Zero-Cost Streaming Pattern Matching
During `alpm_trans_commit`, `libarchive` extracts file entries one by one from `.pkg.tar.zst` payload archives. `libalpm` intercepts every file path in memory and tests it against registered `[Trigger]` glob patterns using fast `fnmatch()`.
* **Zero Disk Crawling**: Pacman never searches the disk to find modified files after an upgrade.
* **Single Batch Execution**: Whether a transaction upgrades 1 font package or 500 font packages, the font cache hook runs **exactly once**.

#### 2. The `NeedsTargets` Stdin Streaming Protocol
When `NeedsTargets` is specified, `libalpm` spawns the `Exec` binary with a pipe connected to standard input, writing each matching target on a new line:

```bash
#!/bin/sh
# /usr/share/libalpm/scripts/python-compile
# Consumes matching Python source paths from stdin:
while read -r file; do
    case "$file" in
        *.py)
            /usr/bin/python3 -m py_compile "$file" 2>/dev/null || true
            ;;
    esac
done
```

---

### D. Code Examples: Real-World Subsystem Handlers

#### 1. Subsystem Cache Rebuild: GSettings Schema Compiler

```ini
# /usr/share/libalpm/hooks/glib-compile-schemas.hook
[Trigger]
Type = Path
Operation = Install
Operation = Upgrade
Operation = Remove
Target = usr/share/glib-2.0/schemas/*.gschema.xml
Target = usr/share/glib-2.0/schemas/gschemas.compiled

[Action]
Description = Compiling GSettings XML schema files...
When = PostTransaction
Exec = /usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas
```

#### 2. Kernel & Initramfs Generation: `mkinitcpio`

```ini
# /usr/share/libalpm/hooks/90-mkinitcpio-install.hook
[Trigger]
Type = Path
Operation = Install
Operation = Upgrade
Target = usr/lib/modules/*/vmlinuz
Target = usr/lib/initcpio/*

[Action]
Description = Updating linux initcpios...
When = PostTransaction
Exec = /usr/share/libalpm/scripts/mkinitcpio-install
NeedsTargets
Depends = mkinitcpio
```

#### 3. Sysadmin Automated Snapshotting Gate (PreTransaction Safety)

```ini
# /etc/pacman.d/hooks/00-btrfs-pre-snapshot.hook
[Trigger]
Type = Package
Operation = Install
Operation = Upgrade
Operation = Remove
Target = *

[Action]
Description = Creating pre-transaction Btrfs snapshot...
When = PreTransaction
Exec = /usr/local/bin/create-btrfs-snapshot pre
AbortOnFail
```

---

### E. The Paradigm Shift: Deprecation of Imperative `.INSTALL` Scriptlets

In early Arch Linux (and traditional packaging formats), every individual package that installed a font, icon, desktop file, or schema contained an imperative `.INSTALL` scriptlet:

```bash
# Old Arch Linux .INSTALL scriptlet (Obsolete Anti-Pattern)
post_install() {
    echo "Updating icon cache..."
    gtk-update-icon-cache -q -t -f usr/share/icons/hicolor
    echo "Updating desktop database..."
    update-desktop-database -q
}
post_upgrade() {
    post_install
}
post_remove() {
    post_install
}
```

#### Why ALPM Hooks Replaced `.INSTALL`:
1. **Redundant Execution Storms Eliminated**: Upgrading 50 GNOME packages previously ran `gtk-update-icon-cache` 50 times in a row. ALPM hooks consolidate this into a single execution at `PostTransaction`.
2. **Pure Declarative Packages**: Packages became pure data archives (`.pkg.tar.zst`) without arbitrary maintainer shell scripts, eliminating packaging boilerplate and security vulnerabilities.
3. **Subsystem Decoupling**: Application packages (`gimp`, `vlc`) no longer need to know which tools compile schemas or rebuild caches; the subsystem packages (`glib2`, `gtk3`) own their own `.hook` definitions.

---

### F. Search Paths, Precedence & Administrator Overrides

ALPM resolves hooks using a strict priority hierarchy:

1. **Administrator Directory (`/etc/pacman.d/hooks/`)**: Highest priority. Custom user hooks and overrides.
2. **System Distribution Directory (`/usr/share/libalpm/hooks/`)**: Default distribution hooks provided by installed packages.

#### 1. Lexicographical Sorting Order
Hooks across both directories are executed in **alphanumeric sorting order** based on their filename:
* `00-btrfs-pre-snapshot.hook` (runs first in `PreTransaction`)
* `20-systemd-sysusers.hook`
* `60-mkinitcpio-remove.hook`
* `90-mkinitcpio-install.hook`
* `99-secureboot-sign.hook` (runs last in `PostTransaction`)

#### 2. Masking & Overriding System Hooks
* **Override**: Placing a hook with the same filename in `/etc/pacman.d/hooks/<name>.hook` completely replaces the system hook in `/usr/share/libalpm/hooks/<name>.hook`.
* **Masking**: Symlinking a system hook name to `/dev/null` inside `/etc/pacman.d/hooks/` completely disables that hook:
  ```bash
  ln -s /dev/null /etc/pacman.d/hooks/glib-compile-schemas.hook
  ```

---

### G. Advantages
1. **Blazing Fast Native Batching**: Decouples data packages from system handlers; evaluates file paths in C during extraction and runs consolidated `PostTransaction` hooks once at the end.
2. **Minimalist, Script-Free Packages**: Over 95% of packages contain zero scripts or boilerplate, eliminating security attack surfaces and packaging maintenance burden.
3. **Declarative System Overrides**: Clean INI-style syntax in `/etc/pacman.d/hooks/` allows administrators to easily override, customize, or mask system hooks with symlinks.
4. **Pre-Transaction Abort Gates (`AbortOnFail`)**: Enables automated safety checks and filesystem snapshots to halt transactions before any package files are written to disk.

### H. Limitations & Disadvantages
1. **No Package-to-Package Reactive Inversion**: Lacks native package-to-package lifecycle triggers (like RPM's `%triggerin`); cross-package coordination must be funneled through filesystem path triggers or systemd services.
2. **Coarse Lexicographical Ordering**: Execution order relies on alphanumeric filename sorting (`20-*.hook`, `90-*.hook`) rather than explicit dependency graphs or priority numbers.
3. **Unrecoverable Post-Commit Failures**: If a `PostTransaction` hook fails, the transaction is already committed; pacman emits a warning but does not trigger automatic state rollback.

---

## 6. Drift Configuration Lifecycle Hook Model

While distribution package managers operate on binary archives and system root directories, **Drift** operates on a **declarative, host-adaptive configuration lifecycle**.

Hooks in Drift reside strictly within [`drift_hooks/`](../features/configuration_and_hooks.md) and participate across the entire compilation, staging, and deployment pipeline.

### A. Taxonomy & Hook Classification

1. **Category 1: Build & Compilation Lifecycle**
   * **`pre_source`**: Dynamic build hook executed before reading package source files (enables just-in-time compilation via `cargo build` or remote asset downloads).
   * **`post_render`**: Validation and transformation hook executed in `render/<pkg>/` after template compilation, prior to touching host targets.

2. **Category 2: Deployment & Upgrade Lifecycle**
   * **`pre_install` / `post_install`**: Initial deployment boundary hooks (around native relative symlinking or copy).
   * **`pre_update` / `post_update`**: Upgrade boundary hooks executed around Staged Git delta synchronization.
   * **`pre_uninstall` / `post_uninstall`**: Decommissioning and resource cleanup hooks.

3. **Category 3: Operational Diagnostics & Capability Gating**
   * **`probe`**: Dynamic capability gate evaluated during requirement resolution (skips or disables package if host hardware/daemon readiness fails).
   * **`health`**: Diagnostic health check evaluated on-demand or during `drift health` to verify runtime operational integrity.

### B. Configuration & Dynamic Hook Example

```toml
# src/my_service/drift_package.toml
[package]
install_method = "copy"

[package.requirements]
os = ["linux", "darwin"]
binaries = ["curl", "systemctl"]

[hooks]
probe        = "drift_hooks/check_gpu.sh"
pre_source   = "drift_hooks/build_binary.sh"
post_render  = "drift_hooks/validate_syntax.sh"
# Configure the post-render target path; Drift automatically compiles start_service.envst.sh -> .drift/hooks/start_service.sh
post_install = "drift_hooks/start_service.sh"
health       = "drift_hooks/health_check.sh"
timeout      = 30
```

```bash
# src/my_service/drift_hooks/start_service.envst.sh
#!/usr/bin/env bash
echo "Deploying ${drift_package_name} to target ${drift_package_target_dir}"
systemctl --user restart "${SERVICE_NAME}"
```

### C. Context & Fact Injection
Every Drift hook executes with typed domain facts and 6-tier scoped environment variables:
* `$drift_package_name`: The active package name.
* `$drift_package_source_dir` / `$drift_package_src_dir`, `$drift_package_render_dir`, `$drift_package_install_dir`: Stage directory paths for the package.
* `$drift_package_target_dir`: The destination host root path.
* `$drift_package_install_method`: `symlink` or `copy`.
* `$drift_os`, `$drift_arch`, `$drift_distro`, `$drift_hostname`, `$drift_user`, `$drift_ip_addresses`: Standardized host facts.
* `[env.override]`, `[env.secrets]`, `[env.default]`, and `[env.fallback]`: Scoped package environment tables.

### D. Advantages
1. **Dynamic Multi-Engine Hook Templating**: Hook scripts (`drift_hooks/*.[suffix].sh`) are first-class compilation targets, rendered dynamically into `.drift/hooks/` with host facts (`$drift_os`, `$drift_arch`, `$drift_user`) and local secrets.
2. **Rich Lifecycle Phasing (`pre_source`, `post_render`, `probe`, `health`)**: Supports compilation before source read, syntax validation before deployment, and continuous runtime health diagnostics.
3. **Structured Safety & Isolation**: Strict `drift_hooks/` containment, per-hook timeouts, configurable `failure_is_fatal` guards, and non-destructive `--dry-run` inspection.

### E. Limitations & Disadvantages
1. **No Cross-Package Reactive Batching**: Operates on package-isolated pipelines rather than global event queues; updating shared caches across multiple packages requires explicit orchestrator steps or symlinks.
2. **Configuration/User Scope Focus**: Tailored for user environments, dotfiles, and service configurations rather than system-wide kernel/OS binary distribution.

---

## 7. Comparative Synthesis: Real-World Scenarios

### Scenario 1: Rebuilding Subsystem Caches (Fonts / MIME / Desktop Files)
* **DEB**: Relies on `dpkg triggers` (`interest /usr/share/fonts`). Runs in intermediate trigger execution cycles.
* **RPM**: Uses `%transfiletriggerin -P 1000 -- /usr/share/fonts`. Aggregates all modified font paths and runs `fc-cache` once at transaction end.
* **ALPM (Arch)**: Uses `/usr/share/libalpm/hooks/fontconfig.hook` matching `usr/share/fonts/*`. Evaluates in C during `libarchive` unpack, runs `fc-cache -s` once at `PostTransaction`.
* **Drift**: Operates on explicit package units. System cache rebuilds are triggered via explicit `post_install` / `post_update` scripts or dedicated system service packages.

### Scenario 2: Parent Daemon & Modular Plugin Upgrades (Apache & SSL Addon)
* **DEB**: `apache2` package maintains generic configuration hooks; plugins drop configs into `/etc/apache2/mods-available/` and call `a2enmod` in `postinst`.
* **RPM**: `mod_ssl` uses `%triggerin -- httpd` to automatically wake up and reload `httpd` whenever `httpd` upgrades, without polluting `httpd.spec`.
* **ALPM (Arch)**: `httpd` is managed by systemd triggers; modules declare package dependencies and rely on systemd service restarts.
* **Drift**: Managed through declarative package composition and symlink trees. Upstream changes are diffed via `drift diff` and merged back using `drift adopt`.

### Scenario 3: Machine-Adaptive Configuration & Secret Injection
* **DEB / RPM / ALPM**: Static package content built once by the distribution packager. Cannot adapt templates or inject local secrets at install time without secondary tools (Ansible, Puppet).
* **Drift**: Built specifically for host adaptation. Compiles multi-engine templates (`.envst`, `.mustache`, `.j2`) on the local host with local secrets, host facts, and user overrides before staging to disk.

---

## 8. Architectural Takeaways

1. **Passive Triggers Maximize Throughput**: Decoupling data packages from event handlers (as in ALPM and RPM file triggers) eliminates redundant process forking and makes multi-package transactions orders of magnitude faster.
2. **Inversion of Control Simplifies Modularity**: RPM's `%triggerin -- target` solves the "dormant plugin" problem by letting addons declare callbacks on parent package lifecycles.
3. **Defensive Rigor Prioritizes Durability**: DEB's state machine and Debconf integration prioritize atomic recovery and enterprise automation over raw install speed.
4. **Declarative Lifecycle Completes Configuration Management**: Drift fills the gap where OS packagers stop—providing dynamic templated compilation, continuous health monitoring, pre-source dynamic builds, and bidirectional drift adoption for user and server environments.
