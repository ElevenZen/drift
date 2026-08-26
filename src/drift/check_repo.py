"""Repository validation and health checks using pathlib."""

import logging
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Set

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

from .constants import (
    CONFIG_DIR_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    GLOBAL_CONFIG_LOCAL_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    STATE_REGISTRY_FILE_NAME,
    INSTALL_STOW_IGNORE_PATTERN,
    STOW_LOCAL_IGNORE_FILE_NAME,
)
from .git_utils import (
    is_git_tracked,
    is_bare_repository,
    is_detached_head,
    is_merge_or_rebase_in_progress,
)

logger = logging.getLogger(__name__)


class ComponentStatus(str, Enum):
    GOOD = "good"
    BROKEN = "broken"
    NOT_FOUND = "not_found"


@dataclass
class CheckResult:
    name: str
    status: ComponentStatus
    details: str
    fix_hint: Optional[str] = None


@dataclass
class WorkspaceHealthReport:
    overall_status: ComponentStatus
    checks: List[CheckResult] = field(default_factory=list)

    def is_healthy(self) -> bool:
        return self.overall_status == ComponentStatus.GOOD

    def is_fresh(self) -> bool:
        return self.overall_status == ComponentStatus.NOT_FOUND

    def is_broken(self) -> bool:
        return self.overall_status == ComponentStatus.BROKEN

    def __bool__(self) -> bool:
        """For backward compatibility, returns True only if overall_status is GOOD."""
        return self.is_healthy()

    def get_check(self, name: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def format_diagnostic_summary(self) -> str:
        lines = []
        for c in self.checks:
            if c.status == ComponentStatus.GOOD:
                icon = "✅"
            elif c.status == ComponentStatus.BROKEN:
                icon = "❌"
            else:
                icon = "⚪"
            hint_str = f" (Hint: {c.fix_hint})" if c.fix_hint and c.status != ComponentStatus.GOOD else ""
            lines.append(f"   {icon} {c.name}: [{c.status.value.upper()}] {c.details}{hint_str}")
        return "\n".join(lines)


def check_root_git_repo(drift_root: Path) -> CheckResult:
    """Checks the main workspace Git repository health."""
    if not drift_root.exists():
        return CheckResult(
            name="Main Git Repository",
            status=ComponentStatus.NOT_FOUND,
            details=f"Directory '{drift_root}' does not exist.",
            fix_hint="Create directory and run 'git init'"
        )

    if not is_git_tracked(drift_root):
        return CheckResult(
            name="Main Git Repository",
            status=ComponentStatus.NOT_FOUND,
            details="Workspace root is not tracked by Git.",
            fix_hint="Run 'git init' at workspace root"
        )

    if is_bare_repository(drift_root):
        return CheckResult(
            name="Main Git Repository",
            status=ComponentStatus.BROKEN,
            details="Workspace root is a bare Git repository.",
            fix_hint="Use a non-bare Git repository"
        )

    if is_detached_head(drift_root):
        return CheckResult(
            name="Main Git Repository",
            status=ComponentStatus.BROKEN,
            details="Workspace Git repository is in detached HEAD state.",
            fix_hint="Checkout an active branch (e.g. 'git checkout main')"
        )

    if is_merge_or_rebase_in_progress(drift_root):
        return CheckResult(
            name="Main Git Repository",
            status=ComponentStatus.BROKEN,
            details="Workspace Git repository has a merge or rebase in progress.",
            fix_hint="Complete or abort the active merge/rebase"
        )

    return CheckResult(
        name="Main Git Repository",
        status=ComponentStatus.GOOD,
        details="Main Git repository is healthy."
    )


def check_workspace_config(drift_root: Path) -> CheckResult:
    """
    Checks the global workspace configuration file (config/drift.toml or template).
    """
    config_dir = drift_root / CONFIG_DIR_NAME
    config_file = config_dir / GLOBAL_CONFIG_FILE_NAME
    envst_file = config_dir / f"{GLOBAL_CONFIG_FILE_NAME.split('.')[0]}.envst.toml"

    if not config_file.exists() and not envst_file.exists():
        return CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{CONFIG_DIR_NAME}/{GLOBAL_CONFIG_FILE_NAME}' not found.",
            fix_hint=f"Create default '{CONFIG_DIR_NAME}/{GLOBAL_CONFIG_FILE_NAME}'"
        )

    from .workspace_config import load_workspace_config, render_envst_load_toml

    try:
        data = render_envst_load_toml(config_file)
        if data is None:
            return CheckResult(
                name="Workspace Configuration",
                status=ComponentStatus.NOT_FOUND,
                details=f"'{CONFIG_DIR_NAME}/{GLOBAL_CONFIG_FILE_NAME}' not found.",
                fix_hint=f"Create default '{CONFIG_DIR_NAME}/{GLOBAL_CONFIG_FILE_NAME}'"
            )

        if (
            not isinstance(data, dict)
            or "workspace" not in data
            or "packages" not in data
            or not isinstance(data["packages"], dict)
            or "enable" not in data["packages"]
        ):
            return CheckResult(
                name="Workspace Configuration",
                status=ComponentStatus.BROKEN,
                details="Configuration is missing mandatory '[workspace]' or '[packages.enable]' section.",
                fix_hint="Add '[workspace]' and '[packages.enable]' sections"
            )

        # Validate full workspace config loading
        load_workspace_config(drift_root)
    except Exception as e:
        return CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.BROKEN,
            details=f"Invalid configuration syntax or schema: {e}",
            fix_hint="Fix configuration syntax in drift.toml"
        )

    return CheckResult(
        name="Workspace Configuration",
        status=ComponentStatus.GOOD,
        details="Workspace configuration is valid."
    )


