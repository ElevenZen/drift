import os
import sys

from .actions import get_drift_root, execute_render, execute_init, execute_stage, execute_render_commit, execute_apply


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

    # stage subcommand
    stage_parser = subparsers.add_parser(
        "stage",
        help="Stage compiled sandbox templates from render/ to install/ state database"
    )
    stage_parser.add_argument(
        "package",
        nargs="*",
        help="Optional package name(s) to stage specifically"
    )
    stage_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force staging and bypass uncommitted modifications check"
    )

    # apply subcommand
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply configurations from state database to active host system"
    )
    apply_parser.add_argument(
        "package",
        nargs="*",
        help="Optional package name(s) to apply specifically"
    )
    apply_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force deployment and bypass check"
    )

    # render-commit subcommand
    render_commit_parser = subparsers.add_parser(
        "render-commit",
        help="Stage and commit compiled render sandbox changes"
    )
    render_commit_parser.add_argument(
        "package",
        nargs="?",
        help="Optional package name to commit specifically"
    )
    render_commit_parser.add_argument(
        "-m", "--message",
        required=True,
        help="Commit message"
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
    elif args.command == "stage":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_stage(drift_root, args.package, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "apply":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_apply(drift_root, args.package, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "render-commit":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_render_commit(drift_root, args.message, args.package)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
