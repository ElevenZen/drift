"""Git repository remote cloning and workspace bootstrapping engine."""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .workspace_init import init_drift_workspace
from .workspace_repair import repair_drift_workspace
from .constants import (
    CONFIG_DIR_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
)
from .result_models import CloneResult

logger = logging.getLogger(__name__)


def extract_repo_name_from_url(git_url: str) -> str:
    """Extracts a default directory name from a Git repository URL or path.

    Examples:
        'https://github.com/user/dotfiles.git' -> 'dotfiles'
        'git@github.com:user/dotfiles.git'     -> 'dotfiles'
        '../repos/my-configs.git/'            -> 'my-configs'
        '/home/user/my-dotfiles'              -> 'my-dotfiles'
    """
    clean_url = git_url.strip().rstrip("/")
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]

    # Handle SSH url format: git@github.com:user/repo
    if ":" in clean_url and not clean_url.startswith("http://") and not clean_url.startswith("https://") and not clean_url.startswith("file://"):
        # Split on the colon if it's an scp-style SSH url
        clean_url = clean_url.split(":")[-1]

    parsed = urlparse(clean_url)
    path_part = parsed.path if parsed.path else clean_url
    name = Path(path_part).name.strip()
    return name if name else "dotfiles"


def is_drift_repository(repo_path: Path) -> bool:
    """Determines whether a repository path is structured as a Drift workspace."""
    if not repo_path.exists() or not repo_path.is_dir():
        return False

    config_dir = repo_path / CONFIG_DIR_NAME
    config_file = config_dir / GLOBAL_CONFIG_FILE_NAME
    envst_config = config_dir / f"{GLOBAL_CONFIG_FILE_NAME.split('.')[0]}.envst.toml"
    root_config = repo_path / GLOBAL_CONFIG_FILE_NAME
    src_dir = repo_path / "src"

    if config_file.exists() or envst_config.exists() or root_config.exists() or src_dir.is_dir():
        return True

    return False


def clone_git_repository(
    git_url: str,
    target_dir: Path,
    branch: Optional[str] = None,
    depth: Optional[int] = None
) -> None:
    """Clones a remote or local Git repository to target_dir."""
    if target_dir.exists():
        if any(target_dir.iterdir()):
            raise FileExistsError(f"Target directory '{target_dir}' already exists and is not empty.")
    else:
        target_dir.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone"]
    if branch:
        cmd.extend(["--branch", branch])
    if depth is not None:
        cmd.extend(["--depth", str(depth)])

    cmd.extend([git_url, str(target_dir)])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        logger.debug(f"Git clone output: {res.stdout}")
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.strip() if e.stderr else str(e)
        logger.error(f"Git clone failed: {err_msg}")
        raise RuntimeError(f"Failed to clone git repository from '{git_url}': {err_msg}") from e


