# 🌀 Drift: Next-Gen Transactional Dotfile Manager

Drift is a declarative, modular configuration and dotfile deployment engine designed 
for power users who demand system safety, predictability, and complete visibility.

Unlike traditional dotfile managers that directly symlink mutable directories or run 
opaque installation scripts, Drift implements a **two-stage, Git-backed compilation 
and deployment pipeline**. It isolates templates, compiles them in a secure sandbox, 
audits active system drifts, and executes deployments using atomic, transactional workflows.

## 🔄 The Drift Data-Flow Loop
```
                     [ 1. DECLARATIVE SOURCE ]
                     src/ (Templates & drift.toml)
                                 │
                                 ▼ (drift deploy)
                    [ 2. SANDBOX RENDER ZONE ]
                      render/ (Git sandbox compile base)
                                 │
                                 ▼ (Stage render to install)
                    [ 3. LOCAL STATE DATABASE ]
                      install/ (Git local state database)
                                 ▲
                        Diff     │ (drift status / drift diff -s)
                       (Live)    ▼ (Symmetric path translation)
                    [ 4. SYSTEM ACTIVE HOST ]
                       ~/* or /etc/* (Active system configurations)
```

## 🚀 High-Level User Commands (Frequently Used)
*   `drift init`              Initializes a new Git-backed Drift workspace & databases.
*   `drift new <pkg>`         Scaffolds a new package directory with `drift_package.toml` metadata.
*   `drift add <pkg> <paths>` Imports external target-system configurations into package source.
*   `drift adopt <pkg>`       Backports uncommitted system drifts back into package templates.
*   `drift deploy [pkgs]`     Sandbox-compiles, stages, and deploys declarative configs to host.
*   `drift uninstall [pkgs]`  Removes stowed/copied mappings on host paths, restoring backups.
*   `drift rollback [pkgs]`   Resets staging/deploy midway failures to restore a stable state.
*   `drift status`            Audits and inspects current template, staging, and system-drift status.
*   `drift diff`              Compares and visualizes template, deployment, or active system layers.

👉 For detailed documentation, run:
    `drift help package`               Understand the 'package' concept and config files.
    `drift help src`                   Learn about the declarative source directory (src/).
    `drift help render`                Understand the sandbox compilation (render/).
    `drift help install`               Understand the state database and deployment (install/).
    `drift help fcd`                   Understand Fully-Controlled Directories (FCDs) and file tracking.
    `drift help ignore`                Understand .drift_ignore syntax, install ignore logic, and FCD ignore mechanics.
    `drift help drift_package.toml`    View a complete, commented drift_package.toml configuration template.
    `drift help drift.toml`            View a complete, commented drift.toml global template.
    `drift help workspace`             Learn about workspace directories, local overrides, and the secrets vault.
