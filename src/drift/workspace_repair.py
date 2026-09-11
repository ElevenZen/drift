"""Feature implementation for repairing a damaged or partially-initialized drift workspace.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Repair Pipeline Flow:
    repair_drift_workspace(drift_root, dry_run) [Layer 3: Pipeline Orchestration]
        │
        ├── [Step 1: Workspace Config (Fail-Fast Foundation)] [Layer 1]
        │   └── repair_workspace_config(drift_root, dry_run) -> (actions, WorkspaceConfig)
        │       ├── Migrates legacy filenames (drift.toml -> drift_workspace.toml)
        │       ├── Generates default configuration if missing
        │       ├── Validates & loads WorkspaceConfig (raises ConfigError immediately if broken)
        │
        └── [Step 2: Component Repair Steps (Require WorkspaceConfig)] [Layer 2]
            ├── repair_core_directories(drift_root, workspace_config, dry_run)
            ├── repair_gitignore(drift_root, workspace_config, dry_run)
            ├── repair_render_repo(drift_root, workspace_config, dry_run)
            ├── repair_install_repo(drift_root, workspace_config, dry_run)
            ├── repair_internal_gitignores(drift_root, workspace_config, dry_run)
            ├── repair_install_stow_ignore(drift_root, workspace_config, dry_run)
            ├── repair_state_registry(drift_root, workspace_config, dry_run)
            ├── repair_secrets_env(drift_root, workspace_config, dry_run)
            └── repair_engine_inputs(drift_root, workspace_config, dry_run)

Result Formatting:
    build_repair_result(report, actions, dry_run) [Layer 3: Pipeline Orchestration]

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Foundation Config Repair (Step 1 - Fail Fast)
        repair_workspace_config
    Layer 2: Downstream Component Repair Handlers (Step 2 - Require WorkspaceConfig)
        repair_core_directories
        repair_gitignore
        repair_render_repo
        repair_install_repo
        repair_internal_gitignores
        repair_install_stow_ignore
        repair_state_registry
        repair_secrets_env
        repair_engine_inputs
    Layer 3: Pipeline Orchestration & Reporting
        repair_drift_workspace
        build_repair_result
===============================================================================
"""

import logging
from pathlib import Path
from typing import List, Sequence, Tuple

from .constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    STATE_REGISTRY_FILE_NAME,
    STOW_LOCAL_IGNORE_FILE_NAME,
    get_default_drift_workspace_toml_content,
    DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT,
    get_default_secrets_env_content,
    get_default_envsubst_content,
    get_default_mustache_content,
    get_default_jinja2_content,
    get_default_internal_gitignore_content,
)
from .ignore import get_default_install_stow_ignore_content
from .workspace_check import (
    ComponentStatus,
    WorkspaceHealthReport,
    check_root_gitignore,
    check_render_repo,
    check_install_repo,
    check_render_gitignore,
    check_install_gitignore,
    check_install_stow_ignore,
    check_state_registry,
    check_workspace_config,
)
from .git_utils import (
    git_init_repo,
    append_to_gitignore,
)
from .exceptions import ConfigError
from .workspace_config import WorkspaceConfig, load_workspace_config
from .toml_utils import parse_toml

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Foundation Config Repair (Step 1 - Fail Fast)
# =====================================================================