def check_state_registry(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """Checks the deployment state database registry (install/state.toml)."""
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")
    state_file = install_dir / STATE_REGISTRY_FILE_NAME
    if not state_file.exists():
        return CheckResult(
            name="State Registry",
            status=ComponentStatus.NOT_FOUND,
            details=f"'install/{STATE_REGISTRY_FILE_NAME}' not found.",
            fix_hint=f"Create 'install/{STATE_REGISTRY_FILE_NAME}' with '[packages]'"
        )

    from .toml_utils import parse_toml
    try:
        content = state_file.read_text(encoding="utf-8")
        data = parse_toml(content)
        if not isinstance(data, dict):
            return CheckResult(
                name="State Registry",
                status=ComponentStatus.BROKEN,
                details=f"'install/{STATE_REGISTRY_FILE_NAME}' must contain a TOML table.",
                fix_hint=f"Reset 'install/{STATE_REGISTRY_FILE_NAME}' to '[packages]'"
            )
    except Exception as e:
        return CheckResult(
            name="State Registry",
            status=ComponentStatus.BROKEN,
            details=f"Corrupt 'install/{STATE_REGISTRY_FILE_NAME}': {e}",
            fix_hint=f"Fix syntax or reset 'install/{STATE_REGISTRY_FILE_NAME}'"
        )

    return CheckResult(
        name="State Registry",
        status=ComponentStatus.GOOD,
        details="State registry is valid."
    )


def check_render_repo(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """Checks the sandbox render Git repository (render/)."""
    render_dir = workspace_config.render_path if workspace_config is not None else (drift_root / "render")
    if not render_dir.exists():
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.NOT_FOUND,
            details="'render/' directory not found.",
            fix_hint="Initialize 'render/' as a Git repository"
        )

    if not render_dir.is_dir():
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.BROKEN,
            details="'render' exists but is a file, expected a directory.",
            fix_hint="Delete the file 'render' and initialize 'render/' directory"
        )

    git_dir = render_dir / ".git"
    if not git_dir.exists() or not is_git_tracked(render_dir) or is_bare_repository(render_dir):
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.BROKEN,
            details="'render/' is not a valid non-bare Git repository.",
            fix_hint="Run 'git init' inside 'render/'"
        )

    return CheckResult(
        name="Render Sandbox Repo",
        status=ComponentStatus.GOOD,
        details="Render sandbox Git repository is healthy."
    )


