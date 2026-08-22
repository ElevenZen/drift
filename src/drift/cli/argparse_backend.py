import argparse
import sys
from pathlib import Path

from .actions import (
    get_drift_root,
    execute_render,
    execute_init,
    execute_stage,
    execute_render_commit,
    execute_apply,
    execute_install_commit,
    execute_reverse_sync,
    execute_new_package,
    execute_uninstall,
    execute_status,
    execute_gc,
    execute_diff,
    execute_add,
    execute_adopt,
    execute_rollback,
    execute_deploy,
    execute_help
)

def make_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging output"
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
        help="(Low-Level) Render templates of a package or all enabled packages"
    )
    render_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to render specifically"
    )

    # stage subcommand
    stage_parser = subparsers.add_parser(
        "stage",
        help="(Low-Level) Stage compiled sandbox templates from render/ to install/ state database"
    )
    stage_parser.add_argument(
        "packages",
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
        help="(Low-Level) Apply configurations from state database to active host system"
    )
    apply_parser.add_argument(
        "packages",
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
        help="(Low-Level) Stage and commit compiled render sandbox changes"
    )
    render_commit_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to commit specifically"
    )
    render_commit_parser.add_argument(
        "-m", "--message",
        required=True,
        help="Commit message"
    )

    # install-commit subcommand
    install_commit_parser = subparsers.add_parser(
        "install-commit",
        help="(Low-Level) Stage and commit install state directory changes"
    )
    install_commit_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to commit specifically"
    )
    install_commit_parser.add_argument(
        "-m", "--message",
        required=True,
        help="Commit message"
    )

    # reverse-sync subcommand
    reverse_sync_parser = subparsers.add_parser(
        "reverse-sync",
        help="(Low-Level) Synchronize changes from host system back to install/ state database"
    )
    reverse_sync_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to reverse-sync specifically"
    )

    # new subcommand
    new_parser = subparsers.add_parser(
        "new",
        help="Scaffold a new package directory and drift_package.toml"
    )
    new_parser.add_argument(
        "package_name",
        help="Name of the new package to create"
    )
    new_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Forcefully overwrite any existing config file inside the package"
    )
    new_parser.add_argument(
        "-t", "--target",
        dest="target",
        help="Explicitly configure the deployment target directory inside drift_package.toml"
    )
    new_parser.add_argument(
        "-m", "--method",
        dest="method",
        help="Explicitly configure the installation method ('stow' or 'copy') inside drift_package.toml"
    )

    # uninstall subcommand
    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Uninstall a package from the system and restore any backups"
    )
    uninstall_parser.add_argument(
        "packages",
        nargs="+",
        help="One or more package names to uninstall"
    )
    uninstall_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force uninstallation even if package is still active in drift.toml"
    )
    uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview uninstallation without making changes"
    )
    uninstall_parser.add_argument(
        "--detach",
        action="store_true",
        help="Remove management relationship but keep configurations as actual physical files on host system"
    )

    # adopt subcommand
    adopt_parser = subparsers.add_parser(
        "adopt",
        help="Adopt active system drifts and incorporate them back into source templates"
    )
    adopt_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to adopt specifically"
    )
    adopt_parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Interactively reconcile each drifted file"
    )
    adopt_parser.add_argument(
        "--accept-conflicts",
        action="store_true",
        help="Apply conflicting patches, writing merge conflict markers directly into templates"
    )
    adopt_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force adoption even if the package source directory has uncommitted modifications"
    )
    adopt_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the adoption, previewing changes and conflict results"
    )

    # status subcommand
    status_parser = subparsers.add_parser(
        "status",
        help="Audit and aggregate configuration status across active packages"
    )
    status_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to audit specifically"
    )

    # gc subcommand
    gc_parser = subparsers.add_parser(
        "gc",
        help="(Low-Level) Identify and uninstall orphan packages (present in state but disabled in config)"
    )
    gc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the garbage collection without making changes"
    )

    # diff subcommand
    diff_parser = subparsers.add_parser(
        "diff",
        help="Visualize changes between configuration layers (Default: Pending Delta / Diff Δ)"
    )
    diff_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to diff specifically"
    )
    diff_group = diff_parser.add_mutually_exclusive_group()
    diff_group.add_argument(
        "-t", "--template",
        action="store_true",
        help="Visualize Template Evolution (Diff A: src -> render)"
    )
    diff_group.add_argument(
        "-s", "--system",
        action="store_true",
        help="Visualize System Drift (Diff B: System -> install)"
    )
    diff_parser.add_argument(
        "-y", "--side-by-side",
        action="store_true",
        help="Show side-by-side comparison (Note: relies on git/diff capabilities)"
    )
    diff_parser.add_argument(
        "--stat",
        action="store_true",
        help="Show concise summary of changes (diffstat)"
    )

    # add subcommand
    add_parser = subparsers.add_parser(
        "add",
        help="Import files or folders from the system into a package (with dot-prefix translation)"
    )
    add_parser.add_argument(
        "package_name",
        help="Name of the package to add resources into"
    )
    add_parser.add_argument(
        "paths",
        nargs="+",
        help="One or more file/folder paths to import"
    )
    add_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the import without making changes"
    )

    # rollback subcommand
    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Rollback failed deployments and restore systems to the last committed clean state."
    )
    rollback_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional package name(s) to rollback specifically"
    )
    rollback_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force the rollback and skip failed interlock checking"
    )

    # deploy subcommand
    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Sandbox-compiles, stages, and deploys declarative configuration templates to target hosts."
    )
    deploy_parser.add_argument(
        "packages",
        nargs="*",
        help="Optional specific package(s) to deploy"
    )
    deploy_parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Forcefully deploy and bypass system drift sentinel safeguards"
    )

    # help subcommand
    help_parser = subparsers.add_parser(
        "help",
        help="Show overall model of drift and its detailed manual pages."
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        help="Specific topic to display (package, src, render, install, drift_package.toml, drift.toml)"
    )
    return parser