def repair_workspace_config(
    drift_root: Path,
    dry_run: bool = False,
) -> Tuple[List[str], WorkspaceConfig]:
    """Repairs workspace configuration, renaming legacy config files or generating default if missing.

    Returns:
        A tuple of (actions_list, loaded_workspace_config).

    Raises:
        ConfigError: If workspace configuration is invalid and cannot be repaired/loaded.
    """
    actions: List[str] = []
    config_dir = drift_root / CONFIG_DIR_NAME
    config_file = config_dir / WORKSPACE_CONFIG_FILE_NAME
    local_config_file = config_dir / WORKSPACE_CONFIG_LOCAL_FILE_NAME

    # 1. Detect and rename legacy workspace configuration files
    legacy_mappings = [
        ("drift.toml", WORKSPACE_CONFIG_FILE_NAME),
        ("drift.local.toml", WORKSPACE_CONFIG_LOCAL_FILE_NAME),
        ("drift.envst.toml", f"{WORKSPACE_CONFIG_FILE_NAME.split('.')[0]}.envst.toml"),
        ("drift.local.envst.toml", f"{WORKSPACE_CONFIG_LOCAL_FILE_NAME.split('.')[0]}.local.envst.toml"),
    ]

    has_legacy_main = False
    for old_name, new_name in legacy_mappings:
        old_path = config_dir / old_name
        if not old_path.is_file():
            continue
        target_path = config_dir / new_name

        if target_path.exists():
            actions.append(
                f"⚠️ Found legacy file '{CONFIG_DIR_NAME}/{old_name}', but '{CONFIG_DIR_NAME}/{new_name}' already exists. "
                f"Please migrate and remove '{old_name}' manually."
            )
            continue

        if old_name in ("drift.toml", "drift.envst.toml"):
            has_legacy_main = True

        actions.append(f"Renamed legacy workspace configuration file '{CONFIG_DIR_NAME}/{old_name}' to '{CONFIG_DIR_NAME}/{new_name}'.")
        if not dry_run:
            old_path.rename(target_path)

    # 2. Check if main workspace configuration needs to be generated or inspected
    main_config_files = [
        config_file,
        config_dir / f"{WORKSPACE_CONFIG_FILE_NAME.split('.')[0]}.envst.toml",
        config_dir / "drift.toml",
        config_dir / "drift.envst.toml",
    ]
    main_config_occupied = any(f.exists() for f in main_config_files)
    config_res = check_workspace_config(drift_root)
    if config_res.status == ComponentStatus.NOT_FOUND:
        if not dry_run:
            config_dir.mkdir(parents=True, exist_ok=True)
            config_file.write_text(get_default_drift_workspace_toml_content(), encoding="utf-8")
        if not main_config_occupied:
            actions.append(f"Generated default '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_FILE_NAME}'.")
    elif config_res.status == ComponentStatus.BROKEN and not has_legacy_main:
        raise ConfigError(
            f"Workspace configuration at '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_FILE_NAME}' is invalid ({config_res.details}). "
            f"Manual inspection required."
        )

    # 3. Check and restore drift_workspace.local.toml template if missing
    local_config_files = [
        local_config_file,
        config_dir / f"{WORKSPACE_CONFIG_LOCAL_FILE_NAME.split('.')[0]}.local.envst.toml",
        config_dir / "drift.local.toml",
        config_dir / "drift.local.envst.toml",
    ]
    local_config_occupied = any(f.exists() for f in local_config_files)
    if not local_config_occupied:
        if not dry_run:
            config_dir.mkdir(parents=True, exist_ok=True)
            local_config_file.write_text(DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT, encoding="utf-8")
        actions.append(f"Generated '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_LOCAL_FILE_NAME}' template.")

    # 4. Load and validate WorkspaceConfig
    try:
        if dry_run and not config_file.exists() and not (config_dir / f"{WORKSPACE_CONFIG_FILE_NAME.split('.')[0]}.envst.toml").exists():
            if (config_dir / "drift.toml").is_file():
                d = parse_toml((config_dir / "drift.toml").read_text(encoding="utf-8"))
                ws_config = WorkspaceConfig.from_dict(d, drift_root=drift_root)
            else:
                default_dict = parse_toml(get_default_drift_workspace_toml_content())
                ws_config = WorkspaceConfig.from_dict(default_dict, drift_root=drift_root)
        else:
            ws_config = load_workspace_config(drift_root)
    except Exception as e:
        raise ConfigError(f"Failed to load workspace configuration during repair: {e}") from e

    return actions, ws_config


# =====================================================================
# Layer 2: Downstream Component Repair Handlers (Step 2 - Require WorkspaceConfig)
# =====================================================================

