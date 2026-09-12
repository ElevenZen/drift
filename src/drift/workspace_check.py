"""Subsystem health inspection and validation for drift workspaces.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Health Inspection Flow:
    check_existing_workspace_status(drift_root) [Layer 3: Orchestration]
        │
        ├── [Step 1: Workspace Config (Fail-Fast Gatekeeper)] [Layer 1]
        │   └── check_workspace_config(drift_root) -> CheckResult
        │       ├── If BROKEN: Short-circuits immediately, returning BROKEN report
        │       ├── If NOT_FOUND: Fast presence probe (probe_existing_workspace_structure)
        │       │   ├── No drift artifacts -> Returns NOT_FOUND report (fresh workspace)
        │       │   └── Any drift artifact exists -> Returns BROKEN report (incomplete/broken workspace)
        │       └── If GOOD: Proceed to Step 2
        │
        └── [Step 2: Component Health Checks (Require WorkspaceConfig)] [Layer 2]
            ├── check_core_dirs(drift_root, workspace_config)
            ├── check_root_gitignore(drift_root, workspace_config)
            ├── check_render_repo(drift_root, workspace_config)
            ├── check_install_repo(drift_root, workspace_config)
            ├── check_render_gitignore(drift_root, workspace_config)
            ├── check_install_gitignore(drift_root, workspace_config)
            ├── check_install_stow_ignore(drift_root, workspace_config)
            ├── check_state_registry(drift_root, workspace_config)
            └── check_engine_inputs(drift_root, workspace_config)

Status Aggregation Logic (3-Value Logic):
    - NOT_FOUND: Workspace config is NOT_FOUND and no workspace artifacts exist.
    - GOOD: All components are GOOD (fully initialized & healthy workspace).
    - BROKEN: Workspace config is BROKEN/missing with artifacts, or any component is BROKEN.

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Foundation Config & Repo Checks (Step 1 - Gatekeeper)
        probe_existing_workspace_structure
        check_workspace_config
    Layer 2: Component Health Check Handlers (Step 2 - Downstream Components)
        check_core_dirs
        check_root_gitignore
        check_render_repo
        check_install_repo
        check_render_gitignore
        check_install_gitignore
        check_install_stow_ignore
        check_state_registry
        check_engine_inputs
    Layer 3: Inspection Orchestration & Report Models
        ComponentStatus
        CheckResult
        WorkspaceHealthReport
        check_existing_workspace_status
===============================================================================
"""

import logging
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .workspace_config import WorkspaceConfig

from .constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    CURRENT_WORKSPACE_CONFIG_FILE_NAMES,
    LEGACY_WORKSPACE_CONFIG_FILE_NAMES,
    SECRETS_ENV_FILE_NAME,
    STATE_REGISTRY_FILE_NAME,
    INSTALL_STOW_IGNORE_PATTERN,
    STOW_LOCAL_IGNORE_FILE_NAME,
    DEFAULT_ROOT_GITIGNORE_ENTRIES,
    DEFAULT_INTERNAL_GITIGNORE_ENTRIES,
)
from .git_utils import (
    is_git_tracked,
    is_bare_repository,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Data Models
# =====================================================================

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

    def to_repair_result(
        self,
        actions: Sequence[str] = (),
        dry_run: bool = False
    ):
        """Converts this health report and any performed repair actions into a RepairResult object."""
        from .result_models import RepairResult, RepairCheckDetail

        checks_list = [
            RepairCheckDetail(
                name=c.name,
                status=c.status.value,
                details=c.details,
                fix_hint=c.fix_hint
            )
            for c in self.checks
        ]
        return RepairResult(
            overall_health=self.overall_status.value,
            dry_run=dry_run,
            actions_performed=list(actions),
            checks=checks_list
        )


# =====================================================================
# Layer 1: Foundation Config & Repo Checks (Step 1 - Gatekeeper)
# =====================================================================

def probe_existing_workspace_structure(drift_root: Path) -> bool:
    """Probes whether any drift workspace directory or configuration artifact exists.

    Used when config/drift_workspace.toml is NOT_FOUND:
      - Returns False if drift_root is a fresh directory (or only contains a root .git).
      - Returns True if any drift component directories or config files exist,
        indicating a partially initialized or broken workspace.
    """
    candidates = [
        drift_root / "src",
        drift_root / "render",
        drift_root / "install",
        drift_root / CONFIG_DIR_NAME,
        drift_root / "backup",
        drift_root / CONFIG_DIR_NAME / "drift.toml",
        drift_root / CONFIG_DIR_NAME / "drift.local.toml",
        drift_root / CONFIG_DIR_NAME / "drift.envst.toml",
        drift_root / CONFIG_DIR_NAME / "drift.local.envst.toml",
        drift_root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME,
    ]
    return any(p.exists() for p in candidates)


def check_workspace_config(drift_root: Path) -> CheckResult:
    """Checks the workspace configuration file (config/drift_workspace.toml or template).

    If any legacy configuration file (e.g. config/drift.toml or config/drift.local.toml)
    is detected, immediately returns BROKEN so that 'drift repair' can migrate it.
    """
    from .workspace_config import load_workspace_config, render_envst_load_toml

    config_dir = drift_root / CONFIG_DIR_NAME

    # 1. Check for legacy configuration files in config/
    legacy_name_found = next((legacy_name
                             for legacy_name in LEGACY_WORKSPACE_CONFIG_FILE_NAMES
                             if (config_dir / legacy_name).is_file()), None)
    if legacy_name_found is not None:
        return CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.BROKEN,
            details=f"Legacy configuration file '{CONFIG_DIR_NAME}/{legacy_name_found}' detected in workspace.",
            fix_hint="Run 'drift repair' to migrate legacy configuration files"
        )

    # 2. Check for existence of current configuration file or template
    # Base config file missing with override files existence is accepted.
    config_found = next((x for x in CURRENT_WORKSPACE_CONFIG_FILE_NAMES if (config_dir / x).is_file()), None)
    if not config_found:
        return CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.NOT_FOUND,
            details=f"Workspace configuration not found.",
            fix_hint=f"Create default '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_FILE_NAME}'"
        )

    try:
        # Validate full workspace config loading (without legacy check since already checked)
        load_workspace_config(drift_root, check_legacy=False)
    except Exception as e:
        return CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.BROKEN,
            details=f"Invalid configuration syntax or schema: {e}",
            fix_hint="Fix configuration syntax in drift_workspace.toml"
        )

    return CheckResult(
        name="Workspace Configuration",
        status=ComponentStatus.GOOD,
        details="Workspace configuration is valid."
    )


