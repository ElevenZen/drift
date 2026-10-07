"""Package configuration loading, template rendering, and multi-layer pipeline.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: High-Level Stage Discovery & Pipeline Loaders
    - load_package_config_from_source_dir(): Full 6-stage loading pipeline for src/<pkg>/
    - load_package_config_from_render_dir(): Render sandbox loader for render/<pkg>/
    - load_package_config_for_install(): Install state loader for install/<pkg>/
    - load_package_config_rendered(): Direct rendered TOML parser

Layer 1: Low-Level TOML & Template Rendering Helpers
    - PackageConfigFileInfo (Dataclass): Matched static file or template info
    - get_package_config_file_info(): Suffix & engine matcher for config candidates
    - render_or_load_toml(): Parses static TOML or renders template into temporary TOML
    - render_load_package_config_dict(): Multi-layer loader and deep merger
    - resolve_and_interpolate_package_config(): Stitched env resolution & variable interpolation
===============================================================================
"""

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
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
from ..core.exceptions import ConfigError, mark_logged
from ..utils.path_utils import is_relative_to
from ..utils.env_utils import (
    EnvConfig,
    EnvResolve,
    env_resolve_scope,
    interpolate_config_dict,
    parse_env_dict,
    resolve_env_configs,
)
from ..utils.toml_utils import dump_toml, merge_toml, parse_toml
from .render_engine_config import RenderEngineConfig, RenderEngineRegistry
from .package_config import PackageConfig

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

logger = logging.getLogger(__name__)


@dataclass
class PackageConfigFileInfo:
    """Represents file info for a found package config file (or template)."""
    type: str  # 'static' or 'template'
    path: Path  # path to the file/template
    engine: Optional[RenderEngineConfig] = None  # RenderEngineConfig instance (if 'template', otherwise None)


def get_package_config_file_info(
    config_files: Iterable[Path],
    render_engines: RenderEngineRegistry,
) -> List[PackageConfigFileInfo]:
    """Finds the package config file (or template) for an arbitrary list of rendered candidate paths.

    Inspects each candidate path against the render engine registry, determining whether a static
    file exists or if a matching template counterpart (e.g. .envst.toml) is present.

    Args:
        config_files: Ordered list of candidate package configuration file paths.
        render_engines: Registry of available template render engines.

    Returns:
        List of PackageConfigFileInfo objects for all matched configuration files/templates.
    """
    result = []
    for file in config_files:
        file_match = render_engines.find_source_file_for_rendered_names(file.parent, [file.name])
        if not file_match:
            continue
        result.append(PackageConfigFileInfo(
            type="static" if file_match.engine is None else "template",
            path=file_match.path,
            engine=file_match.engine,
        ))
    return result


def render_or_load_toml(
    info: PackageConfigFileInfo,
    workspace_config: "WorkspaceConfig",
    package_name: str
) -> dict:
    """Renders the package config file info to a temporary file (if it is a template)
    and returns its parsed TOML dictionary.
    """
    if info.type == "static":
        content = info.path.read_text(encoding="utf-8")
        return parse_toml(content)

    # It's a template, we need to render it!
    engine = info.engine
    if engine is None:
        raise ValueError(f"Template configuration file found, but render engine is not specified: {info.path}")

    env_res = resolve_env_configs(
        workspace_config.env_resolve.effective,
        None,
        workspace_config.get_drift_package_facts(package_name)
    )

    with tempfile.TemporaryDirectory(prefix=f"{package_name}_pkg_") as tmpdir:
        temp_path_obj = Path(tmpdir) / "drift_package.toml"
        with env_resolve_scope(env_res):
            from ..render.render_core import render_template_to_file
            render_template_to_file(
                engine_config=engine,
                drift_root=workspace_config.drift_root if workspace_config is not None else info.path.parent,
                template_file_path=info.path,
                output_file_path=temp_path_obj
            )
            content = temp_path_obj.read_text(encoding="utf-8")
            return parse_toml(content)


def render_load_package_config_dict(
    pkg_name: str,
    config_files: Sequence[Path],
    workspace_config: Optional["WorkspaceConfig"] = None
) -> Tuple[dict, List[Path]]:
    """Sequentially loads, renders (if templated), and deep-merges an arbitrary sequence of package config files.

    Supports arbitrary multi-layer package configs without a hardcoded base/local limit.
    If workspace_config is None, falls back to direct static file parsing without template rendering.

    Args:
        pkg_name: Name of the package.
        config_files: Ordered sequence of configuration candidate paths.
        workspace_config: Optional WorkspaceConfig providing render engine registry.

    Returns:
        Tuple of (merged_config_dict, list_of_source_paths).

    Raises:
        FileNotFoundError: If none of the specified configuration files or templates exist.
    """
    combined_dict = {}
    source_list = []
    if workspace_config is None:
        # mainly used in tests, to load a config without rendering or writing out into the render/ directory.
        logger.warning("WorkspaceConfig is not provided. Falling back to static loading without rendering.")
        for file in config_files:
            if not file.is_file():
                continue
            source_list.append(file)
            f_dict = parse_toml(file.read_text(encoding="utf-8"))
            combined_dict = merge_toml(combined_dict, f_dict)
    else:
        # With workspace_config provided, we can render templates if needed.
        info_list = get_package_config_file_info(
            config_files, workspace_config.render_engine_configs
        )
        for info in info_list:
            source_list.append(info.path)
            f_dict = render_or_load_toml(info, workspace_config, pkg_name)
            combined_dict = merge_toml(combined_dict, f_dict)

    if not combined_dict or not source_list:
        raise FileNotFoundError(
            f"Package configuration file not found in [{', '.join(str(x) for x in config_files)}] "
            "or their templates."
        )
    return combined_dict, source_list


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
        extra_facts=pkg_facts,
    )

    interpolated_data = interpolate_config_dict(
        data,
        env=resolved_env.effective_dict,
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
    from ..utils.env_utils import env_resolve_scope

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
        extra_facts=pkg_facts,
    )
    env_node = JsonNode(env_res.effective_dict)
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

    with env_resolve_scope(env_res):
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
