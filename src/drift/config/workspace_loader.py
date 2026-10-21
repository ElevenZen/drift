"""Workspace configuration loading, legacy migration assertion, and layered pipeline.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Workspace Discovery & High-Level Pipeline Loaders
    - load_workspace_config(): Full multi-stage loading, hook preprocessing, and resolution
    - load_workspace_config_files_layered(): Sequential layered TOML loading & deep-merging

Layer 1: Low-Level Template Rendering & Migration Guards
    - locate_workspace_config_file(): Static file > .envst.toml template > None precedence
    - parse_workspace_config_content(): Renders (if .envst) and parses in-memory TOML content
    - render_workspace_config_content() / render_workspace_config(): Python envsubst template renderer
    - load_workspace_config_file_with_render(): locate -> read -> parse composition
    - assert_no_legacy_workspace_config(): Guard prohibiting deprecated drift.toml files
    - resolve_and_interpolate_workspace_config(): 6-tier variable stitching & section interpolation
===============================================================================
"""

import copy
import logging
import sys
from pathlib import Path
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ..core.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    add_envst_path,
)
from ..core.exceptions import ConfigError
from ..utils.func_utils import branch
from ..utils.env_utils import (
    EnvConfig,
    EnvResolve,
    interpolate_config_dict,
    parse_env_dict,
    resolve_env_configs,
)
from ..utils.toml_utils import merge_toml, parse_toml
from .secrets_config import (
    SecretsConfig,
    fetch_workspace_secrets,
)
from .workspace_config import WorkspaceConfig

logger = logging.getLogger(__name__)


def render_workspace_config_content(content: str, source_desc: str = "") -> str:
    """Renders drift_workspace.envst.toml template content using python_envsubst."""
    from ..render.render_core import python_envsubst
    rendered_content = python_envsubst(content, error_cls=ConfigError)
    logger.debug(f"Rendered workspace config from template '{source_desc}':\n{rendered_content}")
    return rendered_content


def render_workspace_config(render_input_path: Path) -> str:
    """Renders the drift_workspace.envst.toml template using python_envsubst,
    returning the rendered output.
    """
    content = render_input_path.read_text(encoding="utf-8")
    return render_workspace_config_content(content, source_desc=render_input_path.as_posix())


def check_is_envst_workspace_config(path: Path) -> bool:
    """Returns True if path names an .envst.toml workspace config template."""
    return path.name.endswith(".envst.toml")


def locate_workspace_config_file(rendered_config_path: Path) -> Optional[Path]:
    """Locates the on-disk source file for a workspace config path.

    Returns the static file if it exists, else its .envst.toml template counterpart if it
    exists, else None. This precedence is shared by loading and editing.
    """
    candidates = (rendered_config_path, add_envst_path(rendered_config_path))
    return next((p for p in candidates if p.is_file()), None)


def parse_workspace_config_content(content: str, source_path: Path) -> dict:
    """Parses raw workspace config content, rendering it first if source_path is an .envst template.

    Raises:
        ConfigError: If rendering fails or the content is not valid TOML.
    """
    text = branch(
        check_is_envst_workspace_config(source_path),
        lambda: render_workspace_config_content(content, source_desc=source_path.as_posix()),
        lambda: content,
    )
    try:
        return parse_toml(text)
    except Exception as e:
        raise ConfigError(f"Failed to parse TOML in '{source_path}': {e}") from e


def load_workspace_config_file_with_render(rendered_config_path: Path) -> Optional[dict]:
    """Loads and parses the TOML file at path.

    Checks the static file first, then falls back to rendering its .envst.toml counterpart.
    Returns None if neither exists.
    """
    located = locate_workspace_config_file(rendered_config_path)
    if located is None:
        return None
    logger.debug(f"Workspace config is being loaded from: '{located}'")
    return parse_workspace_config_content(located.read_text(encoding="utf-8"), located)


