"""Structured help documentation content loading and paging print helper for the drift help commands."""

import sys
from pathlib import Path
from typing import Optional
import pydoc


HELP_TOPIC_ALIASES = {
    # overall / architectural overview
    "overall": "overall",
    "overview": "overall",
    "all": "overall",
    "intro": "overall",
    "architecture": "overall",
    "arch": "overall",

    # package concept
    "package": "package",
    "packages": "package",
    "pkg": "package",
    "pkgs": "package",

    # src templates
    "src": "src",
    "source": "src",
    "templates": "src",
    "template": "src",

    # render sandbox
    "render": "render",
    "rendering": "render",
    "compile": "render",
    "compiler": "render",
    "engine": "render",
    "engines": "render",
    "sandbox": "render",

    # install state database
    "install": "install",
    "staging": "install",
    "deploy": "install",
    "deployment": "install",
    "state": "install",

    # fully controlled directories
    "fcd": "fcd",
    "fully_controlled_dirs": "fcd",
    "fully-controlled-dirs": "fcd",
    "controlled_dirs": "fcd",

    # ignore engine
    "ignore": "ignore",
    "drift_ignore": "ignore",
    ".drift_ignore": "ignore",
    "driftignore": "ignore",
    ".driftignore": "ignore",
    "stow_ignore": "ignore",
    ".stow-local-ignore": "ignore",

    # drift_package.toml configuration reference
    "drift_package.toml": "drift_package_toml",
    "drift_package": "drift_package_toml",
    "drift_package_toml": "drift_package_toml",
    "drift-package.toml": "drift_package_toml",
    "drift-package": "drift_package_toml",
    "package_config": "drift_package_toml",
    "pkg_config": "drift_package_toml",
    "package-config": "drift_package_toml",
    "pkg-config": "drift_package_toml",
    "package.toml": "drift_package_toml",
    "pkg.toml": "drift_package_toml",

    # drift_workspace.toml global configuration reference
    "drift_workspace.toml": "drift_workspace_toml",
    "drift_workspace": "drift_workspace_toml",
    "drift_workspace_toml": "drift_workspace_toml",
    "drift-workspace.toml": "drift_workspace_toml",
    "drift-workspace": "drift_workspace_toml",
    "workspace_config": "drift_workspace_toml",
    "ws_config": "drift_workspace_toml",
    "workspace-config": "drift_workspace_toml",
    "ws-config": "drift_workspace_toml",
    "workspace.toml": "drift_workspace_toml",
    "ws.toml": "drift_workspace_toml",
    "drift.toml": "drift_workspace_toml",
    "drift_toml": "drift_workspace_toml",

    # workspace structure, multi-machine overrides, and secrets
    "workspace": "workspace",
    "ws": "workspace",
    "workspaces": "workspace",
    "overrides": "workspace",
    "secrets": "workspace",
    "secrets.env": "workspace",

    # health checks and lifecycle hooks
    "health": "health",
    "check": "health",
    "checks": "health",
    "probe": "health",
    "probes": "health",
    "hook": "health",
    "hooks": "health",
    "lifecycle": "health",

    # repository cloning and bootstrapping
    "clone": "clone",
    "cloning": "clone",
    "migration": "clone",
    "migrate": "clone",
    "bootstrap": "clone",

    # frequently asked questions and troubleshooting
    "faq": "faq",
    "troubleshooting": "faq",
    "troubleshoot": "faq",
    "qna": "faq",
    "questions": "faq",
}


def get_help_page(topic: Optional[str]) -> str:
    """Returns the Markdown content matching the requested help topic dynamically from markdown files."""
    if not topic:
        topic_file_name = "overall"
    else:
        topic_lower = topic.lower().strip()
        topic_file_name = HELP_TOPIC_ALIASES.get(topic_lower)
        if not topic_file_name:
            raise ValueError(
                f"Unknown help topic: '{topic}'.\n"
                "Available topics are: 'overall', 'package', 'src', 'render', 'install', 'fcd', 'ignore', 'drift_package.toml' (or 'package_config'), 'drift_workspace.toml' (or 'workspace_config'), 'workspace', 'health', 'clone', 'faq'."
            )

    # Try pkgutil first (supports zipapp and installed packages)
    try:
        import pkgutil
        data = pkgutil.get_data("drift.cli", f"help_docs/{topic_file_name}.md")
        if data:
            return data.decode("utf-8")
    except Exception:
        pass

    help_dir = Path(__file__).resolve().parent / "help_docs"
    md_file_path = help_dir / f"{topic_file_name}.md"
    if not md_file_path.exists():
        raise FileNotFoundError(f"Help file not found at: {md_file_path}")

    return md_file_path.read_text(encoding="utf-8")


def print_help_document(topic: Optional[str] = None) -> None:
    """Retrieves and prints the requested help documentation with pager fallback support."""
    try:
        content = get_help_page(topic)
    except Exception as e:
        print(f"❌ [ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if sys.stdout.isatty():
        pydoc.pager(content)
    else:
        print(content)