def check_install_repo(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """Checks the local state install Git repository (install/)."""
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")
    if not install_dir.exists():
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.NOT_FOUND,
            details="'install/' directory not found.",
            fix_hint="Initialize 'install/' as a Git repository"
        )

    if not install_dir.is_dir():
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.BROKEN,
            details="'install' exists but is a file, expected a directory.",
            fix_hint="Delete the file 'install' and initialize 'install/' directory"
        )

    git_dir = install_dir / ".git"
    if not git_dir.exists() or not is_git_tracked(install_dir) or is_bare_repository(install_dir):
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.BROKEN,
            details="'install/' is not a valid non-bare Git repository.",
            fix_hint="Run 'git init' inside 'install/'"
        )

    return CheckResult(
        name="Install State Repo",
        status=ComponentStatus.GOOD,
        details="Install state Git repository is healthy."
    )


def check_root_gitignore(drift_root: Path) -> CheckResult:
    """Checks that the root .gitignore exists and contains all required isolation entries."""
    gitignore_file = drift_root / ".gitignore"
    if not gitignore_file.exists():
        return CheckResult(
            name="Root .gitignore",
            status=ComponentStatus.NOT_FOUND,
            details="'.gitignore' file not found.",
            fix_hint="Create '.gitignore' with isolation rules"
        )

    try:
        content = gitignore_file.read_text(encoding="utf-8")
    except Exception as e:
        return CheckResult(
            name="Root .gitignore",
            status=ComponentStatus.BROKEN,
            details=f"Unreadable '.gitignore': {e}",
            fix_hint="Ensure '.gitignore' is readable"
        )

    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")}
    normalized = set()
    for l in lines:
        normalized.add(l)
        normalized.add(l.rstrip("/"))

    required = ["render", "install", "*.local.toml", f"{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}"]
    missing = []
    for req in required:
        if req not in normalized and req.rstrip("/") not in normalized:
            missing.append(req)

    if missing:
        return CheckResult(
            name="Root .gitignore",
            status=ComponentStatus.BROKEN,
            details=f"Missing required ignore entries: {missing}",
            fix_hint=f"Add {missing} to '.gitignore'"
        )

    return CheckResult(
        name="Root .gitignore",
        status=ComponentStatus.GOOD,
        details="'.gitignore' contains all required isolation rules."
    )


def check_install_stow_ignore(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """Checks install/.stow-local-ignore configuration."""
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")
    if not install_dir.exists() or not install_dir.is_dir():
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.NOT_FOUND,
            details="'install/' directory does not exist.",
            fix_hint=f"Create 'install/{STOW_LOCAL_IGNORE_FILE_NAME}' after initializing 'install/'"
        )

    stow_ignore_file = install_dir / STOW_LOCAL_IGNORE_FILE_NAME
    if not stow_ignore_file.exists():
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.NOT_FOUND,
            details=f"'install/{STOW_LOCAL_IGNORE_FILE_NAME}' not found.",
            fix_hint=f"Create 'install/{STOW_LOCAL_IGNORE_FILE_NAME}' with '{INSTALL_STOW_IGNORE_PATTERN}'"
        )

    try:
        content = stow_ignore_file.read_text(encoding="utf-8")
    except Exception as e:
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.BROKEN,
            details=f"Unreadable 'install/{STOW_LOCAL_IGNORE_FILE_NAME}': {e}",
            fix_hint=f"Ensure 'install/{STOW_LOCAL_IGNORE_FILE_NAME}' is readable"
        )

    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")}
    if INSTALL_STOW_IGNORE_PATTERN not in lines:
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.BROKEN,
            details=f"'install/{STOW_LOCAL_IGNORE_FILE_NAME}' is missing '{INSTALL_STOW_IGNORE_PATTERN}'.",
            fix_hint=f"Add '{INSTALL_STOW_IGNORE_PATTERN}' to 'install/{STOW_LOCAL_IGNORE_FILE_NAME}'"
        )

    return CheckResult(
        name="Install Stow Ignore",
        status=ComponentStatus.GOOD,
        details=f"'install/{STOW_LOCAL_IGNORE_FILE_NAME}' is configured correctly."
    )