def assert_no_legacy_workspace_config(drift_root: Path) -> None:
    """Checks for deprecated legacy workspace config files and aborts with a prominent error if found."""
    config_dir = Path(drift_root) / CONFIG_DIR_NAME
    legacy_candidates = [
        config_dir / "drift.toml",
        config_dir / "drift.local.toml",
        config_dir / "drift.envst.toml",
        config_dir / "drift.local.envst.toml",
    ]
    legacy_found = [x for x in legacy_candidates if x.is_file()]
    if not legacy_found:
        return
    err_box = (
        "\n" + "=" * 80 + "\n"
        "❌ DEPRECATION ERROR: Legacy workspace configuration file detected!\n\n"
        f"Found legacy file: {', '.join(x.as_posix() for x in legacy_found)}\n\n"
        "The workspace configuration file has been renamed:\n"
        "  • 'drift.toml'             -> 'drift_workspace.toml'\n"
        "  • 'drift.local.toml'       -> 'drift_workspace.local.toml'\n"
        "  • 'drift.envst.toml'       -> 'drift_workspace.envst.toml'\n"
        "  • 'drift.local.envst.toml' -> 'drift_workspace.local.envst.toml'\n\n"
        "Backward compatibility for 'drift.toml' has been completely removed.\n"
        "Please rename your configuration file to 'drift_workspace.toml' (or appropriate suffix) to proceed.\n"
        "You can also run 'drift repair' to automatically migrate legacy configuration files.\n"
        + "=" * 80 + "\n"
    )
    print(err_box, file=sys.stderr)
    raise ConfigError(
        f"Legacy workspace configuration file [{', '.join(x.name for x in legacy_found)}] is no longer supported. "
        f"Please rename [{', '.join(x.name for x in legacy_found)}] to "
        f"[{', '.join(x.name.replace('drift', 'drift_workspace') for x in legacy_found)}] "
        f"or run 'drift repair' to automatically migrate it."
    )


def load_workspace_config_files_layered(rendered_config_path_list: Sequence[Path]) -> Dict[str, Any]:
    """Sequentially loads, renders (if templated), and deep-merges an arbitrary list of workspace configuration files.

    Accepts an arbitrary sequence of workspace config paths (e.g. base drift_workspace.toml,
    machine-local drift_workspace.local.toml, or custom override layers), rendering any .envst.toml
    templates as needed. Base missing with override existing is supported.

    Args:
        rendered_config_path_list: Ordered list of candidate workspace configuration file paths.

    Returns:
        Merged configuration dictionary across all loaded file layers.

    Raises:
        ConfigError: If none of the specified configuration files or their templates exist.
    """
    result: Dict[str, Any] = {}
    for idx, file in enumerate(rendered_config_path_list):
        f_dict = load_workspace_config_file_with_render(file)
        if not f_dict:
            continue
        logger.debug(f"Loaded workspace config {'base' if idx == 0 else 'override'} from '{file}'")
        result = merge_toml(result, f_dict)
    # Base config file missing with override files existence is accepted.
    # The whole result cannot be an empty dict.
    if not result:
        raise ConfigError(
            f"Workspace configuration file not found in [{', '.join(x.as_posix() for x in rendered_config_path_list)}] "
            "or their templates."
        )
    return result


def resolve_and_interpolate_workspace_config(
    data: Dict[str, Any],
    secrets_file_dict: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, Any], EnvResolve]:
    """Resolves environment variables, secrets, and interpolates references across a workspace config dictionary.

    Follows the 6-Tier Precedence Model:
    - Tier 1 (Override): Workspace [env.override]
    - Tier 2 (Facts): Protected drift_* system facts
    - Tier 3 (CLI & Ambient): Ambient Process Environment (os.environ) & CLI Overrides
    - Tier 4 (Secrets): Workspace [env.secrets] > [secrets] provider / config/secrets.env
    - Tier 5 (Default): Workspace [env.default]
    - Tier 6 (Fallback): Workspace [env.fallback]

    Args:
        data: Parsed TOML dictionary of the workspace configuration.
        secrets_file_dict: Optional secrets mapping loaded from [secrets] provider or config/secrets.env.

    Returns:
        A tuple of (interpolated_dict, resolved_env_resolve).
    """
    raw_env = data.get("env", {})
    env_config = parse_env_dict(raw_env, context_desc="workspace configuration")
    resolved_env = resolve_env_configs(
        env_config,
        lower_layer=branch(
            bool(secrets_file_dict),
            lambda: EnvConfig(secrets=dict(secrets_file_dict or {})),
            lambda: None,
        ),
    )

    interpolated_data = interpolate_config_dict(
        data,
        env=resolved_env.impact.full_env(),
        exclude_keys={"env"},
        error_cls=ConfigError
    )

    env_dict = resolved_env.current.to_env_dict()
    final_dict = {
        **{k: v for k, v in interpolated_data.items() if k != "env"},
        **({"env": env_dict} if env_dict else {}),
    }
    return final_dict, resolved_env


