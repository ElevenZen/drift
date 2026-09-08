"""State registry subsystem using pathlib."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, List
from .toml_utils import parse_toml


@dataclass
class PackageState:
    """Represents the recorded state of a single package."""
    state: str
    last_deployed: Optional[str] = None
    install_method: Optional[str] = None
    deployed_files: List[Path] = field(default_factory=list)


class StateRegistry:
    """Manages reading, updating, and saving install/state.toml with timestamps and metadata."""

    def __init__(self, packages: Dict[str, PackageState], state_file: Optional[Path] = None):
        self.packages = packages
        self.state_file = state_file

    def get_package_state(self, pkg: str) -> Optional[str]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.state
        return None

    def set_package_state(
        self,
        pkg: str,
        state: str,
        last_deployed: Optional[str] = None,
        install_method: Optional[str] = None
    ) -> None:
        if pkg not in self.packages:
            self.packages[pkg] = PackageState(state=state)
        else:
            self.packages[pkg].state = state
        
        if last_deployed is not None:
            self.packages[pkg].last_deployed = last_deployed
        if install_method is not None:
            self.packages[pkg].install_method = install_method

    def get_package_deployed_files(self, pkg: str) -> List[Path]:
        pkg_data = self.packages.get(pkg)
        if pkg_data:
            return pkg_data.deployed_files
        return []

    def set_package_deployed_files(self, pkg: str, files: List[Path]) -> None:
        if pkg not in self.packages:
            self.packages[pkg] = PackageState(state="unknown")
        self.packages[pkg].deployed_files = [Path(x) for x in files]

    def remove_package(self, pkg: str) -> None:
        if pkg in self.packages:
            del self.packages[pkg]

    def has_deploying_package(self) -> bool:
        for pkg_state in self.packages.values():
            if pkg_state.state == "deploying":
                return True
        return False

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
            last_deployed = v.get("last_deployed")
            if last_deployed is not None:
                last_deployed = str(last_deployed)
            install_method = v.get("install_method")
            if install_method is not None:
                install_method = str(install_method)
            deployed_files_raw = v.get("deployed_files")
            deployed_files = []
            if isinstance(deployed_files_raw, list):
                deployed_files = [Path(x) for x in deployed_files_raw]
            packages[str(pkg)] = PackageState(
                state=state,
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
        if pkg_state.last_deployed is not None:
            lines.append(f'last_deployed = "{pkg_state.last_deployed}"')
        if pkg_state.install_method is not None:
            lines.append(f'install_method = "{pkg_state.install_method}"')
        if pkg_state.deployed_files:
            list_items = ", ".join(f'"{x}"' for x in pkg_state.deployed_files)
            lines.append(f'deployed_files = [{list_items}]')
        lines.append("")  # Empty line separator
    filepath.write_text("\n".join(lines), encoding="utf-8")