def repair_core_directories(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs core workspace directories (src/ and config/)."""
    actions: List[str] = []
    src_dir = workspace_config.source_path
    config_dir = drift_root / CONFIG_DIR_NAME

    if not src_dir.exists():
        actions.append("Created missing 'src/' directory." if src_dir == drift_root / "src" else f"Created missing '{src_dir.name}/' directory.")
        if not dry_run:
            src_dir.mkdir(parents=True, exist_ok=True)

    if not config_dir.exists():
        actions.append(f"Created missing '{CONFIG_DIR_NAME}/' directory.")
        if not dry_run:
            config_dir.mkdir(parents=True, exist_ok=True)

    return actions


def repair_gitignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs root .gitignore rules."""
    actions: List[str] = []
    gitignore_res = check_root_gitignore(drift_root, workspace_config=workspace_config)
    if gitignore_res.status != ComponentStatus.GOOD:
        actions.append("Updated '.gitignore' with required workspace isolation entries.")
        if not dry_run:
            append_to_gitignore(drift_root, [
                f"{workspace_config.render_path.name}/",
                f"{workspace_config.install_path.name}/",
                "*.local.toml",
                f"{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}"
            ])
    return actions


def repair_render_repo(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs the render/ sandbox Git repository."""
    actions: List[str] = []
    render_res = check_render_repo(drift_root, workspace_config=workspace_config)
    render_dir = workspace_config.render_path

    if render_res.status == ComponentStatus.GOOD:
        return actions

    if render_dir.exists() and not render_dir.is_dir():
        actions.append(f"⚠️ Error: '{render_dir.name}' exists as a regular file. Expected a directory.")
        return actions

    git_dir = render_dir / ".git"
    if not git_dir.exists():
        actions.append(f"Initialized '{render_dir.name}/' sandbox Git repository.")
        if not dry_run:
            git_init_repo(render_dir, "render")
    else:
        actions.append(f"⚠️ Error in '{render_dir.name}/' Git repository: {render_res.details}. Manual resolution required.")

    return actions


def repair_install_repo(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs the install/ local state Git repository."""
    actions: List[str] = []
    install_res = check_install_repo(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path

    if install_res.status == ComponentStatus.GOOD:
        return actions

    if install_dir.exists() and not install_dir.is_dir():
        actions.append(f"⚠️ Error: '{install_dir.name}' exists as a regular file. Expected a directory.")
        return actions

    git_dir = install_dir / ".git"
    if not git_dir.exists():
        actions.append(f"Initialized '{install_dir.name}/' local state Git repository.")
        if not dry_run:
            git_init_repo(install_dir, "install")
    else:
        actions.append(f"⚠️ Error in '{install_dir.name}/' Git repository: {install_res.details}. Manual resolution required.")

    return actions


def repair_internal_gitignores(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs .gitignore files inside render/ and install/ internal repositories."""
    actions: List[str] = []
    render_dir = workspace_config.render_path
    install_dir = workspace_config.install_path

    render_check = check_render_gitignore(drift_root, workspace_config=workspace_config)
    if render_check.status != ComponentStatus.GOOD and render_dir.exists() and render_dir.is_dir():
        actions.append(f"Restored '{render_dir.name}/.gitignore'.")
        if not dry_run:
            (render_dir / ".gitignore").write_text(get_default_internal_gitignore_content(), encoding="utf-8")

    install_check = check_install_gitignore(drift_root, workspace_config=workspace_config)
    if install_check.status != ComponentStatus.GOOD and install_dir.exists() and install_dir.is_dir():
        actions.append(f"Restored '{install_dir.name}/.gitignore'.")
        if not dry_run:
            (install_dir / ".gitignore").write_text(get_default_internal_gitignore_content(), encoding="utf-8")

    return actions


def repair_install_stow_ignore(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs install/.stow-local-ignore configuration."""
    actions: List[str] = []
    stow_ignore_res = check_install_stow_ignore(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path
    stow_ignore_path = install_dir / STOW_LOCAL_IGNORE_FILE_NAME

    if stow_ignore_res.status != ComponentStatus.GOOD:
        actions.append(f"Restored '{install_dir.name}/{STOW_LOCAL_IGNORE_FILE_NAME}'.")
        if not dry_run:
            install_dir.mkdir(parents=True, exist_ok=True)
            stow_ignore_path.write_text(get_default_install_stow_ignore_content(), encoding="utf-8")
    return actions


def repair_state_registry(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs install/state.toml registry database."""
    actions: List[str] = []
    state_res = check_state_registry(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path
    state_file = install_dir / STATE_REGISTRY_FILE_NAME

    if state_res.status != ComponentStatus.GOOD:
        actions.append(f"Restored '{install_dir.name}/{STATE_REGISTRY_FILE_NAME}' registry database.")
        if not dry_run:
            install_dir.mkdir(parents=True, exist_ok=True)
            state_file.write_text("[packages]\n", encoding="utf-8")
    return actions


def repair_secrets_env(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs config/secrets.env template if missing."""
    actions: List[str] = []
    config_dir = drift_root / CONFIG_DIR_NAME
    secrets_file = config_dir / SECRETS_ENV_FILE_NAME

    if not secrets_file.exists():
        actions.append(f"Generated '{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}' template.")
        if not dry_run:
            config_dir.mkdir(parents=True, exist_ok=True)
            secrets_file.write_text(get_default_secrets_env_content(), encoding="utf-8")
    return actions


def repair_engine_inputs(
    drift_root: Path,
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """Repairs configured render engine input files."""
    actions: List[str] = []
    config_dir = drift_root / CONFIG_DIR_NAME

    configured_engines = list(workspace_config.render_engine_configs.values())
    for engine in configured_engines:
        if engine.is_disabled:
            actions.append(f"⚠️ Warning: Render engine '{engine.name}' has no input file configured. Manual creation required.")
            continue
        input_path = config_dir / engine.input_file

        if input_path.exists():
            continue

        filename = input_path.name
        if filename == "envsubst.bash":
            actions.append(f"Created default '{CONFIG_DIR_NAME}/envsubst.bash'.")
            if not dry_run:
                config_dir.mkdir(parents=True, exist_ok=True)
                input_path.write_text(get_default_envsubst_content(), encoding="utf-8")
        elif filename == "mustache.envst.json":
            actions.append(f"Created default '{CONFIG_DIR_NAME}/mustache.envst.json'.")
            if not dry_run:
                config_dir.mkdir(parents=True, exist_ok=True)
                input_path.write_text(get_default_mustache_content(), encoding="utf-8")
        elif filename == "jinja2.mustache.json":
            actions.append(f"Created default '{CONFIG_DIR_NAME}/jinja2.mustache.json'.")
            if not dry_run:
                config_dir.mkdir(parents=True, exist_ok=True)
                input_path.write_text(get_default_jinja2_content(), encoding="utf-8")
        else:
            actions.append(f"⚠️ Warning: Missing custom engine input file '{engine.input_file}'. Manual creation required.")

    return actions


# =====================================================================
# Layer 3: Pipeline Orchestration & Reporting
# =====================================================================

def repair_drift_workspace(
    drift_root: Path,
    dry_run: bool = False,
) -> List[str]:
    """Repairs missing or broken components in a drift workspace non-destructively.

    Step 1 repairs and validates the workspace configuration. If it fails,
    ConfigError is raised immediately. Subsequent steps consume the validated
    WorkspaceConfig object as a required input.

    Args:
        drift_root: Absolute or resolved Path to workspace root.
        dry_run: If True, only returns list of actions without applying changes.

    Returns:
        List of repair actions taken (or planned in dry-run mode).

    Raises:
        ConfigError: If workspace configuration repair or loading fails.
    """
    drift_root = Path(drift_root).resolve()
    actions: List[str] = []

    # 1. Step 1: Repair workspace configuration FIRST and obtain validated WorkspaceConfig.
    # If this fails, an exception is raised immediately.
    cfg_actions, ws_config = repair_workspace_config(drift_root, dry_run=dry_run)
    actions.extend(cfg_actions)

    # 2. Later steps take validated workspace_config as a REQUIRED input
    actions.extend(repair_core_directories(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_gitignore(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_render_repo(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_install_repo(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_internal_gitignores(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_install_stow_ignore(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_state_registry(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_secrets_env(drift_root, workspace_config=ws_config, dry_run=dry_run))
    actions.extend(repair_engine_inputs(drift_root, workspace_config=ws_config, dry_run=dry_run))

    return actions


def build_repair_result(
    report: "WorkspaceHealthReport",
    actions: Sequence[str] = (),
    dry_run: bool = False
):
    """Converts a WorkspaceHealthReport and performed actions into a RepairResult object."""
    return report.to_repair_result(actions=actions, dry_run=dry_run)
