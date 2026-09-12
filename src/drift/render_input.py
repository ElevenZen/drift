"""Dependency resolution and template compilation for render engine input files.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Engine Input Rendering & Path Mutation (Public Entry Point)
    render_input_templates(engines, drift_root, output_dir)
        1. Dependency Graph & Cycle Detection:
            resolve_dependencies [Layer 2]
            check_cyclic_dependencies [Layer 1]
        2. Recursive Topological Input Rendering:
            get_or_render_input_file
                resolve_static_input_file [Layer 1]
                render_template_to_file (from render_core)
        3. In-Place Update:
            Mutates each engine.input_file to point to the resolved absolute path

Layer 2: Dependency Graph Construction
    resolve_dependencies(engines)
        get_engine_dependency(engine, engines) [Layer 1]

Layer 1: Validation & Inspection Helpers
    get_engine_dependency
    check_cyclic_dependencies
    resolve_static_input_file
===============================================================================
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Dict, Optional

from .constants import CONFIG_DIR_NAME
from .render_engine_config import RenderEngineConfig, RenderEngineRegistry
from .render_core import render_template_to_file

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Validation & Inspection Helpers
# =====================================================================

def get_engine_dependency(
    engine: RenderEngineConfig,
    engines: RenderEngineRegistry
) -> Optional[str]:
    """Determines if an engine's input file is a template requiring a dependency engine."""
    if engine.is_internal or not engine.input_file or str(engine.input_file) in ("", "."):
        return None
    dep_engine = engines.find_engine_for_file(str(engine.input_file))
    return dep_engine.name if (dep_engine and dep_engine.name != engine.name) else None


def check_cyclic_dependencies(dependency_map: Mapping[str, Optional[str]]) -> None:
    """Checks if the dependency map contains any cyclic dependencies.

    Raises:
        ValueError: If a cyclic dependency is detected.
    """
    visited: Dict[str, int] = {}  # name -> state: 0=unvisited, 1=visiting, 2=visited

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
    engine_name: str
) -> Path:
    """Validates an absolute static input file path."""
    if not input_file or str(input_file) in ("", "."):
        logger.warning(
            f"Input file for render engine '{engine_name}' is not specified or empty. Engine '{engine_name}' is disabled."
        )
        return Path("")

    if not input_file.exists():
        logger.warning(
            f"Input file for render engine '{engine_name}' not found: {input_file}. Engine '{engine_name}' is disabled."
        )
        return Path("")
    return input_file


# =====================================================================
# Layer 2: Dependency Graph Construction
# =====================================================================

def resolve_dependencies(
    engines: RenderEngineRegistry
) -> Dict[str, Optional[str]]:
    """Resolves the input file dependency relationships among engines as a map of:

    engine_name -> dependency_engine_name (or None)
    """
    return {
        engine.name: get_engine_dependency(engine, engines)
        for engine in engines.values()
    }


# =====================================================================
# Layer 3: Engine Input Rendering & Path Mutation (Public Entry Point)
# =====================================================================

def render_input_templates(
    engines: RenderEngineRegistry,
    drift_root: Path,
    output_dir: Path,
) -> None:
    """Resolves engine input dependencies, checks for cycles,
    renders input templates into output_dir, prints progress,
    and updates each RenderEngineConfig.input_file path.

    Args:
        engines: The RenderEngineRegistry instance.
        drift_root: The root path of the drift workspace.
        output_dir: Target destination directory for rendered input files (e.g. render/.drift or render/<pkg>/.drift).

    Raises:
        ValueError: If a cyclic dependency is detected.
    """
    dependency_map = resolve_dependencies(engines)
    check_cyclic_dependencies(dependency_map)

    target_output_dir = Path(output_dir)
    memo: Dict[str, Path] = {}

    def get_or_render_input_file(engine: RenderEngineConfig) -> Path:
        if engine.name in memo:
            return memo[engine.name]

        if engine.is_internal:
            memo[engine.name] = Path("")
            return Path("")

        dep_name = dependency_map[engine.name]
        if dep_name:
            dep_engine = engines[dep_name]
            dep_input_file = get_or_render_input_file(dep_engine)
            if dep_input_file == Path("") and not dep_engine.is_internal:
                logger.warning(
                    f"Render engine '{engine.name}' is disabled because dependent engine '{dep_name}' is disabled."
                )
                memo[engine.name] = Path("")
                return Path("")

            template_file_path = engine.input_file
            if not template_file_path.exists():
                logger.warning(
                    f"Input template file for render engine '{engine.name}' not found: {template_file_path}. Engine '{engine.name}' is disabled."
                )
                memo[engine.name] = Path("")
                return Path("")

            output_filename = dep_engine.strip_suffix(template_file_path.name)
            output_file_path = target_output_dir / output_filename

            logger.info(f"🎨 Rendering engine input: {engine.name} (via {dep_name})")
            logger.debug(f"   {template_file_path} -> {output_file_path}")

            try:
                render_template_to_file(
                    engine_config=dep_engine,
                    drift_root=drift_root,
                    template_file_path=template_file_path,
                    output_file_path=output_file_path,
                    input_file_path=dep_input_file if dep_input_file != Path("") else None,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to render input template for engine '{engine.name}': {e}. Engine '{engine.name}' is disabled."
                )
                memo[engine.name] = Path("")
                return Path("")

            memo[engine.name] = output_file_path
            return output_file_path
        else:
            path = resolve_static_input_file(engine.input_file, engine.name)
            memo[engine.name] = path
            return path

    for engine in engines.values():
        engine.input_file = get_or_render_input_file(engine)
