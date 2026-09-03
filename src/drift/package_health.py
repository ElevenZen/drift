"""Package runtime health check and probe execution engine."""

import logging
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig, load_config_for_install, load_package_config_from_source_dir
from .state_registry import load_state_registry
from .constants import PACKAGE_CONFIG_FILE_NAME, PackageStage
from .result_models import (
    PackageHealthStatus,
    PackageHealthResult,
    HealthResult,
    HookResult,
)

from .lifecycle_hooks import (
    trigger_package_hook_with_render,
    trigger_package_lifecycle_hook,
)

logger = logging.getLogger(__name__)


def _map_hook_result_to_health_result(
    hook_res: HookResult,
    pkg: str,
    target_dir: Path
) -> PackageHealthResult:
    """Converts a lifecycle HookResult into a PackageHealthResult."""
    if hook_res.status == "SKIPPED":
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NO_HOOK,
            target_directory=str(target_dir)
        )
    if hook_res.status == "SUCCESS":
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.HEALTHY,
            exit_code=hook_res.exit_code,
            stdout=hook_res.stdout or "",
            stderr=hook_res.stderr or "",
            duration_ms=hook_res.duration_ms,
            hook_path=hook_res.hook_path,
            target_directory=str(target_dir)
        )
    is_timeout = (hook_res.exit_code == 124) or ("timed out" in (hook_res.error_message or "").lower())
    status = PackageHealthStatus.TIMEOUT if is_timeout else PackageHealthStatus.UNHEALTHY
    return PackageHealthResult(
        package=pkg,
        status=status,
        exit_code=hook_res.exit_code,
        stdout=hook_res.stdout or "",
        stderr=hook_res.stderr or "",
        duration_ms=hook_res.duration_ms,
        hook_path=hook_res.hook_path,
        target_directory=str(target_dir),
        error_message=hook_res.error_message or ""
    )


def _execute_health_hook_no_throw(
    trigger_fn,
    pkg: str,
    target_dir: Path
) -> PackageHealthResult:
    """Safely executes a health hook trigger function, handling errors and mapping to PackageHealthResult."""
    try:
        hook_res = trigger_fn()
    except FileNotFoundError as e:
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.MISSING_HOOK,
            hook_path=str(e),
            target_directory=str(target_dir),
            error_message=str(e)
        )
    except Exception as e:
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.ERROR,
            target_directory=str(target_dir),
            error_message=str(e)
        )

    return _map_hook_result_to_health_result(hook_res, pkg, target_dir)


def run_health_probe_from_source(
    workspace_config: WorkspaceConfig,
    pkg: str,
    custom_timeout: Optional[int] = None
) -> PackageHealthResult:
    """Executes the health probe hook by reading and compiling templates from the src/ directory."""
    src_pkg_dir = workspace_config.source_path / pkg

    try:
        pkg_config = load_package_config_from_source_dir(src_pkg_dir, workspace_config)
        target_dir = pkg_config.get_target_directory(workspace_config)
    except FileNotFoundError:
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NO_HOOK,
            error_message=f"Package directory not found in source base: '{src_pkg_dir}'"
        )
    except Exception as e:
        logger.warning(f"Failed to load package config for '{pkg}': {e}")
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.ERROR,
            error_message=f"Failed to parse package configuration: {e}"
        )

    return _execute_health_hook_no_throw(
        lambda: trigger_package_hook_with_render(
            workspace_config=workspace_config,
            package_name=pkg,
            hook_name="health",
            pkg_config=pkg_config,
            custom_cwd=target_dir,
            load_envs=True,
            no_hooks=False,
            raise_on_error=False,
            custom_timeout=custom_timeout
        ),
        pkg=pkg,
        target_dir=target_dir
    )


def run_health_probe_from_install(
    workspace_config: WorkspaceConfig,
    pkg: str,
    custom_timeout: Optional[int] = None
) -> PackageHealthResult:
    """Executes the health probe hook directly from static files in the install/ directory without render."""
    install_pkg_dir = workspace_config.install_path / pkg

    try:
        pkg_config = load_config_for_install(workspace_config.install_path, pkg)
        target_dir = pkg_config.get_target_directory(workspace_config)
    except FileNotFoundError:
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.NOT_INSTALLED,
            error_message=f"Package directory not found in install base: '{install_pkg_dir}'"
        )
    except Exception as e:
        logger.warning(f"Failed to load package config for '{pkg}': {e}")
        return PackageHealthResult(
            package=pkg,
            status=PackageHealthStatus.ERROR,
            error_message=f"Failed to parse package configuration: {e}"
        )

    def _trigger():
        with pkg_config.package_envs(workspace_config):
            return trigger_package_lifecycle_hook(
                pkg=pkg,
                hook_name="health",
                metadata=pkg_config,
                hook_base_dir=install_pkg_dir,
                cwd=target_dir,
                raise_on_error=False,
                custom_timeout=custom_timeout
            )

    return _execute_health_hook_no_throw(_trigger, pkg=pkg, target_dir=target_dir)


def run_single_package_health_probe(
    workspace_config: WorkspaceConfig,
    pkg: str,
    custom_timeout: Optional[int] = None,
    from_stage: Union[str, PackageStage] = PackageStage.INSTALL
) -> PackageHealthResult:
    """Executes the health probe hook for a single package from either source or install base.

    If from_stage == PackageStage.INSTALL (default):
        Executes the static/staged hook script directly from install/<pkg>/ without rendering.
    If from_stage == PackageStage.SOURCE:
        Finds the hook script in src/<pkg>/, renders templates on-the-fly into render/<pkg>/,
        and executes the compiled script.
    """
    stage = PackageStage.from_str(from_stage)
    if stage == PackageStage.SOURCE:
        return run_health_probe_from_source(
            workspace_config=workspace_config,
            pkg=pkg,
            custom_timeout=custom_timeout
        )
    return run_health_probe_from_install(
        workspace_config=workspace_config,
        pkg=pkg,
        custom_timeout=custom_timeout
    )


def run_primitive_health_checks(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    custom_timeout: Optional[int] = None,
    from_stage: Union[str, PackageStage] = PackageStage.INSTALL
) -> HealthResult:
    """Runs health check probes across specified or all packages from install or source directory."""
    stage = PackageStage.from_str(from_stage)
    if package_names is not None and len(package_names) > 0:
        targets = package_names
    else:
        if stage == PackageStage.SOURCE:
            targets = workspace_config.get_source_packages()
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
            custom_timeout=custom_timeout,
            from_stage=from_stage
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
