from pathlib import Path
from typing import Optional, Sequence, List, Any, Mapping, TypeVar

from .constants import ExitCode

_ExcT = TypeVar("_ExcT", bound=BaseException)


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


def mark_logged(exc: _ExcT) -> _ExcT:
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


class CyclicDependencyError(ConfigError, ValueError):
    """Raised when circular dependencies are detected in packages, render engines, or DAG nodes."""
    exit_code: int = ExitCode.CONFIG_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        cycle: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)
        self.cycle: List[str] = list(cycle) if cycle is not None else []



class WorkspaceNotFoundError(ConfigError, FileNotFoundError):
    """Raised when no drift workspace root (a directory containing a workspace config marker
    under 'config/') is found in the start directory or any of its ancestors."""
    exit_code: int = ExitCode.CONFIG_ERROR


class SafeguardError(DriftError, RuntimeError):
    """Base class for operational safeguards that protect against unintended actions and can be bypassed via CLI flags (--adhoc, --no-deps, or --force)."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class DriftDetectedError(SafeguardError):
    """Raised in stage_repo / deploy operations when unadopted live host drift or uncommitted install modifications block staging (bypassed via --force)."""
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


class MidwayTransactionError(SafeguardError):
    """Raised when package(s) are in an uncommitted midway transaction state ('staging' or 'installing', bypassed via --force)."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str,
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class MidwayCrashError(DriftError, RuntimeError):
    """Raised when a state-mutating deployment step (staging or physical install) fails midway without automatic rollback."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)


class DirtySourceFileError(SafeguardError):
    """Raised during drift adoption when a target template file in src/ has uncommitted Git modifications (bypassed via --force)."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        source_file: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)
        self.source_file: Optional[Path] = source_file


class PackageDisabledError(SafeguardError):
    """Raised when attempting to deploy or plan an explicitly targeted package that is disabled in workspace configuration (bypassed via --adhoc or --force)."""
    exit_code: int = ExitCode.GENERAL_ERROR


class PackageEnabledError(SafeguardError):
    """Raised when attempting to uninstall a package that is still enabled in workspace configuration (bypassed via --adhoc or --force)."""
    exit_code: int = ExitCode.GENERAL_ERROR


class MissingDependencyError(SafeguardError, ConfigError):
    """Raised when required package dependencies are missing from the installable universe or broken by uninstallation (bypassed via --no-deps or --force)."""
    exit_code: int = ExitCode.CONFIG_ERROR

    def __init__(
        self,
        message: str = "",
        packages: Optional[Sequence[str]] = None,
        missing_dependencies: Optional[Mapping[str, Sequence[str]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, packages=packages, **kwargs)
        self.missing_dependencies: dict[str, List[str]] = (
            {pkg: list(deps) for pkg, deps in missing_dependencies.items()}
            if missing_dependencies is not None
            else {}
        )


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


class AdminPrivilegeError(DriftError, PermissionError):
    """Raised when an operation requires elevated Administrator/root privileges that are not held."""
    exit_code: int = ExitCode.GENERAL_ERROR

    def __init__(
        self,
        message: str = "",
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
    """Raised during install_repo collision guard safety aborts (e.g. target directory resolving inside drift_root)."""
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
        is_fatal: bool = True,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message, packages=[package] if package else [])
        self.package = package
        self.hook_name = hook_name
        self.message = message
        self.is_fatal = is_fatal
        self.hook_exit_code = exit_code
