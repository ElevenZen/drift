"""Template rendering core primitives and execution pipeline.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: File Output Compilation (Public Entry Point)
    render_template_to_file(engine_config, drift_root, template_file_path, output_file_path, input_file_path)
        1. Render Template Content:
            render_template [Layer 2]
        2. Write Destination & Preserve Mode:
            output_file_path.parent.mkdir
            output_file_path.write_text
            shutil.copymode (preserve permissions/mode)

Layer 2: Core Render Execution
    render_template(engine_config, drift_root, template_file_path, input_file_path)
        1. Internal Engine Path (is_internal or fallback):
            render_template_internal [Layer 2]
                python_envsubst
        2. External Engine Validation & Input Resolution:
            validate_render_template_args [Layer 1]
            resolve_render_input_file [Layer 1]
        3. Subprocess Command Execution:
            run_command (with %i and %s placeholder substitution)
            Fallback to render_template_internal on envsubst failure

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Validation & Path Resolution Helpers
        validate_render_template_args
        resolve_render_input_file
    Layer 2: Core Render Execution
        render_template_internal
        render_template
    Layer 3: File Output Compilation
        render_template_to_file
===============================================================================
"""

import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional, Type

from .workspace_config import RenderEngineConfig
from .constants import CONFIG_DIR_NAME
from .file_utils import run_command
from .exceptions import DriftError, RenderError
from .env_utils import python_envsubst

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Validation & Path Resolution Helpers
# =====================================================================

def validate_render_template_args(
    engine_config: RenderEngineConfig,
    template_file_path: Path,
) -> None:
    """Validates that the template file exists and engine command placeholders are present."""
    if not template_file_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_file_path}")

    if not engine_config.is_internal:
        if "%i" not in engine_config.render_command:
            raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%i' placeholder for input file.")
        if "%s" not in engine_config.render_command:
            raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%s' placeholder for template file.")


def resolve_render_input_file(
    engine_config: RenderEngineConfig,
    engine_config_input_relative_to: Path,
    input_file_path_override: Optional[Path] = None,
) -> Path:
    """Resolves and validates the input file path for an external render engine."""
    if input_file_path_override:
        if str(input_file_path_override) == "":
            raise RenderError(f"Render engine '{engine_config.name}' is disabled or has an invalid/empty input file.")
        if not input_file_path_override.exists():
            raise FileNotFoundError(f"Input file does not exist: {input_file_path_override}")
        return input_file_path_override

    if engine_config.is_disabled:
        raise RenderError(f"Render engine '{engine_config.name}' is disabled or has an invalid/empty input file.")

    config_path = engine_config_input_relative_to / engine_config.input_file
    if not config_path.exists():
        if config_path.is_absolute():
            raise FileNotFoundError(f"Input file specified in engine config does not exist: {engine_config.input_file}")
        raise FileNotFoundError(
            f"Input file specified in engine config does not exist under '{CONFIG_DIR_NAME}' folder: {config_path}"
        )
    return config_path


# =====================================================================
# Layer 2: Core Render Execution
# =====================================================================

def render_template_internal(
    template_file_path: Path,
    error_cls: Type[DriftError] = RenderError,
) -> str:
    """Validates template file existence and renders its content using the internal python_envsubst engine."""
    if not template_file_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_file_path}")
    content = template_file_path.read_text(encoding="utf-8")
    return python_envsubst(content, error_cls=error_cls)


def render_template(
    engine_config: RenderEngineConfig,
    drift_root: Path,
    template_file_path: Path,
    input_file_path: Optional[Path] = None
) -> str:
    """Renders a template file to a string using a specified render engine configuration.

    The engine configuration provides the render command (e.g. "bash -c 'source %i && envsubst < %s'"),
    where %i is replaced with the path to the input file and %s with the template file.
    If the engine is internal (render_command="internal") or if the engine is 'envsubst'
    and 'bash' or 'envsubst' is not available, falls back to python_envsubst.

    Args:
        engine_config: The RenderEngineConfig instance to use.
        drift_root: The root directory of the drift workspace, used to resolve relative paths.
        template_file_path: The physical path to the template file to render.
        input_file_path: Optional explicit path to the engine's input file.

    Returns:
        The rendered template content as a string.

    Raises:
        FileNotFoundError: If the template file or input file is missing.
        ValueError: If placeholders are missing in the render command.
        RenderError: If the render engine is disabled or subprocess fails.
    """
    if engine_config.is_internal:
        logger.debug(f"Using internal python_envsubst engine for '{template_file_path}'")
        return render_template_internal(template_file_path)

    if engine_config.name == "envsubst" and (shutil.which("bash") is None or shutil.which("envsubst") is None):
        logger.info(f"Using internal python_envsubst engine for '{template_file_path}' (bash or envsubst not found)")
        return render_template_internal(template_file_path)

    validate_render_template_args(engine_config, template_file_path)
    resolved_input_file = resolve_render_input_file(
        engine_config=engine_config,
        engine_config_input_relative_to=drift_root / CONFIG_DIR_NAME,
        input_file_path_override=input_file_path,
    )
    cmd: str = engine_config.render_command
    cmd = cmd.replace("%i", str(resolved_input_file))
    cmd = cmd.replace("%s", str(template_file_path))

    try:
        result = run_command(cmd, shell=True, text=True)
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if engine_config.name == "envsubst":
            logger.warning(
                f"External envsubst command failed ({e}). Falling back to internal python_envsubst for '{template_file_path}'."
            )
            return render_template_internal(template_file_path)
        err_msg = (
            f"Render command failed with exit code {getattr(e, 'returncode', 'unknown')}.\n"
            f"Command: {cmd}\n"
            f"Stderr: {getattr(e, 'stderr', str(e))}"
        )
        raise RenderError(err_msg) from e


# =====================================================================
# Layer 3: File Output Compilation (Public Entry Point)
# =====================================================================

def render_template_to_file(
    engine_config: RenderEngineConfig,
    drift_root: Path,
    template_file_path: Path,
    output_file_path: Path,
    input_file_path: Optional[Path] = None
) -> None:
    """Renders a template file and writes the output directly to the specified file path.

    Automatically creates any missing parent directories for the output file.
    Preserves file permissions (mode) from the template file onto the rendered output file.

    Args:
        engine_config: The RenderEngineConfig instance to use.
        drift_root: The root directory of the drift workspace, used to resolve relative paths.
        template_file_path: The physical path to the template file to render.
        output_file_path: The path where the rendered content will be written.
        input_file_path: Optional explicit path to the engine's input file.

    Raises:
        FileNotFoundError, ValueError, RuntimeError: Same as render_template.
    """
    rendered_content = render_template(
        engine_config=engine_config,
        drift_root=drift_root,
        template_file_path=template_file_path,
        input_file_path=input_file_path
    )

    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    output_file_path.write_text(rendered_content, encoding="utf-8")
    try:
        shutil.copymode(template_file_path, output_file_path)
    except Exception:
        pass