# =====================================================================
# Layer 2: Component Health Check Handlers (Step 2 - Downstream Components)
# =====================================================================

def check_core_dirs(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks the presence of core workspace directories (src/ and config/)."""
    src_dir = workspace_config.source_path
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
            details=f"'{src_dir.name}' exists but is a regular file.",
            fix_hint=f"Replace file '{src_dir.name}' with directory '{src_dir.name}/'"
        )

    if config_exists and not config_dir.is_dir():
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.BROKEN,
            details="'config' exists but is a regular file.",
            fix_hint="Replace file 'config' with directory 'config/'"
        )

    if not src_exists or not config_exists:
        missing = f"{src_dir.name}/" if not src_exists else "config/"
        return CheckResult(
            name="Core Directories",
            status=ComponentStatus.BROKEN,
            details=f"Incomplete directory structure (missing '{missing}').",
            fix_hint=f"Create missing '{missing}' directory"
        )

    return CheckResult(
        name="Core Directories",
        status=ComponentStatus.GOOD,
        details=f"Core directories '{src_dir.name}/' and 'config/' exist."
    )


def check_root_gitignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
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
    normalized = {l.rstrip("/") for l in lines} | lines

    required = [
        workspace_config.render_path.name,
        workspace_config.install_path.name,
        *[entry for entry in DEFAULT_ROOT_GITIGNORE_ENTRIES if entry not in ("render/", "install/")]
    ]
    missing = list(filter(lambda req: req not in normalized and req.rstrip("/") not in normalized, required))

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


def check_render_repo(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks the sandbox render Git repository (render/)."""
    render_dir = workspace_config.render_path
    if not render_dir.exists():
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{render_dir.name}/' directory not found.",
            fix_hint=f"Initialize '{render_dir.name}/' as a Git repository"
        )

    if not render_dir.is_dir():
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.BROKEN,
            details=f"'{render_dir.name}' exists but is a file, expected a directory.",
            fix_hint=f"Delete the file '{render_dir.name}' and initialize '{render_dir.name}/' directory"
        )

    git_dir = render_dir / ".git"
    if not git_dir.exists() or not is_git_tracked(render_dir) or is_bare_repository(render_dir):
        return CheckResult(
            name="Render Sandbox Repo",
            status=ComponentStatus.BROKEN,
            details=f"'{render_dir.name}/' is not a valid non-bare Git repository.",
            fix_hint=f"Run 'git init' inside '{render_dir.name}/'"
        )

    return CheckResult(
        name="Render Sandbox Repo",
        status=ComponentStatus.GOOD,
        details="Render sandbox Git repository is healthy."
    )


