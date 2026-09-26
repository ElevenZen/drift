"""Package Pre-flight Validation Guards & Assertion Helpers.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pre-flight Assertion Guards:
    - assert_packages_hooks_exist(pkg_metadata, base_dir, is_source, hook_names)
        * Inspects lifecycle hook files across all target packages via check_missing_hooks.
        * Aggregates all missing or non-file hooks across the batch.
        * Raises HookMissingError(packages=[...]) if any failures are detected.

    - assert_install_pkg_dirs_clean(install_base, packages)
        * Inspects install/ package directories for uncommitted git modifications via has_uncommitted_modifications.
        * Aggregates all dirty package directories across the batch.
        * Raises DriftDetectedError(packages=[...]) if any unclean directories are detected.

-------------------------------------------------------------------------------
Layers:
    Layer 1: Package-level Pre-flight Assertion Primitives
        assert_packages_hooks_exist
        assert_install_pkg_dirs_clean
===============================================================================
"""

from pathlib import Path
from typing import Union, Iterable, Sequence, Mapping, Dict, List, Tuple

from ..config.package_config import PackageConfig
from ..core.exceptions import DriftDetectedError, HookMissingError
from ..utils.git_utils import has_uncommitted_modifications


def assert_packages_hooks_exist(
    pkg_metadata: Mapping[str, PackageConfig],
    base_dir: Path,
    is_source: bool = False,
    hook_names: Sequence[str] = (),
) -> None:
    """Validates that configured lifecycle hook files exist and are regular files for all packages.

    Collects all hook assertion failures across all packages before raising.

    Args:
        pkg_metadata: Mapping from package name to PackageConfig.
        base_dir: Base directory containing package folders (e.g. render/, install/, or src/).
        is_source: True if validating source directory (src/), False if rendered/staged (render/ or install/).
        hook_names: Specific hook names to check, or empty sequence for all lifecycle hooks.

    Raises:
        HookMissingError: If one or more hook files are missing or invalid across the packages.
    """
    hook_failures: Dict[str, List[Tuple[str, Path, str]]] = {
        pkg: missing
        for pkg, metadata in pkg_metadata.items()
        if (missing := metadata.hooks.check_missing_hooks(base_dir / pkg, is_source=is_source, hook_names=hook_names))
    }
    if hook_failures:
        lines = [
            f"❌ Lifecycle hook file validation failed for {len(hook_failures)} package(s):"
        ]
        for pkg, failures in sorted(hook_failures.items()):
            for _, _, reason in failures:
                lines.append(f"  • {reason}")
        raise HookMissingError(
            "\n".join(lines),
            packages=sorted(hook_failures.keys()),
        )


def assert_install_pkg_dirs_clean(
    install_base: Path,
    packages: Union[str, Iterable[str]],
) -> None:
    """Verifies that none of the target package directories in install/ have uncommitted local git changes.

    Collects all unclean package directories across the batch before raising.

    Args:
        install_base: Path to the install/ repository base directory.
        packages: Single package name or iterable of package names to check.

    Raises:
        DriftDetectedError: If one or more package directories in install/ have uncommitted modifications.
    """
    pkg_list = [packages] if isinstance(packages, str) else list(packages)
    dirty_pkgs = [
        pkg
        for pkg in sorted(pkg_list)
        if (install_base / pkg).is_dir() and has_uncommitted_modifications(install_base, install_base / pkg)
    ]
    if dirty_pkgs:
        details = ", ".join(f"'{pkg}'" for pkg in dirty_pkgs)
        raise DriftDetectedError(
            f"Safety Abort: Package(s) {details} in install directory has uncommitted local modifications.\n"
            f"Please commit or stash your changes before staging, or use --force flag to bypass this check.",
            packages=dirty_pkgs,
        )