def check_core_dirs(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """Checks the presence of core workspace directories (src/ and config/)."""
    src_dir = workspace_config.source_path if workspace_config is not None else (drift_root / "src")
    config_dir = drift_root / CONFIG_DIR_NAME

    src_exists = src_dir.exists()
    config_exists = config_dir.exists()

    if not src_exists and not config_exists:
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.NOT_FOUND,
            details="'src/' and 'config/' directories not found.",
            fix_hint="Create 'src/' and 'config/' directories"
        )

    if src_exists and not src_dir.is_dir():
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.BROKEN,
            details="'src' exists but is a regular file.",
            fix_hint="Replace file 'src' with directory 'src/'"
        )

    if config_exists and not config_dir.is_dir():
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.BROKEN,
            details="'config' exists but is a regular file.",
            fix_hint="Replace file 'config' with directory 'config/'"
        )

    if not src_exists or not config_exists:
        missing = "src/" if not src_exists else "config/"
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.BROKEN,
            details=f"Incomplete directory structure (missing '{missing}').",
            fix_hint=f"Create missing '{missing}' directory"
        )

    return CheckResult(
        name="Core Directories",
        status=ComponentStatus.GOOD,
        details="Core directories 'src/' and 'config/' exist."
    )


def check_engine_inputs(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> CheckResult:
    """
    Checks the presence of configured render engine input files.
    Returns NOT_FOUND if workspace_config is None, as it is required to determine the expected input files.
    """
    config_dir = drift_root / CONFIG_DIR_NAME
    if workspace_config is None:
        return CheckResult(
            name="Engine Input Files",
            status=ComponentStatus.NOT_FOUND,
            details="Workspace configuration not loaded, cannot determine expected render engine input files.",
            fix_hint="Check the workspace configuration.",
        )

    missing = []
    total = 0
    for engine in workspace_config.render_engine_config.values():
        if not engine.is_disabled:
            total += 1
            input_path = engine.input_file
            if not input_path.is_absolute():
                input_path = config_dir / input_path
            if not input_path.exists():
                missing.append(str(engine.input_file))

    if total == 0:
        return CheckResult(
            name="Engine Input Files",
            status=ComponentStatus.GOOD,
            details="No render engine input files required."
        )

    if len(missing) == total:
        return CheckResult(
            name="Engine Input Files",
            status=ComponentStatus.NOT_FOUND,
            details=f"Render engine input files not found: {missing}",
            fix_hint="Create default render engine input templates"
        )

    if missing:
        return CheckResult(
            name="Engine Input Files",
            status=ComponentStatus.BROKEN,
            details=f"Missing render engine input files: {missing}",
            fix_hint=f"Create missing files: {missing}"
        )

    return CheckResult(
        name="Engine Input Files",
        status=ComponentStatus.GOOD,
        details="All configured render engine input files exist."
    )


def check_existing_workspace_status(
    drift_root: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> WorkspaceHealthReport:
    """Performs a comprehensive health inspection across all workspace subsystems.

    Loads the workspace configuration once at the entry (if not provided) and passes
    it to other component check functions to avoid redundant parsing.

    Combines modular check results using 3-value logic:
      - NOT_FOUND: If all core components are NOT_FOUND (fresh directory).
      - GOOD: If all core components are GOOD (fully initialized & healthy).
      - BROKEN: If any component is BROKEN or there is a mix of GOOD and NOT_FOUND.

    Args:
        drift_root: Path to workspace root.
        workspace_config: Optional pre-loaded WorkspaceConfig instance.

    Returns:
        WorkspaceHealthReport containing overall_status and list of CheckResult objects.
    """
    config_check = check_workspace_config(drift_root)
    ws_config = workspace_config

    if ws_config is None and config_check.status == ComponentStatus.GOOD:
        from .workspace_config import load_workspace_config
        try:
            ws_config = load_workspace_config(drift_root)
        except Exception:
            ws_config = None

    checks = [
        config_check,
        check_state_registry(drift_root, workspace_config=ws_config),
        check_render_repo(drift_root, workspace_config=ws_config),
        check_install_repo(drift_root, workspace_config=ws_config),
        check_root_gitignore(drift_root),
        check_install_stow_ignore(drift_root, workspace_config=ws_config),
        check_core_dirs(drift_root, workspace_config=ws_config),
        check_engine_inputs(drift_root, workspace_config=ws_config),
    ]

    statuses = {c.status for c in checks}

    if all(s == ComponentStatus.NOT_FOUND for s in statuses):
        overall = ComponentStatus.NOT_FOUND
    elif all(s == ComponentStatus.GOOD for s in statuses):
        overall = ComponentStatus.GOOD
    else:
        overall = ComponentStatus.BROKEN

    return WorkspaceHealthReport(overall_status=overall, checks=checks)