def run_argparse_cli(argv=None) -> None:
    parser = make_parser()
    args = parser.parse_args(argv)

    if args.verbose:
        from . import setup_logging
        import logging
        setup_logging(level=logging.DEBUG)

    # Resolve literal base directory path
    base_dir = Path(args.directory).resolve() if args.directory else Path.cwd().resolve()

    if args.command == "init":
        # Bypassing show-toplevel check for init, using raw directory/cwd as root
        drift_root = base_dir
        try:
            execute_init(drift_root, force=args.force, no_git_root=args.no_git_root)
            print("✨ Initialized drift workspace!")
            print("📁 Created render/ sandbox Git database.")
            print("📁 Created install/ local state Git database.")
            print("📝 Generated drift.toml template.")
            print("📝 Generated config/envsubst.bash and config/mustache.envst.json.")
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "render":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_render(drift_root, args.packages)
            if args.packages:
                print(f"✨ Successfully rendered package(s) '{', '.join(args.packages)}'!")
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
            execute_stage(drift_root, args.packages, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "apply":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_apply(drift_root, args.packages, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "render-commit":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_render_commit(drift_root, args.message, args.packages)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "install-commit":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_install_commit(drift_root, args.message, args.packages)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "reverse-sync":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_reverse_sync(drift_root, args.packages)
            if args.packages:
                print(f"✨ Successfully reverse-synced package(s) '{', '.join(args.packages)}'!")
            else:
                print("✨ Successfully reverse-synced all enabled packages!")
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "new":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_new_package(
                drift_root,
                args.package_name,
                force=args.force,
                target_directory=args.target,
                install_method=args.method
            )
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "uninstall":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_uninstall(drift_root, args.packages, force=args.force, dry_run=args.dry_run, detach=args.detach)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "adopt":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_adopt(
                drift_root=drift_root,
                package_names=args.packages,
                interactive=args.interactive,
                accept_conflicts=args.accept_conflicts,
                force=args.force,
                dry_run=args.dry_run
            )
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "status":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_status(drift_root, args.packages)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "gc":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_gc(drift_root, dry_run=args.dry_run)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "diff":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        diff_type = "pending"
        if args.template:
            diff_type = "template"
        elif args.system:
            diff_type = "system"

        try:
            execute_diff(drift_root, args.packages, diff_type=diff_type, side_by_side=args.side_by_side, stat=args.stat)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "add":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_add(drift_root, args.package_name, args.paths, dry_run=args.dry_run)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "rollback":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_rollback(drift_root, args.packages, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "deploy":
        if args.no_git_root:
            drift_root = base_dir
        else:
            drift_root = get_drift_root(base_dir)

        try:
            execute_deploy(drift_root, args.packages, force=args.force)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "help":
        try:
            execute_help(args.topic)
        except Exception as e:
            print(f"❌ [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
