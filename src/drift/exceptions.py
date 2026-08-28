"""Domain exception hierarchy for the Drift dotfiles manager."""

from .constants import ExitCode


class DriftError(Exception):
    """Base class for all Drift domain exceptions."""
    exit_code: int = ExitCode.GENERAL_ERROR


class ConfigError(DriftError, ValueError):
    """Raised when configuration files (drift.toml, drift.local.toml, drift_package.toml, secrets.env) are invalid, missing, or corrupt."""
    exit_code: int = ExitCode.CONFIG_ERROR


class DriftDetectedError(DriftError, RuntimeError):
    """Raised in stage_repo / deploy operations when unadopted live host drift or uncommitted install modifications block staging."""
    exit_code: int = ExitCode.DRIFT_DETECTED


class RenderError(DriftError, RuntimeError):
    """Raised when template compilation or render engine pipelines fail."""
    exit_code: int = ExitCode.RENDER_ERROR


class CollisionError(DriftError, RuntimeError):
    """Raised during install_repo collision guard safety aborts (untracked destination path collision or cross-package state collision)."""
    exit_code: int = ExitCode.COLLISION_ERROR
