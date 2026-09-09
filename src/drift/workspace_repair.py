"""Feature implementation for repairing a damaged or partially-initialized drift workspace."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Callable, Tuple

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

from .constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    STATE_REGISTRY_FILE_NAME,
    INSTALL_STOW_IGNORE_PATTERN,
    STOW_LOCAL_IGNORE_FILE_NAME,
    get_default_drift_workspace_toml_content,
    DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT,
    get_default_secrets_env_content,
    get_default_envsubst_content,
    get_default_mustache_content,
    get_default_jinja2_content,
)
from .ignore import get_default_install_stow_ignore_content
from .check_repo import (
    ComponentStatus,
    WorkspaceHealthReport,
    check_root_gitignore,
    check_render_repo,
    check_install_repo,
    check_install_stow_ignore,
    check_state_registry,
    check_workspace_config,
    check_core_dirs,
    check_engine_inputs,
)
from .git_utils import (
    git_init_repo,
    append_to_gitignore,
)

logger = logging.getLogger(__name__)


def repair_core_directories(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs core workspace directories (src/ and config/)."""
    actions: List[str] = []
    src_dir = workspace_config.source_path if workspace_config is not None else (drift_root / "src")
    config_dir = drift_root / CONFIG_DIR_NAME

    if not src_dir.exists():
        actions.append("Created missing 'src/' directory.")
        if not dry_run:
            src_dir.mkdir(parents=True, exist_ok=True)

    if not config_dir.exists():
        actions.append(f"Created missing '{CONFIG_DIR_NAME}/' directory.")
        if not dry_run:
            config_dir.mkdir(parents=True, exist_ok=True)

    return actions


def repair_gitignore(drift_root: Path, dry_run: bool = False) -> List[str]:
    """Repairs root .gitignore rules."""
    actions: List[str] = []
    gitignore_res = check_root_gitignore(drift_root)
    if gitignore_res.status != ComponentStatus.GOOD:
        actions.append("Updated '.gitignore' with required workspace isolation entries.")
        if not dry_run:
            append_to_gitignore(drift_root, [
                "render/",
                "install/",
                "*.local.toml",
                f"{CONFIG_DIR_NAME}/{SECRETS_ENV_FILE_NAME}"
            ])
    return actions


