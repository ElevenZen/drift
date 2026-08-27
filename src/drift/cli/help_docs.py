"""Structured help documentation content loading and paging print helper for the drift help commands."""

import sys
from pathlib import Path
from typing import Optional
import pydoc


def get_help_page(topic: Optional[str]) -> str:
    """Returns the Markdown content matching the requested help topic dynamically from markdown files."""
    help_dir = Path(__file__).resolve().parent / "help_docs"
    
    if not topic:
        topic_file_name = "overall"
    else:
        topic_lower = topic.lower().strip()
        if topic_lower == "package":
            topic_file_name = "package"
        elif topic_lower == "src":
            topic_file_name = "src"
        elif topic_lower == "render":
            topic_file_name = "render"
        elif topic_lower == "install":
            topic_file_name = "install"
        elif topic_lower == "fcd":
            topic_file_name = "fcd"
        elif topic_lower == "ignore":
            topic_file_name = "ignore"
        elif topic_lower == "drift_package.toml":
            topic_file_name = "drift_package_toml"
        elif topic_lower == "drift.toml":
            topic_file_name = "drift_toml"
        elif topic_lower == "workspace":
            topic_file_name = "workspace"
        elif topic_lower == "health":
            topic_file_name = "health"
        elif topic_lower == "clone":
            topic_file_name = "clone"
        elif topic_lower == "faq":
            topic_file_name = "faq"
        else:
            raise ValueError(
                f"Unknown help topic: '{topic}'.\n"
                "Available topics are: 'package', 'src', 'render', 'install', 'fcd', 'ignore', 'drift_package.toml', 'drift.toml', 'workspace', 'health', 'clone', 'faq'."
            )

    # Try pkgutil first (supports zipapp and installed packages)
    try:
        import pkgutil
        data = pkgutil.get_data("drift.cli", f"help_docs/{topic_file_name}.md")
        if data:
            return data.decode("utf-8")
    except Exception:
        pass

    help_dir = Path(__file__).resolve().parent / "help_docs"
    md_file_path = help_dir / f"{topic_file_name}.md"
    if not md_file_path.exists():
        raise FileNotFoundError(f"Help file not found at: {md_file_path}")

    return md_file_path.read_text(encoding="utf-8")


def print_help_document(topic: Optional[str] = None) -> None:
    """Retrieves and prints the requested help documentation with pager fallback support."""
    try:
        content = get_help_page(topic)
    except Exception as e:
        print(f"❌ [ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if sys.stdout.isatty():
        pydoc.pager(content)
    else:
        print(content)
