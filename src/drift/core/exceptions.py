from typing import Optional, Sequence, List, Any, Mapping

from .constants import ExitCode


class DriftError(Exception):
    """Base class for all Drift domain exceptions."""
    exit_code: int = ExitCode.GENERAL_ERROR
    logged: bool = False

    def __init__(
        self,
        *args: Any,
        packages: Optional[Sequence[str]] = None,
        logged: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args)
        self.packages: List[str] = list(packages) if packages is not None else []
        self.logged = logged


def mark_logged(exc: BaseException) -> BaseException:
    """Marks any exception instance (built-in or domain) as having been logged to console output."""
    setattr(exc, "logged", True)
    return exc


def is_logged(exc: BaseException) -> bool:
    """Returns True if the exception has already been logged to console output."""
    return getattr(exc, "logged", False)


def is_drift_error(exc: BaseException) -> bool:
    """Returns True if the exception is a custom Drift domain error."""
    return isinstance(exc, DriftError)


class ConfigError(DriftError, ValueError, TypeError):
    """Raised when configuration files (drift_workspace.toml, drift_workspace.local.toml, drift_package.toml, secrets.env) are invalid, missing, or corrupt."""
    exit_code: int = ExitCode.CONFIG_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class DriftDetectedError(DriftError, RuntimeError):
    """Raised in stage_repo / deploy operations when unadopted live host drift or uncommitted install modifications block staging."""
    exit_code: int = ExitCode.DRIFT_DETECTED

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class HookMissingError(DriftError, FileNotFoundError):
    """Raised when one or more configured lifecycle hook files are missing or invalid."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        hook_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)
        self.hook_name = hook_name


class MidwayTransactionError(DriftError, RuntimeError):
    """Raised when package(s) are in an uncommitted midway transaction state ('staging' or 'installing')."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class PackageInstallDirMissingError(DriftError, FileNotFoundError):
    """Raised during pre-flight checks when staged install directories for target packages are missing."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class TargetPermissionError(DriftError, PermissionError):
    """Raised during pre-flight checks when target destination directories are not writable on the host system."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class RenderError(DriftError, RuntimeError):
    """Raised when template compilation or render engine pipelines fail."""
    exit_code: int = ExitCode.RENDER_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class RenderCollisionError(RenderError):
    """Raised when multiple source files in a package render or copy to the same destination path in the render sandbox."""
    exit_code: int = ExitCode.RENDER_ERROR


class InstallCollisionError(DriftError, RuntimeError):
    """Raised during install_repo collision guard safety aborts (e.g. target directory or parent symlink resolving inside drift_root)."""
    exit_code: int = ExitCode.COLLISION_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class CrossPackageCollisionError(InstallCollisionError):
    """Raised when two or more packages have colliding destination target paths during deployment."""
    exit_code: int = ExitCode.COLLISION_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        conflicts: Optional[Mapping[Any, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)
        self.conflicts: Optional[Mapping[Any, Any]] = conflicts


class HookExecutionError(DriftError, RuntimeError):
    """Raised when a lifecycle hook execution script fails."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        package: str,
        hook_name: str,
        message: str,
        requires_rollback: bool = True,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message, packages=[package] if package else [])
        self.package = package
        self.hook_name = hook_name
        self.message = message
        self.requires_rollback = requires_rollback
        self.hook_exit_code = exit_code
