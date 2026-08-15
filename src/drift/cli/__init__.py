import sys

from .argparse_backend import run_argparse_cli

# Try importing Typer to check availability
try:
    import typer
    HAS_TYPER = True
except ImportError:
    HAS_TYPER = False


def main(argv=None) -> None:
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
