# 🌀 Drift

**Two-way, transactional configuration management for the machines you actually use.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org)
[![Build Status](https://img.shields.io/badge/tests-1499%20passed-brightgreen)](tests/)

> **WARNING: Drift is still very glitchy and in early development stage, it's for experimental usage only. Backward compatibility is not considered. File syntax, deployment rules, function signatures may change at any time.**

---

## Binaries don't drift. Configuration does.

Tools like [Nix](https://nixos.org), [mise](https://mise.jdx.dev), [pkgit](https://git.symlinx.net/pkgit/about/), or the system package manager install software reproducibly: a given version is almost the same bytes on every machine, and nothing rewrites it at runtime.

Configuration is different. It varies per machine, embeds secrets, and is edited in place all the time by GUI apps, IDEs, installers, and you. **That mutable layer is what Drift manages.**

Drift treats configuration as **packages** compiled from templates. It previews every change before it happens, deploys transactionally with rollback, and when something edits a deployed file, it detects the drift and lets you **adopt the change back into source** instead of silently overwriting it.

Dotfiles are where most people start, but Drift equally handles:

* Application settings (`~/.config/...`, `~/Library/Application Support/...`)
* System configuration under `/etc` (per-package `sudo = true`)
* Service configs that must be stopped, reloaded, or health-checked around updates (lifecycle hooks)

---

## ⚡ The Three Pillars

### 1. Compile safely
* Templates, layered variables, and secrets compile into an isolated `render/` Git sandbox. A failed render never touches your system.
* 6-tier environment model with in-TOML `${VAR}` stitching and Python workspace/package hooks.
* `[secrets]` are decrypted in memory only (`age`, `gpg`, `sops`, `op`, `bw`, `pass`).
* Incremental Merkle-DAG caching skips unchanged templates instantly.

### 2. Deploy transactionally
* `drift plan` simulates the full render → stage → deliver cycle in an ephemeral sandbox.
* `drift deploy` delivers symlinks or copies with collision guards, automatic backups, dependency ordering, and lifecycle hooks.
* A write-ahead log powers automatic or manual `drift rollback` after a mid-deploy failure.

### 3. Reconcile drift, both ways
* Live edits are reverse-synced into a local state repo (`install/`); a sentinel halts deploys before they would overwrite them.
* Inspect with `drift status` and `drift diff -s` (side-by-side in your editor with `-y`).
* Keep changes with `drift adopt` (patched back into `src/`), or discard them with `drift deploy --force` (a snapshot stays in history).

👉 Full details: **[Feature Guide](docs/features.md)**

---

## 🧭 How It Works

Drift keeps four tiers, each with a single responsibility:

1. **Source** (`src/<pkg>/`, `config/`): templates and package metadata, tracked in your own Git repo.
2. **Render** (`render/`): compiled output, in an isolated local Git repo.
3. **Install** (`install/`): last known-good deployed state, plus live drift pulled back from the host.
4. **Host** (`~`, `~/.config`, `/etc`, …): where applications read their configuration.

`drift deploy` moves changes forward (1 → 4). Reverse-sync and `drift adopt` carry live edits backward (4 → 3 → 1). See [Architecture](docs/architecture.md) for the full model.

---

## 🚀 Five-Minute Tour

### 1. Forward Flow: Package & Deploy

```bash
# 1. Create a workspace (an empty directory, or a subfolder of an existing repo)
mkdir my-config && cd my-config
drift init

# 2. Package an existing config file
drift new git -t '~'
drift add git ~/.gitconfig              # -> src/git/dot-gitconfig

# 3. Enable the package on this machine (writes config/drift_workspace.local.toml)
drift enable --local git

# 4. Commit your source, preview, then deploy
git add -A && git commit -m "Add git package"
drift plan
drift deploy                            # original file is backed up to backup/git/overwritten/
```

### 2. Reverse Flow: Simulate Drift & Adopt

```bash
# 1. Something edits the live file on the host...
printf '[alias]\n\tst = status\n' >> ~/.gitconfig

# 2. ...Drift notices, blocks accidental overwrite, and lets you adopt the edit back
drift status                            # [B] System: DRIFTED
drift diff -s git                       # exactly what changed on the host
drift deploy                            # halts: system drift detected
drift adopt git                         # keep it: patched into src/git/dot-gitconfig
drift deploy                            # clean again
```

More end-to-end flows (bootstrapping a new machine, migrating a legacy dotfiles repo, rollback) are in [Feature Guide → Workflows](docs/features.md#part-v--workflows).

---

## 📦 Installation

Drift requires **Python 3.9+** and has **zero mandatory third-party dependencies** (optional `[rich]` terminal UI).

The recommended install is the shell wrapper, which runs Drift directly from your clone so `git pull` updates take effect immediately:

```bash
git clone https://github.com/ElevenZen/drift.git
cd drift
./script/shell_wrapper_installer.bash   # also installs tab-completions
```

Standalone `zipapp` binaries, wheels, `pipx` / `uv tool`, and manual completion setup for Bash, Zsh, Fish, and Nushell are covered in **[Installation & Packaging](docs/installation.md)**.

---

## 🛠️ Everyday Commands

| Command | Purpose |
| :--- | :--- |
| `drift init` / `drift clone <url>` | Create or bootstrap a workspace |
| `drift new <pkg>` / `drift add <pkg> <paths>` | Scaffold a package / import host files |
| `drift enable <pkgs>` / `drift disable <pkgs>` | Toggle packages in `[packages.enable]` (`--local`) |
| `drift plan [pkgs]` | Preview the full deployment |
| `drift deploy [pkgs]` | Compile, stage, and deliver |
| `drift status [pkgs]` / `drift diff [pkgs]` | Inspect template, pending, and host drift |
| `drift adopt [pkgs]` | Backport host drift into source |
| `drift rollback [pkgs]` | Recover from a failed transaction |
| `drift uninstall <pkgs>` | Remove deployed files, restore backups |
| `drift gc` / `drift repair` | Purge orphans / self-heal the workspace |
| `drift help [topic]` | Built-in manual |

* Drift finds the workspace by walking up from the current directory (or `-C <dir>`) to the nearest folder containing `config/drift_workspace.toml`.
* Every command supports `--json`. Low-level primitives (`render`, `stage`, `apply`, `reverse-sync`, …) and all flags are documented in the **[CLI Reference](docs/cli.md)**.

---

## 🎯 Where Drift Fits

| Layer | Nature | Best tool |
| :--- | :--- | :--- |
| Tools, runtimes, binaries | Immutable, versioned | mise / Nix / pkgit |
| Configuration on your machines | Per-host, mutable | **Drift** |
| Remote fleet orchestration | Push-based, multi-host | Ansible and friends |

* Drift is **local-first**: it runs on the machine it configures and does not orchestrate remote hosts.
* Drift is **not hermetic**: it manages files on a live system rather than building isolated environments.

---

## 📚 Documentation

* **[Feature Guide](docs/features.md)**: everything Drift can do, organized by pillar, plus end-to-end workflows.
* **[Installation & Packaging](docs/installation.md)**: all install methods and shell completions.
* **[CLI Reference](docs/cli.md)**: every command and flag.
* **[Troubleshooting & FAQ](docs/troubleshooting.md)**: recovering from common situations.
* **[Architecture](docs/architecture.md)**: the 4-tier model, primitives, invariants, and subsystem specs.
* **[Roadmap](docs/roadmap.md)**: what's next.
* **Built-in manual**: `drift help` or `drift help <topic>` (`package`, `workspace`, `drift_package.toml`, `faq`, …).

---

## 📜 License

Drift is released under the **MIT License**. See [LICENSE](LICENSE) for details.