def check_install_repo(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks the local state install Git repository (install/)."""
    install_dir = workspace_config.install_path
    if not install_dir.exists():
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{install_dir.name}/' directory not found.",
            fix_hint=f"Initialize '{install_dir.name}/' as a Git repository"
        )

    if not install_dir.is_dir():
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.BROKEN,
            details=f"'{install_dir.name}' exists but is a file, expected a directory.",
            fix_hint=f"Delete the file '{install_dir.name}' and initialize '{install_dir.name}/' directory"
        )

    git_dir = install_dir / ".git"
    if not git_dir.exists() or not is_git_tracked(install_dir) or is_bare_repository(install_dir):
        return CheckResult(
            name="Install State Repo",
            status=ComponentStatus.BROKEN,
            details=f"'{install_dir.name}/' is not a valid non-bare Git repository.",
            fix_hint=f"Run 'git init' inside '{install_dir.name}/'"
        )

    return CheckResult(
        name="Install State Repo",
        status=ComponentStatus.GOOD,
        details="Install state Git repository is healthy."
    )


def check_internal_gitignore_file(
    gitignore_file: Path,
    component_name: str,
    dir_name: str,
) -> CheckResult:
    """Checks that an internal .gitignore exists and contains standard ignore entries."""
    if not gitignore_file.exists():
        return CheckResult(
            name=component_name,
            status=ComponentStatus.NOT_FOUND,
            details=f"'{dir_name}/.gitignore' not found.",
            fix_hint=f"Create '{dir_name}/.gitignore' with default ignore rules"
        )

    try:
        content = gitignore_file.read_text(encoding="utf-8")
    except Exception as e:
        return CheckResult(
            name=component_name,
            status=ComponentStatus.BROKEN,
            details=f"Unreadable '{dir_name}/.gitignore': {e}",
            fix_hint=f"Ensure '{dir_name}/.gitignore' is readable"
        )

    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")}
    normalized = {l.rstrip("/") for l in lines} | lines

    missing = list(filter(lambda entry: entry not in normalized and entry.rstrip("/") not in normalized, DEFAULT_INTERNAL_GITIGNORE_ENTRIES))

    if missing:
        return CheckResult(
            name=component_name,
            status=ComponentStatus.BROKEN,
            details=f"'{dir_name}/.gitignore' missing rules: {missing}",
            fix_hint=f"Add missing rules to '{dir_name}/.gitignore'"
        )

    return CheckResult(
        name=component_name,
        status=ComponentStatus.GOOD,
        details=f"'{dir_name}/.gitignore' is present and up to date."
    )


def check_render_gitignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks render/.gitignore configuration."""
    render_dir = workspace_config.render_path
    if not render_dir.exists() or not render_dir.is_dir():
        return CheckResult(
            name="Render .gitignore",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{render_dir.name}/' directory does not exist.",
            fix_hint=f"Initialize '{render_dir.name}/' repository first"
        )
    return check_internal_gitignore_file(render_dir / ".gitignore", "Render .gitignore", render_dir.name)


def check_install_gitignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks install/.gitignore configuration."""
    install_dir = workspace_config.install_path
    if not install_dir.exists() or not install_dir.is_dir():
        return CheckResult(
            name="Install .gitignore",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{install_dir.name}/' directory does not exist.",
            fix_hint=f"Initialize '{install_dir.name}/' repository first"
        )
    return check_internal_gitignore_file(install_dir / ".gitignore", "Install .gitignore", install_dir.name)


def check_install_stow_ignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks install/.stow-local-ignore configuration."""
    install_dir = workspace_config.install_path
    if not install_dir.exists() or not install_dir.is_dir():
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{install_dir.name}/' directory does not exist.",
            fix_hint=f"Create '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' after initializing '{install_dir.name}/'"
        )

    stow_ignore_file = install_dir / STOW_LOCAL_IGNORE_FILE_NAME
    if not stow_ignore_file.exists():
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' not found.",
            fix_hint=f"Create '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' with '{INSTALL_STOW_IGNORE_PATTERN}'"
        )

    try:
        content = stow_ignore_file.read_text(encoding="utf-8")
    except Exception as e:
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.BROKEN,
            details=f"Unreadable '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}': {e}",
            fix_hint=f"Ensure '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' is readable"
        )

    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")}
    if INSTALL_STOW_IGNORE_PATTERN not in lines:
        return CheckResult(
            name="Install Stow Ignore",
            status=ComponentStatus.BROKEN,
            details=f"'{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' is missing '{INSTALL_STOW_IGNORE_PATTERN}'.",
            fix_hint=f"Add '{INSTALL_STOW_IGNORE_PATTERN}' to '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}'"
        )

    return CheckResult(
        name="Install Stow Ignore",
        status=ComponentStatus.GOOD,
        details=f"'{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}' is configured correctly."
    )