def repair_render_repo(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs the render/ sandbox Git repository."""
    actions: List[str] = []
    render_res = check_render_repo(drift_root, workspace_config=workspace_config)
    render_dir = workspace_config.render_path if workspace_config is not None else (drift_root / "render")

    if render_res.status == ComponentStatus.GOOD:
        return actions

    if render_dir.exists() and not render_dir.is_dir():
        actions.append("⚠️ Error: 'render' exists as a regular file. Expected a directory.")
        return actions

    git_dir = render_dir / ".git"
    if not git_dir.exists():
        actions.append("Initialized 'render/' sandbox Git repository.")
        if not dry_run:
            git_init_repo(render_dir, "render")
    else:
        actions.append(f"⚠️ Error in 'render/' Git repository: {render_res.details}. Manual resolution required.")

    return actions


def repair_install_repo(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs the install/ local state Git repository."""
    actions: List[str] = []
    install_res = check_install_repo(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")

    if install_res.status == ComponentStatus.GOOD:
        return actions

    if install_dir.exists() and not install_dir.is_dir():
        actions.append("⚠️ Error: 'install' exists as a regular file. Expected a directory.")
        return actions

    git_dir = install_dir / ".git"
    if not git_dir.exists():
        actions.append("Initialized 'install/' local state Git repository.")
        if not dry_run:
            git_init_repo(install_dir, "install")
    else:
        actions.append(f"⚠️ Error in 'install/' Git repository: {install_res.details}. Manual resolution required.")

    return actions


def repair_install_stow_ignore(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs install/.stow-local-ignore configuration."""
    actions: List[str] = []
    stow_ignore_res = check_install_stow_ignore(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")
    stow_ignore_path = install_dir / STOW_LOCAL_IGNORE_FILE_NAME

    if stow_ignore_res.status != ComponentStatus.GOOD:
        actions.append(f"Restored 'install/{STOW_LOCAL_IGNORE_FILE_NAME}'.")
        if not dry_run:
            install_dir.mkdir(parents=True, exist_ok=True)
            stow_ignore_path.write_text(get_default_install_stow_ignore_content(), encoding="utf-8")
    return actions


def repair_state_registry(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs install/state.toml registry database."""
    actions: List[str] = []
    state_res = check_state_registry(drift_root, workspace_config=workspace_config)
    install_dir = workspace_config.install_path if workspace_config is not None else (drift_root / "install")
    state_file = install_dir / STATE_REGISTRY_FILE_NAME

    if state_res.status != ComponentStatus.GOOD:
        actions.append(f"Restored 'install/{STATE_REGISTRY_FILE_NAME}' registry database.")
        if not dry_run:
            install_dir.mkdir(parents=True, exist_ok=True)
            state_file.write_text("[packages]\n", encoding="utf-8")
    return actions


def repair_workspace_config(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs workspace configuration, renaming legacy config files or generating default if missing."""
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
    elif config_res.status == ComponentStatus.BROKEN:
        actions.append(f"⚠️ Warning: '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_FILE_NAME}' is invalid ({config_res.details}). Manual inspection required.")

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

    return actions


def repair_secrets_env(drift_root: Path, dry_run: bool = False) -> List[str]:
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
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs configured render engine input files."""
    actions: List[str] = []
    config_dir = drift_root / CONFIG_DIR_NAME

    ws_config = workspace_config
    if ws_config is None:
        from .workspace_config import load_workspace_config
        try:
            ws_config = load_workspace_config(drift_root)
        except Exception:
            ws_config = None

    if ws_config is None:
        # Fallback to checking default engine files if config is not loadable
        default_templates = [
            ("envsubst.bash", get_default_envsubst_content()),
            ("mustache.envst.json", get_default_mustache_content()),
            ("jinja2.mustache.json", get_default_jinja2_content()),
        ]
        for fname, content in default_templates:
            fpath = config_dir / fname
            if not fpath.exists():
                actions.append(f"Created default '{CONFIG_DIR_NAME}/{fname}'.")
                if not dry_run:
                    config_dir.mkdir(parents=True, exist_ok=True)
                    fpath.write_text(content, encoding="utf-8")
        return actions

    configured_engines = list(ws_config.render_engine_configs.values())
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


def repair_drift_workspace(
    drift_root: Path,
    dry_run: bool = False,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> List[str]:
    """Repairs missing or broken components in a drift workspace non-destructively.

    Args:
        drift_root: Absolute or resolved Path to workspace root.
        dry_run: If True, only returns list of actions without applying changes.
        workspace_config: Optional pre-loaded WorkspaceConfig instance.

    Returns:
        List of repair actions taken (or planned in dry-run mode).
    """
    drift_root = Path(drift_root).resolve()
    actions: List[str] = []

    ws_config = workspace_config
    if ws_config is None:
        config_file = drift_root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME
        envst_file = drift_root / CONFIG_DIR_NAME / f"{WORKSPACE_CONFIG_FILE_NAME.split('.')[0]}.envst.toml"
        if config_file.exists() or envst_file.exists():
            from .workspace_config import load_workspace_config
            try:
                ws_config = load_workspace_config(drift_root)
            except Exception:
                ws_config = None

    # Execute all modular repair steps
    actions.extend(repair_core_directories(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_gitignore(drift_root, dry_run=dry_run))
    actions.extend(repair_render_repo(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_install_repo(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_install_stow_ignore(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_state_registry(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_workspace_config(drift_root, dry_run=dry_run, workspace_config=ws_config))
    actions.extend(repair_secrets_env(drift_root, dry_run=dry_run))
    actions.extend(repair_engine_inputs(drift_root, dry_run=dry_run, workspace_config=ws_config))

    return actions


def build_repair_result(
    report: "WorkspaceHealthReport",
    actions: Optional[List[str]] = None,
    dry_run: bool = False
):
    """Converts a WorkspaceHealthReport and performed actions into a RepairResult object."""
    return report.to_repair_result(actions=actions, dry_run=dry_run)

