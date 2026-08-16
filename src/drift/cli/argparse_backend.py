import os
import sys

from .actions import get_drift_root, execute_render, execute_init


def run_argparse_cli(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="drift: Decoupled Two-Stage Git-Backed Dotfiles Manager"
    )
    parser.add_argument(
        "-C", "--directory",
        help="Run as if drift was started in <directory> instead of current working directory"
    )
    parser.add_argument(
        "--no-git-root",
        action="store_true",
        help="Stop resolving git root of cwd or -C directory, using the literal path instead"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # init subcommand
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new drift workspace"
    )
    init_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-initialization and overwrite existing files"
    )

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

    # Resolve literal base directory path
    base_dir = os.path.abspath(args.directory) if args.directory else os.getcwd()

    if args.command == "init":
        # Bypassing show-toplevel check for init, using raw directory/cwd as root
        drift_root = base_dir
        try:
            execute_init(drift_root, force=args.force, no_git_root=args.no_git_root)
            print("✨ Initialized drift workspace!")
            print("📁 Created render/ sandbox Git database.")
            print("📁 Created install/ local state Git database.")
            print("📝 Generated drift.toml template.")
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "render":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

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
