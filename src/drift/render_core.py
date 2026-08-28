import os
import re
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict, Type

from .workspace_config import RenderEngineConfig
from .constants import CONFIG_DIR_NAME, INTERNAL_RENDER_COMMAND
from .file_utils import run_command
from .exceptions import DriftError, RenderError

logger = logging.getLogger(__name__)


def python_envsubst(
    template_content: str,
    env: Optional[Dict[str, str]] = None,
    error_cls: Type[DriftError] = RenderError
) -> str:
    """Pure-Python envsubst equivalent for platforms without GNU gettext or as fallback.

    Args:
        template_content: Raw template string containing $VAR or ${VAR}.
        env: Optional dictionary of environment variables (defaults to os.environ).
        error_cls: Exception class to raise on missing variable (RenderError or ConfigError).

    Returns:
        The rendered template content as a string.

    Raises:
        error_cls: If any referenced variable is not defined in the environment.
    """
    environ = env if env is not None else os.environ
    pattern = re.compile(r"\$(?:\{([a-zA-Z_][a-zA-Z0-9_]*)\}|([a-zA-Z_][a-zA-Z0-9_]*))")

    def replace_var(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        if var_name not in environ:
            raise error_cls(
                f"Environment variable '${var_name}' referenced in template "
                f"was not found in [env], secrets.env, or process environment."
            )
        return str(environ[var_name])

    return pattern.sub(replace_var, template_content)


def python_envsubst_template(
    template_file_path: Path,
    engine_config: RenderEngineConfig,
    error_cls: Type[DriftError] = RenderError
) -> str:
    """Validates template existence and engine status, then renders template content via python_envsubst."""
    if not template_file_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_file_path}")
    if engine_config.is_disabled:
        raise RenderError(f"Render engine '{engine_config.name}' is disabled.")
    content = template_file_path.read_text(encoding="utf-8")
    return python_envsubst(content, error_cls=error_cls)


def resolve_render_template_args(
    engine_config: RenderEngineConfig,
    engine_config_input_relative_to: Path,
    template_file_path: Path,
    input_file_path_override: Optional[Path] = None
) -> Path:
    """Checks the validity of the arguments for rendering a template and resolves the input file path."""
    if not template_file_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_file_path}")

    if engine_config.is_internal:
        return Path("")

    # %i and %s placeholders must occur in the command string, otherwise raise an error
    if "%i" not in engine_config.render_command:
        raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%i' placeholder for input file.")
    if "%s" not in engine_config.render_command:
        raise ValueError(f"Render command for engine '{engine_config.name}' must contain '%s' placeholder for template file.")

    if input_file_path_override:
        if str(input_file_path_override) == "":
            raise RenderError(f"Render engine '{engine_config.name}' is disabled or has an invalid/empty input file.")
        if not input_file_path_override.exists():
            raise FileNotFoundError(f"Input file does not exist: {input_file_path_override}")
        resolved_input_file = input_file_path_override
    else:
        # Resolve input file if not explicitly provided
        if engine_config.is_disabled:
            raise RenderError(f"Render engine '{engine_config.name}' is disabled or has an invalid/empty input file.")
        config_path = engine_config_input_relative_to / engine_config.input_file
        if not config_path.exists():
            if config_path.is_absolute():
                raise FileNotFoundError(f"Input file specified in engine config does not exist: {engine_config.input_file}")
            else:
                raise FileNotFoundError(f"Input file specified in engine config does not exist under '{CONFIG_DIR_NAME}' folder: {config_path}")
        resolved_input_file = config_path

    return resolved_input_file


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
        return python_envsubst_template(template_file_path, engine_config)

    if engine_config.name == "envsubst" and (shutil.which("bash") is None or shutil.which("envsubst") is None):
        logger.info(f"Using internal python_envsubst engine for '{template_file_path}' (bash or envsubst not found)")
        return python_envsubst_template(template_file_path, engine_config)

    resolved_input_file: Path = resolve_render_template_args(
        engine_config=engine_config,
        engine_config_input_relative_to=drift_root / CONFIG_DIR_NAME,
        template_file_path=template_file_path,
        input_file_path_override=input_file_path
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
            return python_envsubst_template(template_file_path, engine_config)
        err_msg = (
            f"Render command failed with exit code {getattr(e, 'returncode', 'unknown')}.\n"
            f"Command: {cmd}\n"
            f"Stderr: {getattr(e, 'stderr', str(e))}"
        )
        raise RenderError(err_msg) from e


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
