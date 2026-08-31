"""Package runtime health check and probe execution engine."""

import logging
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig
from .install_repo import load_config_for_install
from .state_registry import load_state_registry
from .constants import PACKAGE_CONFIG_FILE_NAME, SUDO_ELIGIBLE_HOOKS
from .result_models import (
    PackageHealthStatus,
    PackageHealthResult,
    HealthResult,
)

logger = logging.getLogger(__name__)


def check_package_health_probe(
    workspace_config: WorkspaceConfig,
    pkg: str
) -> Union[PackageHealthResult, Tuple[PackageConfig, Path, Path]]:
    """Checks and validates whether a package has a runnable health probe hook.

    Returns:
        A PackageHealthResult if validation failed or hook is skipped,
        or a tuple of (pkg_config, hook_path, target_dir) if ready for execution.
    """
    install_pkg_dir = workspace_config.install_path / pkg

    if not install_pkg_dir.exists() or not install_pkg_dir.is_dir():
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NOT_INSTALLED,
            error_message=f"Package directory not found in install base: '{install_pkg_dir}'"
        )

    config_file = install_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not config_file.exists():
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NO_HOOK,
            target_directory=str(workspace_config.default_target_path)
        )

    try:
        pkg_config = load_config_for_install(workspace_config.install_path, pkg)
        target_dir = pkg_config.get_target_directory(workspace_config)
    except Exception as e:
        logger.warning(f"Failed to load package config for '{pkg}': {e}")
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.ERROR,
            error_message=f"Failed to parse package configuration: {e}"
        )

    if not pkg_config.hooks.health:
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NO_HOOK,
            target_directory=str(target_dir)
        )

    hook_file_path = Path(pkg_config.hooks.health)
    hook_path = hook_file_path if hook_file_path.is_absolute() else install_pkg_dir / hook_file_path

    if not hook_path.exists():
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.MISSING_HOOK,
            hook_path=str(hook_path),
            target_directory=str(target_dir),
            error_message=f"Configured health hook file does not exist: '{hook_path}'"
        )

    if not hook_path.is_file():
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.MISSING_HOOK,
            hook_path=str(hook_path),
            target_directory=str(target_dir),
            error_message=f"Configured health hook path is not a regular file: '{hook_path}'"
        )

    return pkg_config, hook_path, target_dir


def execute_package_health_probe(
    workspace_config: WorkspaceConfig,
    pkg_config: PackageConfig,
    hook_path: Path,
    target_dir: Path,
    custom_timeout: Optional[int] = None
) -> PackageHealthResult:
    """Executes a validated package health probe hook script and captures results."""
    pkg = pkg_config.name
    timeout = custom_timeout if custom_timeout is not None else pkg_config.hooks.timeout
    from .lifecycle_hooks import build_hook_execution_command
    from .file_utils import run_sudo_command

    cmd = build_hook_execution_command(hook_path)
    use_sudo = bool(pkg_config.sudo and "health" in SUDO_ELIGIBLE_HOOKS)

    start_time = time.perf_counter()
    with pkg_config.package_envs(workspace_config):
        try:
            res = run_sudo_command(
                cmd,
                sudo=use_sudo,
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False
            )
            duration_ms = (time.perf_counter() - start_time) * 1000
            status = PackageHealthStatus.HEALTHY if res.returncode == 0 else PackageHealthStatus.UNHEALTHY
            return PackageHealthResult(
                package=pkg,
                status=status,
                exit_code=res.returncode,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                duration_ms=duration_ms,
                hook_path=str(hook_path),
                target_directory=str(target_dir)
            )
        except subprocess.TimeoutExpired as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            stdout_str = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr_str = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            return PackageHealthResult(
                package=pkg,
                status=PackageHealthStatus.TIMEOUT,
                stdout=stdout_str,
                stderr=stderr_str,
                duration_ms=duration_ms,
                hook_path=str(hook_path),
                target_directory=str(target_dir),
                error_message=f"Probe timed out after {timeout}s"
            )
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            return PackageHealthResult(
                package=pkg,
                status=PackageHealthStatus.ERROR,
                duration_ms=duration_ms,
                hook_path=str(hook_path),
                target_directory=str(target_dir),
                error_message=str(e)
            )


def run_single_package_health_probe(
    workspace_config: WorkspaceConfig,
    pkg: str,
    custom_timeout: Optional[int] = None
) -> PackageHealthResult:
    """Executes the health probe hook for a single installed package.

    The health hook script is read from the package's install/ directory (install/<pkg>/)
    and executed with the package's host target directory as the working directory (CWD).
    All package environment variables ($drift_package_name, $drift_package_target_dir, etc.)
    are injected during execution.
    """
    check_result = check_package_health_probe(workspace_config, pkg)
    if isinstance(check_result, PackageHealthResult):
        return check_result

    pkg_config, hook_path, target_dir = check_result
    return execute_package_health_probe(
        workspace_config=workspace_config,
        pkg_config=pkg_config,
        hook_path=hook_path,
        target_dir=target_dir,
        custom_timeout=custom_timeout
    )


def run_primitive_health_checks(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    custom_timeout: Optional[int] = None
) -> HealthResult:
    """Runs health check probes across specified or all installed packages."""
    if package_names is not None and len(package_names) > 0:
        targets = package_names
    else:
        state_file = workspace_config.install_path / "state.toml"
        if state_file.exists():
            registry = load_state_registry(state_file)
            targets = list(registry.packages.keys())
        else:
            targets = []

    if not targets:
        return HealthResult(
            command="health",
            status="SUCCESS",
            packages=[],
            healthy_count=0,
            unhealthy_count=0,
            skipped_count=0,
            total_duration_ms=0.0
        )

    results: List[PackageHealthResult] = []
    healthy_count = 0
    unhealthy_count = 0
    skipped_count = 0
    total_duration = 0.0

    for pkg in targets:
        probe_res = run_single_package_health_probe(
            workspace_config=workspace_config,
            pkg=pkg,
            custom_timeout=custom_timeout
        )
        results.append(probe_res)
        total_duration += probe_res.duration_ms

        if probe_res.status == PackageHealthStatus.HEALTHY:
            healthy_count += 1
        elif probe_res.status in (PackageHealthStatus.UNHEALTHY, PackageHealthStatus.TIMEOUT, PackageHealthStatus.ERROR):
            unhealthy_count += 1
        else:
            skipped_count += 1

    overall_status = "SUCCESS" if unhealthy_count == 0 else "FAILED"

    return HealthResult(
        command="health",
        status=overall_status,
        packages=results,
        healthy_count=healthy_count,
        unhealthy_count=unhealthy_count,
        skipped_count=skipped_count,
        total_duration_ms=total_duration
    )
