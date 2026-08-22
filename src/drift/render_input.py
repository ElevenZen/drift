"""Dependency and cyclic rendering engine checks using pathlib."""

import logging
from pathlib import Path
from typing import Mapping, Dict, List, Optional
from .workspace_config import RenderEngineConfig, WorkspaceConfig
from .render_core import render_template_to_file
from .constants import CONFIG_DIR_NAME

logger = logging.getLogger(__name__)


def find_engine_for_file(filename: str, engines: List[RenderEngineConfig]) -> Optional[RenderEngineConfig]:
    """Finds which engine (if any) should render the given file based on suffix patterns."""
    for engine in engines:
        suffix = engine.suffix
        if not suffix:
            continue
        if filename.endswith(f".{suffix}"):
            return engine
        if f".{suffix}." in filename:
            return engine
    return None


def strip_engine_suffix(filename: str, suffix: str) -> str:
    """Strips the engine suffix segment from the filename, replacing only the last occurrence (legacy wrapper)."""
    temp_config = RenderEngineConfig(name="temp", input_file=Path(""), suffix=suffix, render_command="")
    return temp_config.strip_suffix(filename)


def resolve_dependencies(engines: List[RenderEngineConfig]) -> Dict[str, Optional[str]]:
    """Resolves the input file dependency relationships among engines as a map of:

    engine_name -> dependency_engine_name (or None)
    """
    dependency_map: Dict[str, Optional[str]] = {}
    for engine in engines:
        dep_engine = find_engine_for_file(str(engine.input_file), engines)
        # If dep_engine is the same as engine, it means the input file is static and not rendered by any other engine
        if dep_engine and dep_engine.name != engine.name:
            dependency_map[engine.name] = dep_engine.name
        else:
            dependency_map[engine.name] = None
    return dependency_map


def check_cyclic_dependencies(dependency_map: Mapping[str, Optional[str]]) -> None:
    """Checks if the dependency map contains any cyclic dependencies.

    Raises:
        ValueError: If a cyclic dependency is detected.
    """
    visited = {}  # name -> state: 0=unvisited, 1=visiting, 2=visited

    def dfs(node: str) -> None:
        visited[node] = 1  # visiting
        dep = dependency_map.get(node)
        if dep:
            if visited.get(dep, 0) == 1:
                raise ValueError(
                    f"Cyclic dependency detected: render engine inputs form a cycle "
                    f"containing '{node}' and '{dep}'."
                )
            elif visited.get(dep, 0) == 0:
                dfs(dep)
        visited[node] = 2  # visited

    for node in dependency_map:
        if visited.get(node, 0) == 0:
            dfs(node)


def resolve_static_input_file(
    input_file: Path,
    drift_root: Path,
    engine_name: str
) -> Path:
    """Resolves and validates a static input file path (handling both absolute and config-relative paths)."""
    if not input_file or str(input_file) in ("", "."):
        logger.warning(f"Input file for render engine '{engine_name}' is not specified or empty.")
        return Path("")
    if input_file.is_absolute():
        path = input_file
    else:
        path = drift_root / CONFIG_DIR_NAME / input_file

    if not path.exists():
        logger.warning(
            f"Input file for render engine '{engine_name}' not found: {path}"
        )
        return Path("")
    return path


def render_input_templates(
    engines: List[RenderEngineConfig],
    drift_root: Path,
    workspace_config: Optional[WorkspaceConfig] = None
) -> None:
    """Resolves engine input dependencies, checks for cycles,

    renders input templates, prints progress, and updates each RenderEngineConfig.input_file path.

    Args:
        engines: The list of RenderEngineConfig instances.
        drift_root: The root path of the drift workspace.
        workspace_config: Optional WorkspaceConfig instance to read the render directory name from.

    Raises:
        ValueError: If a cyclic dependency is detected.
        FileNotFoundError: If any required template or static file is missing.
        RuntimeError: If subprocess template rendering fails.
    """
    # 1. Resolve dependencies
    dependency_map = resolve_dependencies(engines)

    # 2. Check for cycles
    check_cyclic_dependencies(dependency_map)

    # 3. Render templates using the dependency map directly
    engines_by_name = {e.name: e for e in engines}
    render_dir = workspace_config.render_directory if workspace_config else "render"
    memo: Dict[str, Path] = {}

    def get_or_render_input_file(engine: RenderEngineConfig) -> Path:
        if engine.name in memo:
            return memo[engine.name]

        dep_name = dependency_map[engine.name]
        if dep_name:
            dep_engine = engines_by_name[dep_name]
            dep_input_file = get_or_render_input_file(dep_engine)
            if dep_input_file == Path(""):
                logger.warning(f"Dependency engine '{dep_name}' has an invalid input file, rendering for '{engine.name}' may fail.")
                memo[engine.name] = Path("")
                return Path("")

            # Formulate template file path (supporting both absolute and config-relative paths)
            template_file_path = drift_root / CONFIG_DIR_NAME / engine.input_file

            if not template_file_path.exists():
                logger.warning(
                    f"Input template file for render engine '{engine.name}' not found: {template_file_path}"
                )
                memo[engine.name] = Path("")
                return Path("")

            output_filename = dep_engine.strip_suffix(template_file_path.name)
            # The 'render' string is read dynamically from the workspace_config if provided
            output_file_path = drift_root / render_dir / CONFIG_DIR_NAME / output_filename

            # Use logger.info with a high-signal format
            logger.info(f"🎨 Rendering engine input: {engine.name} (via {dep_name})")
            logger.debug(f"   {template_file_path.relative_to(drift_root)} -> {output_file_path.relative_to(drift_root)}")

            try:
                render_template_to_file(
                    engine_config=dep_engine,
                    drift_root=drift_root,
                    template_file_path=template_file_path,
                    output_file_path=output_file_path,
                    input_file_path=dep_input_file
                )
            except Exception as e:
                logger.warning(f"Failed to render input template for engine '{engine.name}': {e}")
                memo[engine.name] = Path("")
                return Path("")

            memo[engine.name] = output_file_path
            return output_file_path
        else:
            # Static input file
            path = resolve_static_input_file(engine.input_file, drift_root, engine.name)
            memo[engine.name] = path
            return path

    # Render inputs for all engines and update their paths
    for engine in engines:
        rendered_path = get_or_render_input_file(engine)
        engine.input_file = rendered_path
