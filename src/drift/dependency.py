import os
import logging
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
    temp_config = RenderEngineConfig(name="temp", input_file="", suffix=suffix, render_command="")
    return temp_config.strip_suffix(filename)


def resolve_dependencies(engines: List[RenderEngineConfig]) -> Dict[str, Optional[str]]:
    """Resolves the input file dependency relationships among engines as a map of:

    engine_name -> dependency_engine_name (or None)
    """
    dependency_map: Dict[str, Optional[str]] = {}
    for engine in engines:
        dep_engine = find_engine_for_file(engine.input_file, engines)
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


def check_multi_level_dependencies(dependency_map: Mapping[str, Optional[str]]) -> None:
    for name, dep_name in dependency_map.items():
        if dep_name is not None:
            if dependency_map.get(dep_name) is not None:
                raise ValueError(
                    f"Multi-level dependency chain detected: engine '{name}' depends on '{dep_name}', "
                    f"which itself depends on '{dependency_map.get(dep_name)}'. "
                    f"Only one level of rendering is allowed."
                )


def resolve_static_input_file(input_file: str, drift_root: str, engine_name: str) -> str:
    """Resolves and validates a static input file path (handling both absolute and config-relative paths)."""
    if os.path.isabs(input_file):
        path = input_file
    else:
        path = os.path.join(drift_root, CONFIG_DIR_NAME, input_file)

    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Input file for render engine '{engine_name}' not found: {path}"
        )
    return path


def render_input_templates(
    engines: List[RenderEngineConfig],
    drift_root: str,
    workspace_config: Optional[WorkspaceConfig] = None
) -> None:
    """Resolves engine input dependencies, checks for cycles and multi-level dependency chains,

    renders input templates, prints progress, and updates each RenderEngineConfig.input_file path.

    Args:
        engines: The list of RenderEngineConfig instances.
        drift_root: The root path of the drift workspace.
        workspace_config: Optional WorkspaceConfig instance to read the render directory name from.

    Raises:
        ValueError: If a cyclic dependency or multi-level dependency is detected.
        FileNotFoundError: If any required template or static file is missing.
        RuntimeError: If subprocess template rendering fails.
    """
    # 1. Resolve dependencies
    dependency_map = resolve_dependencies(engines)

    # 2. Check for cycles
    check_cyclic_dependencies(dependency_map)

    # 3. Enforce only one level of dependency
    check_multi_level_dependencies(dependency_map)

    # 4. Render templates using the dependency map directly
    engines_by_name = {e.name: e for e in engines}
    render_dir = workspace_config.render_directory if workspace_config else "render"
    memo: Dict[str, str] = {}

    def get_or_render_input_file(engine: RenderEngineConfig) -> str:
        if engine.name in memo:
            return memo[engine.name]

        dep_name = dependency_map[engine.name]
        if dep_name:
            dep_engine = engines_by_name[dep_name]
            # Since only one level of dependency is allowed, dep_engine's input file is resolved as static without recursion
            dep_input_file = resolve_static_input_file(dep_engine.input_file, drift_root, dep_engine.name)

            # Formulate template file path (supporting both absolute and config-relative paths)
            if os.path.isabs(engine.input_file):
                template_file_path = engine.input_file
            else:
                template_file_path = os.path.join(drift_root, CONFIG_DIR_NAME, engine.input_file)

            if not os.path.exists(template_file_path):
                raise FileNotFoundError(
                    f"Input template file for render engine '{engine.name}' not found: {template_file_path}"
                )

            output_filename = dep_engine.strip_suffix(os.path.basename(engine.input_file))
            # The 'render' string is read dynamically from the workspace_config if provided
            output_file_path = os.path.join(drift_root, render_dir, CONFIG_DIR_NAME, output_filename)

            # Use logger.info instead of print
            logger.info(f"Rendering input for {engine.name} using {dep_name}: {template_file_path} >> {output_file_path}")

            render_template_to_file(
                engine_config=dep_engine,
                drift_root=drift_root,
                template_file_path=template_file_path,
                output_file_path=output_file_path,
                input_file_path=dep_input_file
            )

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
