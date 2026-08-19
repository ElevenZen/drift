import sys
import logging

from .argparse_backend import run_argparse_cli

# Try importing Typer to check availability
try:
    import typer
    HAS_TYPER = True
except ImportError:
    HAS_TYPER = False


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
        # We configure rich handler for high-signal formatted output
        handler = RichHandler(rich_tracebacks=True, markup=True, show_path=False)
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
