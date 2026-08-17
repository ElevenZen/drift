"""State registry subsystem using pathlib."""

from pathlib import Path
from typing import Dict, Optional
from .toml_parser import parse_toml


class StateRegistry:
    """Manages reading, updating, and saving install/state.toml with timestamps and metadata."""

    def __init__(self, packages: Dict[str, dict]):
        self.packages = packages

    def get_package_state(self, pkg: str) -> Optional[str]:
        pkg_data = self.packages.get(pkg)
        if isinstance(pkg_data, dict):
            return pkg_data.get("state")
        return pkg_data  # fallback for simple string values

    def set_package_state(
        self,
        pkg: str,
        state: str,
        last_deployed: Optional[str] = None,
        install_method: Optional[str] = None
    ) -> None:
        if pkg not in self.packages or not isinstance(self.packages[pkg], dict):
            self.packages[pkg] = {}
        self.packages[pkg]["state"] = state
        if last_deployed:
            self.packages[pkg]["last_deployed"] = last_deployed
        if install_method:
            self.packages[pkg]["install_method"] = install_method

    def remove_package(self, pkg: str) -> None:
        if pkg in self.packages:
            del self.packages[pkg]

    def has_deploying_package(self) -> bool:
        for pkg_data in self.packages.values():
            if isinstance(pkg_data, dict) and pkg_data.get("state") == "deploying":
                return True
            elif pkg_data == "deploying":
                return True
        return False


def load_state_registry(filepath: Path) -> StateRegistry:
    """Loads state.toml from the given filepath. Returns empty registry if file doesn't exist."""
    if not filepath.exists():
        return StateRegistry({})
    try:
        content = filepath.read_text(encoding="utf-8")
        data = parse_toml(content)
        packages = data.get("packages", {})
        
        cleaned_packages = {}
        for k, v in packages.items():
            if isinstance(v, dict):
                cleaned_packages[str(k)] = {str(sub_k): str(sub_v) for sub_k, sub_v in v.items()}
            else:
                cleaned_packages[str(k)] = {"state": str(v)}
        return StateRegistry(cleaned_packages)
    except Exception:
        return StateRegistry({})


def save_state_registry(filepath: Path, registry: StateRegistry) -> None:
    """Saves the state registry to the given filepath in valid TOML format."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for pkg, data in sorted(registry.packages.items()):
        lines.append(f"[packages.{pkg}]")
        if isinstance(data, dict):
            for k, v in sorted(data.items()):
                lines.append(f'{k} = "{v}"')
        else:
            lines.append(f'state = "{data}"')
        lines.append("")  # Empty line separator
    filepath.write_text("\n".join(lines), encoding="utf-8")