def convert_legacy_dotfiles_repo(target_dir: Path, pkg_name: str) -> List[str]:
    """Converts a cloned plain/legacy dotfiles repository into a modular Drift package."""
    actions: List[str] = []
    tmp_payload = target_dir / "_drift_tmp_payload"
    if tmp_payload.exists():
        shutil.rmtree(str(tmp_payload))
    tmp_payload.mkdir(parents=True, exist_ok=True)

    # 1. Move all cloned contents (excluding .git and _drift_tmp_payload) into tmp_payload
    for item in list(target_dir.iterdir()):
        if item.name in (".git", "_drift_tmp_payload"):
            continue
        shutil.move(str(item), str(tmp_payload / item.name))

    actions.append("Isolated existing dotfile contents into temporary payload buffer.")

    # 2. Run drift init in target_dir
    init_drift_workspace(target_dir, no_git_root=True)
    actions.append("Initialized Drift workspace infrastructure (config/, render/, install/, .gitignore).")

    # 3. Move payload to target_dir / src / pkg_name
    src_dir = target_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    dest_pkg_dir = src_dir / pkg_name
    if dest_pkg_dir.exists():
        shutil.rmtree(str(dest_pkg_dir))
    shutil.move(str(tmp_payload), str(dest_pkg_dir))
    actions.append(f"Migrated dotfile assets into package source directory 'src/{pkg_name}/'.")

    # 4. Generate drift_package.toml
    pkg_config_path = dest_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not pkg_config_path.exists():
        pkg_config_path.write_text(f"""[package]
name = "{pkg_name}"
install_method = "stow"
target_directory = "~"
""", encoding="utf-8")
        actions.append(f"Generated default package metadata 'src/{pkg_name}/{PACKAGE_CONFIG_FILE_NAME}'.")

    # 5. Generate .drift_ignore
    ignore_path = dest_pkg_dir / DRIFT_IGNORE_FILE_NAME
    if not ignore_path.exists():
        ignore_path.write_text("""# =====================================================================
# .drift_ignore - PCRE Regex Package Ignore Patterns
# =====================================================================
# Ignored non-configuration repository files during deployment
^/README.*$
^/LICENSE.*$
^/install.*\\.sh$
^/setup.*\\.sh$
^/bootstrap.*\\.sh$
^/\\.git.*$
""", encoding="utf-8")
        actions.append(f"Generated default ignore patterns 'src/{pkg_name}/{DRIFT_IGNORE_FILE_NAME}'.")

    # 6. Enable package in config/drift.toml
    config_file = target_dir / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    if config_file.exists():
        config_content = config_file.read_text(encoding="utf-8")
        if f"{pkg_name} =" not in config_content:
            if "[packages.enable]" in config_content:
                config_content = config_content.replace(
                    "[packages.enable]",
                    f"[packages.enable]\n{pkg_name} = true"
                )
            else:
                config_content += f"\n[packages.enable]\n{pkg_name} = true\n"
            config_file.write_text(config_content, encoding="utf-8")
            actions.append(f"Enabled package '{pkg_name}' in '{CONFIG_DIR_NAME}/{GLOBAL_CONFIG_FILE_NAME}'.")

    return actions


def run_primitive_clone(
    git_url: str,
    target_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    depth: Optional[int] = None,
    no_repair: bool = False
) -> CloneResult:
    """Clones a repository, inspects workspace structure, and bootstraps or heals state databases."""
    if target_dir is None:
        repo_name = extract_repo_name_from_url(git_url)
        target_dir = Path.cwd() / repo_name
    else:
        target_dir = Path(target_dir).resolve()
        repo_name = target_dir.name

    try:
        clone_git_repository(
            git_url=git_url,
            target_dir=target_dir,
            branch=branch,
            depth=depth
        )
    except Exception as e:
        return CloneResult(
            command="clone",
            status="FAILED",
            git_url=git_url,
            target_directory=str(target_dir),
            error_message=str(e)
        )

    is_drift = is_drift_repository(target_dir)
    repaired_actions: List[str] = []
    converted_pkg: Optional[str] = None
    next_steps: List[str] = []

    if is_drift:
        if not no_repair:
            repaired_actions = repair_drift_workspace(target_dir)
        next_steps = [
            f"cd {target_dir.name}",
            "Adjust local settings in 'config/drift.local.toml' (this overrides 'config/drift.toml'; see 'drift help drift.toml')",
            "Define machine-specific environment variables and secrets in 'config/secrets.env' (see 'drift help workspace')",
            "Run 'drift deploy' (or 'drift status' / 'drift diff' to preview)"
        ]
    else:
        converted_pkg = repo_name
        repaired_actions = convert_legacy_dotfiles_repo(target_dir, converted_pkg)
        next_steps = [
            f"cd {target_dir.name}",
            f"Review converted package configuration in 'src/{converted_pkg}/{PACKAGE_CONFIG_FILE_NAME}' (verify target_directory = \"~\" and install_method = \"stow\" or \"copy\"; see 'drift help drift_package.toml')",
            f"Review 'src/{converted_pkg}/.drift_ignore' (exclude files like README/scripts from deployment; see 'drift help ignore')",
            "Adjust local settings in 'config/drift.local.toml' (this overrides 'config/drift.toml'; see 'drift help drift.toml')",
            "Define machine-specific secrets and environment variables in 'config/secrets.env' (see 'drift help workspace')",
            "Run 'drift diff' or 'drift status' to preview, then run 'drift deploy'"
        ]

    return CloneResult(
        command="clone",
        status="SUCCESS",
        git_url=git_url,
        target_directory=str(target_dir),
        is_drift_workspace=is_drift,
        converted_legacy_package=converted_pkg,
        repaired_actions=repaired_actions,
        recommended_next_steps=next_steps,
        recommended_next_command=f"cd {target_dir.name} && drift deploy"
    )
