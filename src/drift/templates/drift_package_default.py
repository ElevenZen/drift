"""drift_package.py - Dynamic Python Package Configuration Hook for '{package_name}'.

For programmatic, procedural package configuration that exceeds static TOML or
declarative variable stitching capabilities, Drift supports dynamic Python package hooks.

Best Practice:
    drift_package.py is the recommended, canonical place to download configuration files
    or secrets from remote servers (such as 1Password CLI, HashiCorp Vault, Bitwarden,
    AWS Secrets Manager, or remote HTTP endpoints) and inject them dynamically into the
    package environment ([env.default] or [env.secrets]) before template compilation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from drift.hooks import PackageHookContext


def configure_package(context: PackageHookContext) -> Dict[str, Any]:
    """Dynamically transforms package settings before variable stitching and template rendering.

    Execution Pipeline Order:
        1. Multi-File Discovery & Merging (drift_package.toml + drift_package.local.toml).
        2. Dynamic Python Hook (this function): Runs BEFORE variable stitching and template rendering.
        3. 6-Tier Precedence & DAG Resolution: Evaluates package [env.default], [env.secrets], etc.
        4. Cross-Section Interpolation: Replaces ${VAR} across package configuration fields.
        5. Render Staging: Emits static compiled metadata to render/<pkg>/.drift/drift_package.toml.

    Context Attributes (PackageHookContext):
        - context.config: Mutable raw configuration dictionary parsed from TOML.
        - context.package_name: Name of the current package (str).
        - context.package_dir: Path to the package source directory (Path).
        - context.drift_root: Path to the workspace root directory (Optional[Path]).
        - context.workspace_config: Parent WorkspaceConfig object (Optional[WorkspaceConfig]).
        - context.facts: Auto-detected system facts (drift_os, drift_arch, drift_distro, etc.).
        - context.package_facts: Package-specific facts (drift_package_name, drift_package_src_dir, etc.).
        - context.env: Current active environment variables snapshot (Dict[str, str]).
        - Helper properties: context.os, context.arch, context.distro, context.hostname, context.user.

    Returns:
        The modified configuration dictionary (must be a valid dictionary).
    """
    config = context.config

    # -------------------------------------------------------------------------
    # Example 1: Download Remote Secrets / Configs & Inject into [env.secrets] / [env.default]
    # -------------------------------------------------------------------------
    # import subprocess, json
    # try:
    #     # Example fetching a secret from 1Password CLI or remote API
    #     # token = subprocess.check_output(["op", "read", "op://vault/item/token"], text=True).strip()
    #     # env_secrets = config.setdefault("env", {}).setdefault("secrets", {})
    #     # env_secrets["MY_APP_API_TOKEN"] = token
    #     pass
    # except Exception as err:
    #     pass

    # -------------------------------------------------------------------------
    # Example 2: Host / Platform Conditional Adjustments
    # -------------------------------------------------------------------------
    # pkg = config.setdefault("package", {})
    # if context.os == "darwin":
    #     pkg["target_directory"] = "~/Library/Application Support/{package_name}"
    # elif context.os == "linux" and context.distro == "arch":
    #     pkg["install_method"] = "stow"

    return config
