"""State registry subsystem using pathlib."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, List, Mapping, Tuple, Iterable, Any
from ..utils.toml_utils import parse_toml
from ..utils.path_utils import encode_dot_prefix
from ..utils.git_utils import has_uncommitted_modifications
from ..utils.process_utils import run_command
from .exceptions import ConfigError
from .constants import MIDWAY_TRANSACTION_STATES, ROLLBACK_ELIGIBLE_STATES, InstallMethod



@dataclass
class PackageState:
    """Represents the recorded state of a single package."""
    state: str
    target_directory: Optional[Path] = None
    last_deployed: Optional[str] = None
    install_method: Optional[InstallMethod] = None
    deployed_files: List[Path] = field(default_factory=list)
    sudo: bool = False


class StateRegistry:
    """Manages reading, updating, and saving install/state.toml with timestamps and metadata."""

    def __init__(self, packages: Mapping[str, PackageState] = {}, state_file: Optional[Path] = None):
        if not isinstance(packages, (dict, Mapping)):
            raise ConfigError(f"packages must be a Mapping, got {type(packages).__name__}")
        self.packages = dict(packages)
        self.state_file = state_file

    def get_package_state(self, pkg: str) -> Optional[str]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.state
        return None

    def get_package_target_directory(self, pkg: str) -> Optional[Path]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.target_directory
        return None

    def get_package_install_method(self, pkg: str) -> Optional[InstallMethod]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.install_method
        return None

    def set_package_state(
        self,
        pkg: str,
        state: str,
        last_deployed: Optional[str] = None,
    ) -> None:
        if pkg not in self.packages:
            self.packages[pkg] = PackageState(
                state=state,
                last_deployed=last_deployed,
            )
        else:
            self.packages[pkg].state = state
            if last_deployed is not None:
                self.packages[pkg].last_deployed = last_deployed

    def get_target_migrated_from(self, pkg: str, current_target: Path) -> Optional[Path]:
        """Returns the previous target directory if the package is migrating to a new destination, else None.

        If the package was previously deployed to a recorded target directory that differs from current_target,
        returns that previous directory. If the package has no previous record, has no recorded target directory,
        or the target directory has not changed, returns None.
        """
        pkg_data = self.packages.get(pkg)
        if pkg_data is None or pkg_data.target_directory is None:
            return None
        if pkg_data.target_directory != Path(current_target):
            return pkg_data.target_directory
        return None

    def build_destination_ownership_map(
        self,
        exclude_packages: Optional[Iterable[str]] = None,
    ) -> Dict[Path, str]:
        """Builds a mapping of absolute host destination paths to owning package names.

        Resolves each package's deployed_files relative to its recorded target_directory.
        Optionally excludes packages in `exclude_packages` (e.g. packages currently being redeployed).
        """
        exclude_set = set(exclude_packages) if exclude_packages is not None else set()
        ownership_map: Dict[Path, str] = {}
        for pkg, pkg_state in self.packages.items():
            if pkg in exclude_set:
                continue
            if pkg_state.state == "installed" and pkg_state.target_directory is not None:
                for rel_file in pkg_state.deployed_files:
                    dst = pkg_state.target_directory / encode_dot_prefix(rel_file)
                    ownership_map[dst] = pkg
        return ownership_map

    def get_file_owner(self, dst_path: Path) -> Optional[str]:
        """Returns the name of the installed package that owns the given host destination path, or None."""
        ownership_map = self.build_destination_ownership_map()
        return ownership_map.get(dst_path)

    def get_package_deployed_files(self, pkg: str) -> List[Path]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.deployed_files
        return []

    def sync_deployed_files(
        self,
        pkg: str,
        target_directory: Path,
        install_method: InstallMethod,
        deployable_files: Iterable[Path] = (),
        sudo: bool = False,
    ) -> None:
        """Updates the target directory, install method, and deployed_files manifest list for a package in the state registry.

        Args:
            pkg: Name of the package.
            target_directory: Target directory on host system.
            install_method: Method used to install package ('copy', 'symlink').
            deployable_files: Full iterable of deployable files in the package.
            sudo: Whether the package is installed with root escalation.

        Raises:
            KeyError: If pkg is not registered in the state registry.
        """
        if pkg not in self.packages:
            raise KeyError(f"Package '{pkg}' not found in state registry.")

        self.packages[pkg].target_directory = Path(target_directory)
        self.packages[pkg].install_method = install_method
        self.packages[pkg].deployed_files = sorted(Path(x) for x in deployable_files)
        self.packages[pkg].sudo = sudo

    def remove_package(self, pkg: str) -> None:
        if pkg in self.packages:
            del self.packages[pkg]

    def has_installing_package(self) -> bool:
        """Returns True if any package in the registry is in midway 'installing' state."""
        return any(pkg_state.state == "installing" for pkg_state in self.packages.values())

    def filter_by_states(
        self,
        states: Iterable[str],
        target_packages: Optional[Iterable[str]] = None,
    ) -> List[Tuple[str, str]]:
        """Filters packages matching any of the specified states.

        Args:
            states: An iterable of state names to filter by.
            target_packages: Optional subset of package names to check. If None, checks all packages in registry.

        Returns:
            A list of (package_name, state) tuples for packages matching the states.
        """
        states_set = set(states)
        target_names = target_packages if target_packages is not None else self.packages.keys()
        all_packages: List[Tuple[str, str]] = [
            (pkg, self.packages[pkg].state)
            for pkg in target_names
            if pkg in self.packages and self.packages[pkg].state is not None
        ]
        return list(filter(lambda item: item[1] in states_set, all_packages))

    def get_midway_packages(
        self,
        target_packages: Optional[Iterable[str]] = None,
    ) -> List[Tuple[str, str]]:
        """Finds packages currently in a midway transaction state ('staging' or 'installing').
        'staged' is not included because it is the state for installing packages.

        Args:
            target_packages: Optional subset of package names to check. If None, checks all packages in registry.

        Returns:
            A list of (package_name, state) tuples for packages in midway transaction states.
        """
        return self.filter_by_states(MIDWAY_TRANSACTION_STATES, target_packages=target_packages)

    def get_rollback_eligible_packages(
        self,
        target_packages: Optional[Iterable[str]] = None,
        install_base: Optional[Path] = None,
    ) -> List[Tuple[str, str]]:
        """Finds packages currently in an uncommitted state eligible for rollback.

        Eligible packages are those in 'staging', 'staged', or 'installing' states, or in
        'installed' state with uncommitted modifications in the install/ repository.

        Args:
            target_packages: Optional subset of package names to check. If None, checks all packages in registry.
            install_base: Optional path to the install/ repository. Defaults to state_file.parent if state_file is set.

        Returns:
            A list of (package_name, state) tuples for packages eligible for rollback.
        """
        resolved_install_base = install_base or (self.state_file.parent if self.state_file is not None else None)
        head_registry = (
            load_head_state_registry(resolved_install_base)
            if resolved_install_base is not None and resolved_install_base.is_dir()
            else None
        )
        target_names = target_packages if target_packages is not None else self.packages.keys()
        return [
            (pkg, self.packages[pkg].state)
            for pkg in target_names
            if pkg in self.packages
            and self.packages[pkg].state is not None
            and check_is_package_rollback_eligible(
                pkg=pkg,
                state_registry=self,
                install_base=resolved_install_base,
                head_registry=head_registry,
            )
        ]

    def is_package_in_midway_state(self, pkg: str) -> bool:
        """Checks if a specific package is currently in a midway transaction state."""
        state = self.get_package_state(pkg)
        return state in MIDWAY_TRANSACTION_STATES if state is not None else False


    def save(self) -> None:
        """Saves this state registry to disk using its associated state_file."""
        save_state_registry(self)


def parse_state_registry_content(content: str, state_file: Optional[Path] = None) -> StateRegistry:
    """Parses raw state.toml text content into a StateRegistry instance."""
    try:
        data = parse_toml(content)
        packages_dict = data.get("packages", {})

        packages = {}
        for pkg, v in packages_dict.items():
            if not isinstance(v, dict):
                packages[str(pkg)] = PackageState(state=str(v))
                continue
            state = str(v.get("state", ""))
            target_directory_raw = v.get("target_directory")
            target_directory = Path(str(target_directory_raw)) if target_directory_raw is not None else None
            last_deployed = v.get("last_deployed")
            if last_deployed is not None:
                last_deployed = str(last_deployed)
            install_method_raw = v.get("install_method")
            install_method: Optional[InstallMethod] = None
            if install_method_raw is not None:
                try:
                    install_method = InstallMethod.from_str(install_method_raw)
                except ValueError:
                    install_method = None
            deployed_files_raw = v.get("deployed_files")
            deployed_files = []
            if isinstance(deployed_files_raw, list):
                deployed_files = [Path(x) for x in deployed_files_raw]
            sudo = bool(v.get("sudo", False))
            packages[str(pkg)] = PackageState(
                state=state,
                target_directory=target_directory,
                last_deployed=last_deployed,
                install_method=install_method,
                deployed_files=deployed_files,
                sudo=sudo,
            )
        return StateRegistry(packages, state_file=state_file)
    except Exception:
        return StateRegistry({}, state_file=state_file)


def load_state_registry(filepath: Path) -> StateRegistry:
    """Loads state.toml from the given filepath. Returns empty registry if file doesn't exist."""
    if not filepath.exists():
        return StateRegistry({}, state_file=filepath)
    try:
        content = filepath.read_text(encoding="utf-8")
        return parse_state_registry_content(content, state_file=filepath)
    except Exception:
        return StateRegistry({}, state_file=filepath)