def check_state_registry(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks the deployment state database registry (install/state.toml)."""
    install_dir = workspace_config.install_path
    state_file = install_dir / STATE_REGISTRY_FILE_NAME
    if not state_file.exists():
        return CheckResult(
            name="State Registry",
            status=ComponentStatus.NOT_FOUND,
            details=f"'{install_dir.name}/{STATE_REGISTRY_FILE_NAME}' not found.",
            fix_hint=f"Create '{install_dir.name}/{STATE_REGISTRY_FILE_NAME}' with '[packages]'"
        )

    from .toml_utils import parse_toml
    try:
        content = state_file.read_text(encoding="utf-8")
        data = parse_toml(content)
        if not isinstance(data, dict):
            return CheckResult(
                name="State Registry",
                status=ComponentStatus.BROKEN,
                details=f"'{install_dir.name}/{STATE_REGISTRY_FILE_NAME}' must contain a TOML table.",
                fix_hint=f"Reset '{install_dir.name}/{STATE_REGISTRY_FILE_NAME}' to '[packages]'"
            )
    except Exception as e:
        return CheckResult(
            name="State Registry",
            status=ComponentStatus.BROKEN,
            details=f"Corrupt '{install_dir.name}/{STATE_REGISTRY_FILE_NAME}': {e}",
            fix_hint=f"Fix syntax or reset '{install_dir.name}/{STATE_REGISTRY_FILE_NAME}'"
        )

    return CheckResult(
        name="State Registry",
        status=ComponentStatus.GOOD,
        details="State registry is valid."
    )


def check_engine_inputs(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
) -> CheckResult:
    """Checks the presence of configured render engine input files."""
    config_dir = drift_root / CONFIG_DIR_NAME

    missing = []
    total = 0
    for engine in workspace_config.render_engine_configs.values():
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


# =====================================================================
# Layer 3: Inspection Orchestration (Public Entry Point)
# =====================================================================

def check_existing_workspace_status(
    drift_root: Path,
) -> WorkspaceHealthReport:
    """Performs a comprehensive health inspection across all workspace subsystems.

    Step 1 inspects the workspace configuration (config/drift_workspace.toml).
    - If the configuration is BROKEN, execution short-circuits immediately and
      returns a BROKEN WorkspaceHealthReport containing the configuration error.
    - If the configuration is NOT_FOUND, probes for existing workspace structure:
      - If no Drift artifacts exist -> returns a NOT_FOUND report (fresh workspace).
      - If Drift artifacts exist -> returns a BROKEN report (incomplete/broken workspace).
    - If the configuration is GOOD, loads WorkspaceConfig and executes all downstream
      component checks in Step 2.

    Combines modular check results using 3-value logic:
      - NOT_FOUND: If workspace config is NOT_FOUND and no workspace artifacts exist.
      - GOOD: If all core components are GOOD (fully initialized & healthy).
      - BROKEN: If any component is BROKEN or there is a mix of GOOD and NOT_FOUND.

    Args:
        drift_root: Path to workspace root.

    Returns:
        WorkspaceHealthReport containing overall_status and list of CheckResult objects.
    """
    drift_root = Path(drift_root).resolve()

    # 1. Step 1: Workspace Configuration Check (Fail-Fast Gatekeeper)
    config_check = check_workspace_config(drift_root)
    if config_check.status == ComponentStatus.BROKEN:
        return WorkspaceHealthReport(overall_status=ComponentStatus.BROKEN, checks=[config_check])

    if config_check.status == ComponentStatus.NOT_FOUND:
        if not probe_existing_workspace_structure(drift_root):
            return WorkspaceHealthReport(overall_status=ComponentStatus.NOT_FOUND, checks=[config_check])
        return WorkspaceHealthReport(overall_status=ComponentStatus.BROKEN, checks=[config_check])

    from .workspace_config import load_workspace_config
    try:
        ws_config = load_workspace_config(drift_root, check_legacy=False)
    except Exception as e:
        broken_check = CheckResult(
            name="Workspace Configuration",
            status=ComponentStatus.BROKEN,
            details=f"Failed to load workspace configuration: {e}",
            fix_hint="Fix configuration syntax in drift_workspace.toml"
        )
        return WorkspaceHealthReport(overall_status=ComponentStatus.BROKEN, checks=[broken_check])

    # 2. Step 2: Component Health Checks (ordered to match repair pipeline)
    checks = [
        config_check,
        check_core_dirs(drift_root, workspace_config=ws_config),
        check_root_gitignore(drift_root, workspace_config=ws_config),
        check_render_repo(drift_root, workspace_config=ws_config),
        check_install_repo(drift_root, workspace_config=ws_config),
        check_render_gitignore(drift_root, workspace_config=ws_config),
        check_install_gitignore(drift_root, workspace_config=ws_config),
        check_install_stow_ignore(drift_root, workspace_config=ws_config),
        check_state_registry(drift_root, workspace_config=ws_config),
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


# Alias for unified naming convention
check_drift_workspace = check_existing_workspace_status
