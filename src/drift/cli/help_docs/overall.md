# 🌀 Drift: Next-Gen Transactional Dotfile Manager

Drift is a declarative, modular configuration and dotfile deployment engine designed 
for power users who demand system safety, predictability, and complete visibility.

Unlike traditional dotfile managers that directly symlink mutable directories or run 
opaque installation scripts, Drift implements a **two-stage, Git-backed compilation 
and deployment pipeline**. It isolates templates, compiles them in a secure sandbox, 
audits active system drifts, and executes deployments using atomic, transactional workflows.

## 🔄 The Drift Data-Flow Loop
```
[1. Declarative Source]      src/ & config/
  │
  ▼  drift deploy (compile)
[2. Sandbox Render Zone]     render/ (isolated Git repo)
  │
  ▼  Stage delta (Diff Δ)
[3. Local State Database]    install/ (tracking Git repo)
  │ ▲
  │ │  Reverse-sync (Diff B) / drift adopt
  ▼ │  Apply (stow / copy)
[4. Active Host System]      ~/* or /etc/*
```

## 🚀 High-Level User Commands (Frequently Used)
*   `drift clone <url> [dir]` Clones a Git repo and auto-bootstraps/repairs the Drift workspace.
*   `drift init`              Initializes a new Git-backed Drift workspace & databases.
*   `drift new <pkg>`         Scaffolds a new package directory with `drift_package.toml` metadata.
*   `drift add <pkg> <paths>` Imports external target-system configurations into package source.
*   `drift adopt <pkg>`       Backports uncommitted system drifts back into package templates.
*   `drift deploy [pkgs]`     Sandbox-compiles, stages, and deploys declarative configs to host.
*   `drift health [pkgs]`     Runs runtime health check probes on installed packages.
*   `drift uninstall [pkgs]`  Removes stowed/copied mappings on host paths, restoring backups.
*   `drift rollback [pkgs]`   Resets staging/deploy midway failures to restore a stable state.
*   `drift status`            Audits and inspects current template, staging, and system-drift status.
*   `drift diff`              Compares and visualizes template, deployment, or active system layers (-y for visual diff).
*   `drift gc`                Purges orphan packages and zombie database directories.
*   `drift repair`            Audits and self-heals workspace structure and Git databases.
*   `drift complete [shell]`  Generates or installs interactive shell tab-completions.

👉 For detailed documentation, run:
    `drift help package`               Understand the 'package' concept and config files.
    `drift help src`                   Learn about the declarative source directory (src/).
    `drift help render`                Understand the sandbox compilation (render/).
    `drift help install`               Understand the state database and deployment (install/).
    `drift help fcd`                   Understand Fully-Controlled Directories (FCDs) and file tracking.
    `drift help ignore`                Understand .drift_ignore syntax, install ignore logic, and FCD ignore mechanics.
    `drift help drift_package.toml`    View a complete, commented drift_package.toml configuration template.
    `drift help drift_workspace.toml`  View a complete, commented drift_workspace.toml global template.
    `drift help workspace`             Learn about workspace directories, local overrides, and the secrets vault.
    `drift help health`                Learn about package runtime health check probes and lifecycle hooks.
    `drift help clone`                 Learn about cloning Drift repositories and migrating legacy dotfiles.
    `drift help faq`                   Troubleshooting recipes and frequently asked questions.
