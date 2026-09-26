"""Workspace configuration loading, legacy migration assertion, and layered pipeline.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Workspace Discovery & High-Level Pipeline Loaders
    - load_workspace_config(): Full multi-stage loading, hook preprocessing, and resolution
    - load_workspace_config_files_layered(): Sequential layered TOML loading & deep-merging

Layer 1: Low-Level Template Rendering & Migration Guards
    - render_workspace_config(): Python envsubst template renderer
    - load_workspace_config_file_with_render(): Static TOML or template file loader
    - assert_no_legacy_workspace_config(): Guard prohibiting deprecated drift.toml files
    - resolve_and_interpolate_workspace_config(): 6-tier variable stitching & section interpolation
===============================================================================
"""

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
from ..utils.env_utils import (
    EnvConfig,
    EnvResolve,
    interpolate_config_dict,
    parse_env_dict,
    parse_secrets_env,
    resolve_env_configs,
)
from ..utils.toml_utils import merge_toml, parse_toml
from .workspace_config import WorkspaceConfig

logger = logging.getLogger(__name__)


def render_workspace_config(render_input_path: Path) -> str:
    """Renders the drift_workspace.envst.toml template using python_envsubst,
    returning the rendered output.
    """
    from ..render.render_core import python_envsubst
    content = render_input_path.read_text(encoding="utf-8")
    rendered_content = python_envsubst(content, error_cls=ConfigError)
    logger.debug(f"Rendered workspace config from template '{render_input_path}':\n{rendered_content}")
    return rendered_content


def load_workspace_config_file_with_render(rendered_config_path: Path) -> Optional[dict]:
    """Loads and parses the TOML file at path.

    Checks the static file first, then falls back to rendering its .envst.toml counterpart.
    Propagates FileNotFoundError if neither exists.
    """
    envst_path = add_envst_path(rendered_config_path)
    if rendered_config_path.exists():
        logger.debug(f"Workspace config is being loaded from: '{rendered_config_path}'")
        content = rendered_config_path.read_text(encoding="utf-8")
    elif envst_path.exists():
        logger.debug(f"Workspace config is being rendered from template: '{envst_path}'")
        content = render_workspace_config(envst_path)
    else:
        return None
    return parse_toml(content)


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
        f"Found legacy file: {', '.join(str(x) for x in legacy_found)}\n\n"
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
            f"Workspace configuration file not found in [{', '.join(str(x) for x in rendered_config_path_list)}] "
            "or their templates."
        )
    return result


def resolve_and_interpolate_workspace_config(
    data: Dict[str, Any],
    secrets_file: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, Any], EnvResolve]:
    """Resolves environment variables, secrets, and interpolates references across a workspace config dictionary.

    Follows the 6-Tier Precedence Model:
    - Tier 1 (CLI): Ambient Process Environment & CLI Variables (INITIAL_ENV / os.environ)
    - Tier 2 (Override): Workspace [env.override]
    - Tier 3 (Facts): Protected drift_* system facts
    - Tier 4 (Secrets): Workspace [env.secrets] > config/secrets.env
    - Tier 5 (Default): Workspace [env.default]
    - Tier 6 (Fallback): Workspace [env.fallback]

    Args:
        data: Parsed TOML dictionary of the workspace configuration.
        secrets_file: Optional secrets mapping loaded from config/secrets.env.

    Returns:
        A tuple of (interpolated_dict, resolved_env_resolve).
    """
    raw_env = data.get("env", {})
    env_config = parse_env_dict(raw_env, context_desc="workspace configuration")
    resolved_env = resolve_env_configs(
        env_config,
        lower_layer=EnvConfig(secrets=dict(secrets_file)) if secrets_file else None,
    )

    interpolated_data = interpolate_config_dict(
        data,
        env=resolved_env.effective_dict,
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
) -> WorkspaceConfig:
    """Loads, transforms, and validates the workspace configuration.

    Configuration Pipeline Execution Order:
    1. Multi-File Discovery & Merging: Loads base and override TOML files (or .envst.toml templates)
       via load_workspace_config_files_layered.
    2. Dynamic Python Workspace Hook: Executes configure_workspace(context) from config/drift_workspace.py
       (or custom hook_file). The hook operates as a preprocessor on the raw dictionary with access to
       resolved context facts and environment.
    3. Variable Stitching & Topological Sort: Resolves inter-variable references in [env.default]
       and [env.secrets] using Kahn's topological sort algorithm with cycle detection.
    4. Cross-Section Interpolation: Interpolates ${VAR} references across all non-env sections.
    5. Schema Validation & Model Construction: Instantiates strongly-typed WorkspaceConfig.

    Args:
        drift_root: Path to the drift workspace repository root.
        check_legacy: Whether to detect and reject legacy drift.toml files.
        config_files_override: Optional custom sequence of configuration paths to load instead of defaults.

    Returns:
        Fully resolved and validated WorkspaceConfig instance.
    """
    root = Path(drift_root).resolve()
    if check_legacy:
        assert_no_legacy_workspace_config(root)

    load_configs_from = list(config_files_override) if config_files_override else [
        root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME,
        root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    ]

    secrets_file = parse_secrets_env(root)

    combined_dict = load_workspace_config_files_layered(load_configs_from)

    # Apply dynamic workspace hook (config/drift_workspace.py or custom hook_file)
    from ..hooks.workspace_hook import apply_workspace_hook
    combined_dict = apply_workspace_hook(root, combined_dict)

    # Pure in-memory topological resolution and section interpolation
    interpolated_dict, env_res = resolve_and_interpolate_workspace_config(
        combined_dict,
        secrets_file=secrets_file,
    )

    try:
        return WorkspaceConfig.from_dict(interpolated_dict, drift_root=root, env_resolve=env_res)
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid workspace configuration in '{load_configs_from[0]}': {e}") from e
