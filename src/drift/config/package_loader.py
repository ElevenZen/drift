"""Package configuration loading, template rendering, and multi-layer pipeline.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: High-Level Stage Discovery & Pipeline Loaders
    - load_package_config_from_source_dir(): Full 6-stage loading pipeline for src/<pkg>/
    - load_package_config_from_render_dir(): Render sandbox loader for render/<pkg>/
    - load_package_config_for_install(): Install state loader for install/<pkg>/
    - load_package_config_rendered(): Direct rendered TOML parser

Layer 1: Low-Level TOML & Variable Interpolation Helpers
    - resolve_and_interpolate_package_config(): Stitched env resolution & variable interpolation
===============================================================================
"""

import logging
import tempfile
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

from ..core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.env_utils import (
    EnvConfig,
    EnvResolve,
    interpolate_config_dict,
    parse_env_dict,
    resolve_env_configs,
)
from ..utils.toml_utils import merge_toml, parse_toml
from .package_config import PackageConfig

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

logger = logging.getLogger(__name__)


def resolve_and_interpolate_package_config(
    data: dict,
    package_name: str,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> Tuple[Dict[str, Any], EnvResolve]:
    """Resolves environment variables and interpolates references across a package config dictionary.

    Args:
        data: Parsed TOML dictionary of the package configuration.
        package_name: Name of the package.
        workspace_config: Optional workspace configuration for deriving directory facts and workspace envs.

    Returns:
        A tuple of (stitched_config_dict, resolved_env_resolve).
    """
    pkg_facts = (workspace_config.get_drift_package_facts(package_name)
                 if workspace_config is not None
                 else {"drift_package_name": package_name})

    env_config = parse_env_dict(data.get("env", {}), context_desc=f"package '{package_name}'")
    resolved_env = resolve_env_configs(
        env_config,
        lower_layer=workspace_config.env_resolve.effective if workspace_config is not None else None,
        package_facts=pkg_facts,
    )

    interpolated_data = interpolate_config_dict(
        data,
        env=resolved_env.impact.full_env(),
        exclude_keys={"env"},
        error_cls=ConfigError
    )

    env_dict = resolved_env.current.to_env_dict()
    stitched_data = {
        **{k: v for k, v in interpolated_data.items() if k != "env"},
        **({"env": env_dict} if env_dict else {}),
    }
    return stitched_data, resolved_env


def load_package_config_rendered(
    package_toml_path: Path,
    package_name: str,
    package_dir: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml.

    Args:
        package_toml_path: Absolute path to the rendered drift_package.toml file.
        package_name: Required canonical name of the package.
        package_dir: Required package root directory (e.g. render/<pkg> or install/<pkg>).
        workspace_config: Optional active WorkspaceConfig providing workspace layout and environment for effective resolution.
    """
    if not package_toml_path.exists():
        raise FileNotFoundError(f"Package configuration file not found: {package_toml_path}")
    content = package_toml_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    try:
        config = PackageConfig.from_dict(
            data,
            package_name=package_name,
            source_files=[package_toml_path],
            base_dir=package_dir,
            workspace_config=workspace_config,
        )
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid package configuration for '{package_name}' in '{package_toml_path}': {e}") from e
    return config


def load_package_config_from_source_dir(
    package_dir: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
    dry_run: bool = False,
    silent: bool = False,
) -> PackageConfig:
    """Loads, transforms, and validates the package configuration from its source directory."""
    if workspace_config is not None:
        return _load_package_config_with_workspace(package_dir, workspace_config, dry_run=dry_run, silent=silent)
    return _load_package_config_static_fallback(package_dir)


def _load_package_config_with_workspace(
    package_dir: Path,
    workspace_config: "WorkspaceConfig",
    dry_run: bool = False,
    silent: bool = False,
) -> PackageConfig:
    """Full Phase 1 Merkle DAG compilation pipeline for package configuration."""
    if dry_run:
        with tempfile.TemporaryDirectory(prefix=f"{package_dir.name}_cfg_dry_") as tmp_dir:
            temp_pkg_render_dir = Path(tmp_dir) / package_dir.name
            return _execute_load_package_config_dag(
                package_dir=package_dir,
                workspace_config=workspace_config,
                pkg_render_dir=temp_pkg_render_dir,
                silent=silent,
            )
    return _execute_load_package_config_dag(
        package_dir=package_dir,
        workspace_config=workspace_config,
        pkg_render_dir=workspace_config.render_path / package_dir.name,
        silent=silent,
    )


def _execute_load_package_config_dag(
    package_dir: Path,
    workspace_config: "WorkspaceConfig",
    pkg_render_dir: Path,
    silent: bool = False,
) -> PackageConfig:
    """Executes the Phase 1 Merkle DAG compilation pipeline into specified pkg_render_dir."""
    pkg_name = package_dir.name
    candidate_rendered_names = [PACKAGE_CONFIG_FILE_NAME, PACKAGE_CONFIG_LOCAL_FILE_NAME]
    candidate_source_files: List[Path] = []

    engines = workspace_config.render_engine_configs
    for cand_name in candidate_rendered_names:
        file_match = engines.find_source_file_for_rendered_names(package_dir, [cand_name])
        if file_match:
            candidate_source_files.append(file_match.path)

    if not candidate_source_files:
        raise FileNotFoundError(
            f"Package configuration file not found in [{', '.join(candidate_rendered_names)}] "
            "or their templates."
        )

    from ..render.render_dag import PackageConfigNode, UnknownPathNode, JsonNode, Node
    from ..render.render_expansion import ExpansionContext, expand_node_dependencies
    from ..render.render_digester import DigestionContext, digest_render_dag
    from ..render.render_lock import RenderLockfile, RenderBucket

    src_prefix = workspace_config.source_path / pkg_name
    dst_prefix = (
        pkg_render_dir
        / DRIFT_INTERNAL_DIR_NAME
        / DRIFT_INTERNAL_RENDER_DIR_NAME
        / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME
    )
    translation_map = {
        src_prefix / PACKAGE_CONFIG_FILE_NAME: dst_prefix / PACKAGE_CONFIG_FILE_NAME,
        src_prefix / PACKAGE_CONFIG_LOCAL_FILE_NAME: dst_prefix / PACKAGE_CONFIG_LOCAL_FILE_NAME,
    }

    pkg_facts = workspace_config.get_drift_package_facts(pkg_name)
    env_res = resolve_env_configs(
        workspace_config.env_resolve.effective,
        None,
        package_facts=pkg_facts,
    )
    env_node = JsonNode(env_res.impact.restricted_env())
    exp_ctx = ExpansionContext(
        package_name=pkg_name,
        enable_render=True,
        env_node=env_node,
        render_engines=workspace_config.render_engine_configs,
        cache=workspace_config.render_cache,
        path_translation=translation_map,
        drift_root=workspace_config.drift_root,
    )

    cand_nodes: List[Node] = []
    for cand in candidate_source_files:
        cand_nodes.append(UnknownPathNode(cand))

    dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
    cfg_node = PackageConfigNode(
        dst_path=dst_path,
        sources=cand_nodes,
        env_node=env_node,
        package_dir=package_dir,
        workspace_config=workspace_config,
    )

    expand_node_dependencies(cfg_node, exp_ctx)

    lockfile = RenderLockfile.load_from_dir(pkg_render_dir)
    ctx = DigestionContext(
        drift_root=workspace_config.drift_root,
        package_name=pkg_name,
        package_render_dir=pkg_render_dir,
        lockfile=lockfile,
        bucket=RenderBucket.CONFIG,
        cache=workspace_config.render_cache,
        silent=silent,
    )

    with env_res.impact.scope():
        digest_render_dag(cfg_node, ctx)

    assert cfg_node.package_config is not None, "PackageConfigNode failed to instantiate PackageConfig"
    return cfg_node.package_config


def _load_package_config_static_fallback(package_dir: Path) -> PackageConfig:
    """Static file parsing fallback without workspace context (used in isolated unit tests)."""
    pkg_name = package_dir.name
    from ..hooks.package_hook import apply_package_hook

    candidate_rendered_names = [PACKAGE_CONFIG_FILE_NAME, PACKAGE_CONFIG_LOCAL_FILE_NAME]
    candidate_source_files: List[Path] = [
        package_dir / cand_name for cand_name in candidate_rendered_names if (package_dir / cand_name).is_file()
    ]

    if not candidate_source_files:
        raise FileNotFoundError(
            f"Package configuration file not found in [{', '.join(candidate_rendered_names)}] "
            "or their templates."
        )

    combined_dict: Dict[str, Any] = {}
    source_files = list(candidate_source_files)
    for cand in candidate_source_files:
        combined_dict = merge_toml(combined_dict, parse_toml(cand.read_text(encoding="utf-8")))

    combined_dict, hook_path = apply_package_hook(package_dir, combined_dict, None)
    if hook_path:
        source_files.append(hook_path)

    stitched_dict, _ = resolve_and_interpolate_package_config(combined_dict, package_name=pkg_name, workspace_config=None)
    return PackageConfig.from_dict(
        stitched_dict,
        package_name=pkg_name,
        source_files=source_files,
        base_dir=package_dir,
        workspace_config=None,
    )


def load_package_config_from_render_dir(
    package_dir: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> PackageConfig:
    """Loads package configuration strictly from the render/ sandbox package directory.
    The name of package_dir is treated as the package name.

    Args:
        package_dir: Path to the package directory inside render/ (e.g. render/<pkg>).
        workspace_config: Optional active WorkspaceConfig for deriving stage paths and effective environment.
    """
    pkg_name = package_dir.name
    config_file = package_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
    if not config_file.exists():
        legacy_file = package_dir / PACKAGE_CONFIG_FILE_NAME
        if legacy_file.exists():
            logger.warning(
                f"⚠️ [DEPRECATION] Package '{pkg_name}' in render/ contains legacy root '{PACKAGE_CONFIG_FILE_NAME}'. "
                f"Please run 'drift repair' to migrate metadata into '{DRIFT_INTERNAL_DIR_NAME}/'."
            )
            config_file = legacy_file
        else:
            raise RuntimeError(f"Failed to find {PACKAGE_CONFIG_FILE_NAME} in .drift/ for '{pkg_name}' in render sandbox")
    try:
        return load_package_config_rendered(
            package_toml_path=config_file,
            package_name=pkg_name,
            package_dir=package_dir,
            workspace_config=workspace_config,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to load package configuration for '{pkg_name}' from render sandbox: {e}")


def load_package_config_for_install(
    package_dir: Path,
    workspace_config: Optional["WorkspaceConfig"] = None,
) -> PackageConfig:
    """Loads package configuration strictly from the install/ base package directory.
    The name of package_dir is treated as the package name.

    Args:
        package_dir: Path to the package directory inside install/ (e.g. install/<pkg>).
        workspace_config: Optional active WorkspaceConfig for deriving stage paths and effective environment.
    """
    pkg_name = package_dir.name
    install_config_file = package_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
    if not install_config_file.exists():
        legacy_file = package_dir / PACKAGE_CONFIG_FILE_NAME
        if legacy_file.exists():
            logger.warning(
                f"⚠️ [DEPRECATION] Package '{pkg_name}' in install/ contains legacy root '{PACKAGE_CONFIG_FILE_NAME}'. "
                f"Please run 'drift repair' to migrate metadata into '{DRIFT_INTERNAL_DIR_NAME}/'."
            )
            install_config_file = legacy_file
        else:
            raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in .drift/ of install base for '{pkg_name}'.")
    try:
        return load_package_config_rendered(
            package_toml_path=install_config_file,
            package_name=pkg_name,
            package_dir=package_dir,
            workspace_config=workspace_config,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to load package configuration for '{pkg_name}' from install base: {e}")
