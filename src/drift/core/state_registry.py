"""State registry subsystem using pathlib."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, List, Mapping, Tuple, Iterable, Any
from ..utils.toml_utils import parse_toml
from .exceptions import ConfigError
from .constants import MIDWAY_TRANSACTION_STATES, InstallMethod



@dataclass
class PackageState:
    """Represents the recorded state of a single package."""
    state: str
    target_directory: Optional[Path] = None
    last_deployed: Optional[str] = None
    install_method: Optional[InstallMethod] = None
    deployed_files: List[Path] = field(default_factory=list)


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
        from ..utils.path_utils import resolve_target_path
        exclude_set = set(exclude_packages) if exclude_packages is not None else set()
        ownership_map: Dict[Path, str] = {}
        for pkg, pkg_state in self.packages.items():
            if pkg in exclude_set:
                continue
            if pkg_state.state == "installed" and pkg_state.target_directory is not None:
                for rel_file in pkg_state.deployed_files:
                    dst = resolve_target_path(rel_file, pkg_state.target_directory)
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
        redeploy: bool = False,
        deployable_files: Iterable[Path] = (),
        package_changes: Optional[Any] = None,
    ) -> None:
        """Updates the target directory, install method, and deployed_files manifest list for a package in the state registry.

        Args:
            pkg: Name of the package.
            target_directory: Target directory on host system.
            install_method: Method used to install package ('copy', 'stow').
            redeploy: If True (or if package_changes is None), overwrites the manifest with deployable_files.
            deployable_files: Full iterable of deployable files in the package (used during redeploy/full deploy).
            package_changes: Incremental stage changes containing added and deleted file lists.

        Raises:
            KeyError: If pkg is not registered in the state registry.
        """
        if pkg not in self.packages:
            raise KeyError(f"Package '{pkg}' not found in state registry.")

        self.packages[pkg].target_directory = Path(target_directory)
        self.packages[pkg].install_method = install_method

        if redeploy or package_changes is None:
            self.packages[pkg].deployed_files = [Path(x) for x in deployable_files]
        else:
            current_deployed = set(self.packages[pkg].deployed_files)
            updated_deployed = (current_deployed - set(package_changes.deployable_changes.deleted)) | set(package_changes.deployable_changes.added)
            self.packages[pkg].deployed_files = sorted(Path(x) for x in updated_deployed)

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

        Args:
            target_packages: Optional subset of package names to check. If None, checks all packages in registry.

        Returns:
            A list of (package_name, state) tuples for packages in midway transaction states.
        """
        return self.filter_by_states(MIDWAY_TRANSACTION_STATES, target_packages=target_packages)

    def is_package_in_midway_state(self, pkg: str) -> bool:
        """Checks if a specific package is currently in a midway transaction state."""
        state = self.get_package_state(pkg)
        return state in MIDWAY_TRANSACTION_STATES if state is not None else False


    def save(self) -> None:
        """Saves this state registry to disk using its associated state_file."""
        save_state_registry(self)


def load_state_registry(filepath: Path) -> StateRegistry:
    """Loads state.toml from the given filepath. Returns empty registry if file doesn't exist."""
    if not filepath.exists():
        return StateRegistry({}, state_file=filepath)
    try:
        content = filepath.read_text(encoding="utf-8")
        data = parse_toml(content)
        packages_dict = data.get("packages", {})
        
        packages = {}
        for pkg, v in packages_dict.items():
            # process package entry in state.toml file.
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
            packages[str(pkg)] = PackageState(
                state=state,
                target_directory=target_directory,
                last_deployed=last_deployed,
                install_method=install_method,
                deployed_files=deployed_files
            )
        return StateRegistry(packages, state_file=filepath)
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
            target_str = str(pkg_state.target_directory).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'target_directory = "{target_str}"')
        if pkg_state.last_deployed is not None:
            lines.append(f'last_deployed = "{pkg_state.last_deployed}"')
        if pkg_state.install_method is not None:
            lines.append(f'install_method = "{pkg_state.install_method}"')
        if pkg_state.deployed_files:
            list_items = ", ".join(f'"{x}"' for x in pkg_state.deployed_files)
            lines.append(f'deployed_files = [{list_items}]')
        lines.append("")  # Empty line separator
    filepath.write_text("\n".join(lines), encoding="utf-8")
