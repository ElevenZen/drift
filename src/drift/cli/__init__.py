import sys
import logging
import os
import getpass

from .argparse_backend import run_argparse_cli

# Try importing Typer to check availability
try:
    import typer
    HAS_TYPER = True
except ImportError:
    HAS_TYPER = False


def check_sudo_and_root() -> None:
    """Ensures that the user is not running the program under sudo (which pollutes expand_home),
    and check that the user does not have root privilege unless they are actually the root user.
    """
    if sys.platform == "win32":
        # On Windows, elevated Administrator shell is allowed and sudo/root checks do not apply.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        return

    # 1. Check if running under sudo (SUDO_USER or SUDO_UID environment variables exist)
    is_sudo = "SUDO_USER" in os.environ or "SUDO_UID" in os.environ

    # 2. Check if running with root privilege (UID 0)
    has_root_privilege = False
    try:
        has_root_privilege = (os.getuid() == 0)
    except AttributeError:
        # Non-POSIX platforms
        pass

    if is_sudo:
        print("❌ [ERROR] Running under 'sudo' is strictly prohibited as it pollutes configuration paths and home expansions.", file=sys.stderr)
        sys.exit(1)

    if has_root_privilege:
        try:
            current_user = getpass.getuser()
        except Exception:
            current_user = os.environ.get("USER", "root")
        if current_user != "root":
            print("❌ [ERROR] Running with root privilege is prohibited unless you are the actual 'root' user.", file=sys.stderr)
            sys.exit(1)


def setup_logging(level: int = logging.INFO) -> None:
    """Sets up a beautiful, polished logging format across the application.
    
    Can be called multiple times to re-configure the level.
    """
    # Clear existing handlers to allow re-configuration
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    try:
        from rich.logging import RichHandler
        # We configure rich handler for high-signal formatted output (markup=False prevents swallowing [brackets])
        handler = RichHandler(rich_tracebacks=True, markup=False, show_path=False)
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[handler]
        )
    except ImportError:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            stream=sys.stderr
        )


def main(argv=None) -> None:
    from ..constants import update_initial_env
    update_initial_env()
    check_sudo_and_root()
    setup_logging()
    if HAS_TYPER:
        from .typer_backend import app
        args_list = argv if argv is not None else sys.argv[1:]
        try:
            app(args=args_list)
        except SystemExit as e:
            if e.code == 0 and not any(h in args_list for h in ("--help", "-h")):
                return
            raise
    else:
        run_argparse_cli(argv)