def save_state_registry(registry: StateRegistry) -> None:
    """Saves the state registry to its associated state_file in valid TOML format."""
    if registry.state_file is None:
        raise ValueError("No state_file path associated with StateRegistry to save.")
    filepath = registry.state_file
    filepath.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for pkg, pkg_state in sorted(registry.packages.items()):
        lines.append(f"[packages.{pkg}]")
        lines.append(f'state = "{pkg_state.state}"')
        if pkg_state.target_directory is not None:
            lines.append(f'target_directory = "{pkg_state.target_directory.as_posix()}"')
        if pkg_state.last_deployed is not None:
            lines.append(f'last_deployed = "{pkg_state.last_deployed}"')
        if pkg_state.install_method is not None:
            lines.append(f'install_method = "{pkg_state.install_method}"')
        if pkg_state.deployed_files:
            list_items = ", ".join(f'"{x.as_posix()}"' for x in pkg_state.deployed_files)
            lines.append(f'deployed_files = [{list_items}]')
        if pkg_state.sudo:
            lines.append("sudo = true")
        lines.append("")  # Empty line separator
    filepath.write_text("\n".join(lines), encoding="utf-8")


def load_head_state_registry(install_base: Path) -> StateRegistry:
    """Loads the committed HEAD version of state.toml from the install repository."""
    state_file = install_base / "state.toml"
    res = run_command(
        ["git", "-C", str(install_base), "show", "HEAD:state.toml"],
        text=True,
        check=False,
        suppress_output=True,
    )
    if res.returncode != 0 or not res.stdout:
        return StateRegistry({}, state_file=state_file)
    return parse_state_registry_content(res.stdout, state_file=state_file)


def check_is_package_uncommitted_in_install(
    pkg: str,
    state_registry: StateRegistry,
    install_base: Path,
    head_registry: Optional[StateRegistry] = None,
) -> bool:
    """Checks whether a package has uncommitted changes in install/<pkg> or in state.toml compared to HEAD."""
    if has_uncommitted_modifications(install_base, f"{pkg}/"):
        return True
    resolved_head = head_registry if head_registry is not None else load_head_state_registry(install_base)
    return state_registry.packages.get(pkg) != resolved_head.packages.get(pkg)


def check_is_package_rollback_eligible(
    pkg: str,
    state_registry: StateRegistry,
    install_base: Optional[Path] = None,
    head_registry: Optional[StateRegistry] = None,
) -> bool:
    """Checks whether a package is eligible for rollback without --force."""
    if install_base is not None and install_base.is_dir() and not (install_base / pkg).is_dir():
        return False
    state = state_registry.get_package_state(pkg)
    if state in ROLLBACK_ELIGIBLE_STATES:
        return True
    if state == "installed" and install_base is not None and install_base.is_dir():
        return check_is_package_uncommitted_in_install(
            pkg=pkg,
            state_registry=state_registry,
            install_base=install_base,
            head_registry=head_registry,
        )
    return False

