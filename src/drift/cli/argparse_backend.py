import os
import sys

from .actions import execute_render


def run_argparse_cli(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="drift: Decoupled Two-Stage Git-Backed Dotfiles Manager"
    )
    parser.add_argument(
        "-C", "--directory",
        help="Run as if drift was started in <directory> instead of current working directory"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # render subcommand
    render_parser = subparsers.add_parser(
        "render",
        help="Render templates of a package or all enabled packages"
    )
    render_parser.add_argument(
        "package",
        nargs="?",
        help="Optional package name to render specifically"
    )

    args = parser.parse_args(argv)

    # Resolve drift root path
    if args.directory:
        drift_root = os.path.abspath(args.directory)
    else:
        drift_root = os.getcwd()

    if args.command == "render":
        try:
            execute_render(drift_root, args.package)
            if args.package:
                print(f"✨ Successfully rendered package '{args.package}'!")
            else:
                print("✨ Successfully rendered all enabled packages!")
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