def load_workspace_config(
    drift_root: Path,
    check_legacy: bool = True,
    config_files_override: Optional[Sequence[Path]] = None,
    quiet: bool = False,
) -> WorkspaceConfig:
    """Loads, transforms, and validates the workspace configuration.

    Configuration Pipeline Execution Order:
    1. Multi-File Discovery & Merging: Loads base and override TOML files (or .envst.toml templates)
       via load_workspace_config_files_layered.
    2. Secrets Provider Resolution: Parses [secrets] into SecretsConfig and fetches workspace secrets
       via fetch_workspace_secrets (falling back to config/secrets.env when omitted).
    3. Dynamic Python Workspace Hook: Executes configure_workspace(context) from config/drift_workspace.py
       (or custom hook_file). The hook operates as a preprocessor on the raw dictionary with access to
       resolved context facts and environment.
    4. Variable Stitching & Topological Sort: Resolves inter-variable references in [env.default]
       and [env.secrets] using Kahn's topological sort algorithm with cycle detection.
    5. Cross-Section Interpolation: Interpolates ${VAR} references across all non-env sections.
    6. Schema Validation & Model Construction: Instantiates strongly-typed WorkspaceConfig.

    Args:
        drift_root: Path to the drift workspace repository root.
        check_legacy: Whether to detect and reject legacy drift.toml files.
        config_files_override: Optional custom sequence of configuration paths to load instead of defaults.
        quiet: If True, suppresses non-error logging such as missing enabled package warnings.

    Returns:
        Fully resolved and validated WorkspaceConfig instance.
    """
    root = Path(drift_root).resolve()
    if check_legacy:
        assert_no_legacy_workspace_config(root)

    load_configs_from = branch(
        bool(config_files_override),
        lambda: list(config_files_override or ()),
        lambda: [
            root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME,
            root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_LOCAL_FILE_NAME,
        ],
    )

    try:
        combined_dict = load_workspace_config_files_layered(load_configs_from)

        initial_secrets_raw = copy.deepcopy(combined_dict.get("secrets", {}))
        secrets_cfg = SecretsConfig.from_dict(initial_secrets_raw)
        secrets_file_dict = fetch_workspace_secrets(root, secrets_cfg)

        # Apply dynamic workspace hook (config/drift_workspace.py or custom hook_file)
        from ..hooks.workspace_config_hook import apply_workspace_hook
        combined_dict = apply_workspace_hook(root, combined_dict, secrets_file_dict=secrets_file_dict)

        if combined_dict.get("secrets", {}) != initial_secrets_raw:
            secrets_cfg = SecretsConfig.from_dict(combined_dict.get("secrets", {}))
            secrets_file_dict = fetch_workspace_secrets(root, secrets_cfg)

        # Pure in-memory topological resolution and section interpolation
        interpolated_dict, env_res = resolve_and_interpolate_workspace_config(
            combined_dict,
            secrets_file_dict=secrets_file_dict,
        )

        return WorkspaceConfig.from_dict(
            interpolated_dict,
            drift_root=root,
            env_resolve=env_res,
            quiet=quiet,
        )
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid workspace configuration in '{load_configs_from[0]}': {e}") from e
