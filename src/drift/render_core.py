"""Core rendering engine template compiler functions using pathlib."""

import logging
import subprocess
from pathlib import Path
from typing import Optional

from .workspace_config import RenderEngineConfig
from .constants import CONFIG_DIR_NAME

logger = logging.getLogger(__name__)


def resolve_render_template_args(
    engine_config: RenderEngineConfig,
    engine_config_input_relative_to: Path,
    template_file_path: Path,
    input_file_path: Optional[Path] = None
) -> str:
    """Checks the validity of the arguments for rendering a template and resolves the input file path."""
    if not template_file_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_file_path}")

    # %i and %s placeholders must occur in the command string, otherwise raise an error
    if "%i" not in engine_config.render_command:
        raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%i' placeholder for input file.")
    if "%s" not in engine_config.render_command:
        raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%s' placeholder for template file.")

    resolved_input_file = input_file_path
    if not resolved_input_file:
        # Resolve input file if not explicitly provided
        if not engine_config.input_file or str(engine_config.input_file) in ("", "."):
            raise ValueError(f"Render engine '{engine_config.name}' requires an input file")
            
        p_engine_input = engine_config.input_file
        if p_engine_input.is_absolute():
            # 1. Try directly as absolute path
            if not p_engine_input.exists():
                raise FileNotFoundError(f"Input file specified in engine config does not exist: {engine_config.input_file}")
            resolved_input_file = p_engine_input
        else:
            # 2. Try relative path
            config_path = engine_config_input_relative_to / p_engine_input
            if not config_path.exists():
                raise FileNotFoundError(f"Input file specified in engine config does not exist under '{CONFIG_DIR_NAME}' folder: {config_path}")
            resolved_input_file = config_path

    if not resolved_input_file.exists():
        raise FileNotFoundError(f"Resolved input file does not exist: {resolved_input_file}")

    return str(resolved_input_file)


def render_template(
    engine_config: RenderEngineConfig,
    drift_root: Path,
    template_file_path: Path,
    input_file_path: Optional[Path] = None
) -> str:
    """Renders a template file to a string using a specified render engine configuration.

    The engine configuration provides the render command (e.g. "bash -c 'source %i && envsubst < %s'"),
    where %i is replaced with the path to the input file and %s with the template file.

    Args:
        engine_config: The RenderEngineConfig instance to use.
        drift_root: The root directory of the drift workspace, used to resolve relative paths.
        template_file_path: The physical path to the template file to render.
        input_file_path: Optional explicit path to the engine's input file.

    Returns:
        The rendered template content as a string.

    Raises:
        FileNotFoundError: If the template file or input file is missing.
        ValueError: If an input file is required but cannot be found or resolved.
        RuntimeError: If the render subprocess fails.
    """
    resolved_input_file = resolve_render_template_args(
        engine_config=engine_config,
        engine_config_input_relative_to=drift_root / CONFIG_DIR_NAME,
        template_file_path=template_file_path,
        input_file_path=input_file_path
    )
    cmd = engine_config.render_command
    cmd = cmd.replace("%i", resolved_input_file)
    cmd = cmd.replace("%s", str(template_file_path))

    logger.debug(f"Executing render command: {cmd}")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        err_msg = (
            f"Render command failed with exit code {e.returncode}.\n"
            f"Command: {cmd}\n"
            f"Stderr: {e.stderr}"
        )
        raise RuntimeError(err_msg) from e


def render_template_to_file(
    engine_config: RenderEngineConfig,
    drift_root: Path,
    template_file_path: Path,
    output_file_path: Path,
    input_file_path: Optional[Path] = None
) -> None:
    """Renders a template file and writes the output directly to the specified file path.

    Automatically creates any missing parent directories for the output file.

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
