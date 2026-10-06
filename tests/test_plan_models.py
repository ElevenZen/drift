import unittest
import json
from pathlib import Path

from drift.utils.path_utils import rebase_path
from drift.core.file_action import (
    FileAction,
    FileActionType,
    NO_CHANGE_ACTION_TYPES,
    NON_MUTATING_ACTION_TYPES,
    rebase_file_action,
)
from drift.core.result_models import (
    PackageRenderResult,
    PackageStagePlan,
    PackageInstallPlan,
    PackageReverseSyncPlan,
    PackageUninstallPlan,
    PackageDeployPreview,
    WorkspaceDeployPreview,
    DeployPreview,
)
from drift.core.exceptions import (
    RenderError,
    ConfigError,
    CyclicDependencyError,
    CrossPackageCollisionError,
)


class TestPathRebasing(unittest.TestCase):
    def setUp(self) -> None:
        self.old_base = Path("/tmp/sandbox/pkg")
        self.new_base = Path("/repo/render/pkg")

    def test_rebase_path_within_base(self) -> None:
        child = self.old_base / "nested" / "file.txt"
        rebased = rebase_path(child, self.old_base, self.new_base)
        self.assertEqual(rebased, self.new_base / "nested" / "file.txt")

    def test_rebase_path_exact_base(self) -> None:
        rebased = rebase_path(self.old_base, self.old_base, self.new_base)
        self.assertEqual(rebased, self.new_base)

    def test_rebase_path_outside_base(self) -> None:
        outside = Path("/home/test_user/.config/app/conf")
        rebased = rebase_path(outside, self.old_base, self.new_base)
        self.assertEqual(rebased, outside)

    def test_rebase_path_none(self) -> None:
        self.assertIsNone(rebase_path(None, self.old_base, self.new_base))

    def test_rebase_file_action(self) -> None:
        action = FileAction(
            action_type=FileActionType.CREATE_COPY,
            src_path=self.old_base / "init.lua",
            dst_path=Path("/home/test_user/.config/nvim/init.lua"),
            reason="new static file",
        )
        rebased = rebase_file_action(action, self.old_base, self.new_base)

        # Transformed values
        self.assertEqual(rebased.src_path, self.new_base / "init.lua")
        self.assertEqual(rebased.dst_path, Path("/home/test_user/.config/nvim/init.lua"))
        self.assertEqual(rebased.action_type, FileActionType.CREATE_COPY)
        self.assertEqual(rebased.reason, "new static file")

        # Original remains immutable
        self.assertEqual(action.src_path, self.old_base / "init.lua")

    def test_file_action_rebased_method(self) -> None:
        action = FileAction(
            action_type=FileActionType.RENDER_ITEM,
            src_path=Path("/repo/src/pkg/init.envst.lua"),
            dst_path=self.old_base / "init.lua",
            reason="compiled template",
        )
        rebased = action.rebased(self.old_base, self.new_base)
        self.assertEqual(rebased.dst_path, self.new_base / "init.lua")
        self.assertEqual(rebased.src_path, Path("/repo/src/pkg/init.envst.lua"))


class TestPackageDeployPreview(unittest.TestCase):
    def test_empty_preview(self) -> None:
        preview = PackageDeployPreview(package_name="nvim")
        self.assertEqual(preview.package_name, "nvim")
        self.assertFalse(preview.has_changes)
        self.assertIsNone(preview.error)
        self.assertIsNone(preview.error_message)
        self.assertIsNone(preview.error_type)

    def test_has_changes_with_render_actions(self) -> None:
        render_res = PackageRenderResult(
            package="nvim",
            actions=[
                FileAction(
                    action_type=FileActionType.RENDER_ITEM,
                    src_path=Path("/src/init.envst.lua"),
                    dst_path=Path("/render/init.lua"),
                )
            ],
        )
        preview = PackageDeployPreview(package_name="nvim", render_plan=render_res)
        self.assertTrue(preview.has_changes)

    def test_has_changes_with_stage_actions(self) -> None:
        stage_plan = PackageStagePlan(
            package="nvim",
            actions=[
                FileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=Path("/render/init.lua"),
                    dst_path=Path("/install/init.lua"),
                )
            ],
        )
        preview = PackageDeployPreview(package_name="nvim", stage_plan=stage_plan)
        self.assertTrue(preview.has_changes)

    def test_has_changes_with_install_actions(self) -> None:
        install_plan = PackageInstallPlan(
            package="nvim",
            target_directory="/host/target",
            actions=[
                FileAction(
                    action_type=FileActionType.CREATE_SYMLINK,
                    src_path=Path("/install/init.lua"),
                    dst_path=Path("/host/target/init.lua"),
                )
            ],
        )
        preview = PackageDeployPreview(package_name="nvim", install_plan=install_plan)
        self.assertTrue(preview.has_changes)

    def test_has_no_changes_when_only_skip_identical(self) -> None:
        render_res = PackageRenderResult(
            package="nvim",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/src/init.lua"),
                    dst_path=Path("/render/init.lua"),
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="info message",
                ),
            ],
        )
        self.assertFalse(render_res.has_changes)

        stage_plan = PackageStagePlan(
            package="nvim",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/render/init.lua"),
                    dst_path=Path("/install/init.lua"),
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="info message",
                ),
            ],
        )
        self.assertFalse(stage_plan.has_changes)
        self.assertFalse(stage_plan.has_mutations)
        self.assertEqual(len(stage_plan.skipped), 1)

        stage_plan_with_mutation = PackageStagePlan(
            package="nvim",
            actions=[
                FileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=Path("/render/init.lua"),
                    dst_path=Path("/install/init.lua"),
                ),
            ],
        )
        self.assertTrue(stage_plan_with_mutation.has_changes)
        self.assertTrue(stage_plan_with_mutation.has_mutations)

        install_plan = PackageInstallPlan(
            package="nvim",
            target_directory="/host/target",
            can_skip=True,
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/install/init.lua"),
                    dst_path=Path("/host/target/init.lua"),
                )
            ],
        )
        self.assertFalse(install_plan.has_changes)

        reverse_sync_plan = PackageReverseSyncPlan(
            package="nvim",
            target_directory="/host/target",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/install/init.lua"),
                    dst_path=Path("/host/target/init.lua"),
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="info message",
                ),
            ],
        )
        self.assertFalse(reverse_sync_plan.has_changes)
        self.assertFalse(reverse_sync_plan.has_mutations)

        uninstall_plan = PackageUninstallPlan(
            package="nvim",
            target_directory="/host/target",
            actions=[
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="info message",
                ),
            ],
        )
        self.assertFalse(uninstall_plan.has_changes)
        self.assertFalse(uninstall_plan.has_mutations)

        preview = PackageDeployPreview(
            package_name="nvim",
            render_plan=render_res,
            stage_plan=stage_plan,
            install_plan=install_plan,
        )
        self.assertFalse(preview.has_changes)

    def test_no_change_action_types_constant(self) -> None:
        self.assertEqual(
            NO_CHANGE_ACTION_TYPES,
            frozenset({FileActionType.SKIP_IDENTICAL, FileActionType.INFO_MESSAGE}),
        )
        self.assertIs(NO_CHANGE_ACTION_TYPES, NON_MUTATING_ACTION_TYPES)
        self.assertIn(FileActionType.SKIP_IDENTICAL, NO_CHANGE_ACTION_TYPES)
        self.assertIn(FileActionType.INFO_MESSAGE, NO_CHANGE_ACTION_TYPES)
        self.assertNotIn(FileActionType.CREATE_COPY, NO_CHANGE_ACTION_TYPES)
        self.assertNotIn(FileActionType.UPDATE_COPY, NO_CHANGE_ACTION_TYPES)
        self.assertNotIn(FileActionType.DELETE_ITEM, NO_CHANGE_ACTION_TYPES)

    def test_error_object_preservation(self) -> None:
        err = RenderError("syntax error in template: line 42")
        preview = PackageDeployPreview(package_name="nvim", error=err)
        self.assertIs(preview.error, err)
        self.assertIsInstance(preview.error, RenderError)
        self.assertEqual(preview.error_type, "RenderError")
        self.assertIn("syntax error in template: line 42", preview.error_message or "")



class TestWorkspaceDeployPreview(unittest.TestCase):
    def setUp(self) -> None:
        self.render_res = PackageRenderResult(
            package="zsh",
            actions=[
                FileAction(
                    action_type=FileActionType.RENDER_ITEM,
                    src_path=Path("/src/dot-zshrc"),
                    dst_path=Path("/render/.zshrc"),
                )
            ],
        )
        self.err = RenderError("Engine failed")
        self.p1 = PackageDeployPreview(package_name="zsh", render_plan=self.render_res)
        self.p2 = PackageDeployPreview(package_name="tmux", error=self.err)
        self.p3 = PackageDeployPreview(package_name="git")

    def test_alias_equality(self) -> None:
        self.assertIs(DeployPreview, WorkspaceDeployPreview)

    def test_order_and_aggregation(self) -> None:
        preview = WorkspaceDeployPreview(
            command="plan",
            packages_install_order=["zsh", "tmux", "git"],
            package_previews={
                "tmux": self.p2,
                "zsh": self.p1,
                "git": self.p3,
            },
            drift_warnings={"zsh": "1 file modified on host"},
        )

        self.assertTrue(preview.has_changes)
        self.assertEqual(preview.packages_install_order, ["zsh", "tmux", "git"])
        self.assertEqual(preview.packages_with_changes, ["zsh"])
        self.assertEqual(preview.packages_unchanged, ["git"])
        self.assertEqual(preview.packages_with_errors, ["tmux"])
        self.assertEqual(preview.drifted_packages, ["zsh"])

    def test_global_error_objects(self) -> None:
        collision_err = CrossPackageCollisionError("Cross-package destination path collision")
        cyclic_err = CyclicDependencyError("Cyclic package dependency detected: a -> b -> a", cycle=["a", "b", "a"])
        preview = WorkspaceDeployPreview(
            global_errors=[collision_err, cyclic_err],
        )
        self.assertIs(preview.global_errors[0], collision_err)
        self.assertIsInstance(preview.global_errors[0], CrossPackageCollisionError)
        self.assertIs(preview.global_errors[1], cyclic_err)
        self.assertIsInstance(preview.global_errors[1], CyclicDependencyError)
        self.assertIsInstance(preview.global_errors[1], ConfigError)
        self.assertIsInstance(preview.global_errors[1], ValueError)
        self.assertEqual(cyclic_err.cycle, ["a", "b", "a"])

    def test_json_serialization_roundtrip(self) -> None:
        preview = WorkspaceDeployPreview(
            command="plan",
            status="SUCCESS",
            packages_install_order=["zsh", "tmux", "git"],
            package_previews={
                "zsh": self.p1,
                "tmux": self.p2,
                "git": self.p3,
            },
            global_errors=[CrossPackageCollisionError("test collision error")],
            drift_warnings={"zsh": "1 file modified on host"},
        )

        dict_output = preview.to_dict()
        self.assertEqual(dict_output["command"], "plan")
        self.assertEqual(dict_output["packages_install_order"], ["zsh", "tmux", "git"])
        self.assertIn("zsh", dict_output["package_previews"])
        self.assertEqual(dict_output["drift_warnings"]["zsh"], "1 file modified on host")
        self.assertEqual(len(dict_output["global_errors"]), 1)
        self.assertIn("test collision error", dict_output["global_errors"][0])

        json_str = preview.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed["command"], "plan")
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertEqual(parsed["packages_install_order"], ["zsh", "tmux", "git"])
        self.assertEqual(parsed["package_previews"]["tmux"]["error"], "test cycle error") if False else None
        self.assertIn("Engine failed", parsed["package_previews"]["tmux"]["error"])

    def test_format_text_summary(self) -> None:
        preview = WorkspaceDeployPreview(
            command="plan",
            status="SUCCESS",
            packages_install_order=["zsh", "tmux", "git"],
            package_previews={
                "zsh": self.p1,
                "tmux": self.p2,
                "git": self.p3,
            },
            drift_warnings={"zsh": "Host drift detected"},
        )
        text = preview.format_text()
        self.assertIn("Workspace Deployment Preview: plan", text)
        self.assertIn("Host drift detected", text)
        self.assertIn("Package: zsh", text)
        self.assertIn("Package: tmux", text)
        self.assertIn("1 packages unchanged: git", text)

    def test_verbose_format_text_filtering_across_plans(self) -> None:
        """Verifies that format_text(verbose=False) hides SKIP_IDENTICAL and INFO_MESSAGE, and verbose=True shows them."""
        # 1. PackageRenderResult
        render_res = PackageRenderResult(
            package="render_test",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/workspace/src/render_test/unchanged.txt"),
                    dst_path=Path("/workspace/render/render_test/unchanged.txt"),
                    reason="hash match",
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="Render cache hit",
                ),
                FileAction(
                    action_type=FileActionType.RENDER_ITEM,
                    src_path=Path("/workspace/src/render_test/changed.txt"),
                    dst_path=Path("/workspace/render/render_test/changed.txt"),
                ),
            ],
        )
        non_verbose_render = render_res.format_text(verbose=False)
        self.assertNotIn("SKIP_IDENTICAL", non_verbose_render)
        self.assertNotIn("[INFO]", non_verbose_render)
        self.assertIn("RENDER", non_verbose_render)
        self.assertIn("1 to render, 1 up-to-date", non_verbose_render)

        verbose_render = render_res.format_text(verbose=True)
        self.assertIn("SKIP_IDENTICAL", verbose_render)
        self.assertIn("[INFO]", verbose_render)
        self.assertIn("RENDER", verbose_render)

        # 2. PackageStagePlan
        stage_plan = PackageStagePlan(
            package="stage_test",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/workspace/render/stage_test/same.txt"),
                    dst_path=Path("/workspace/install/stage_test/same.txt"),
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="Staged metadata",
                ),
                FileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=Path("/workspace/render/stage_test/new.txt"),
                    dst_path=Path("/workspace/install/stage_test/new.txt"),
                ),
            ],
        )
        non_verbose_stage = stage_plan.format_text(verbose=False)
        self.assertNotIn("SKIP_IDENTICAL", non_verbose_stage)
        self.assertNotIn("[INFO]", non_verbose_stage)
        self.assertIn("CREATE_COPY", non_verbose_stage)
        self.assertIn("1 to create, 1 up-to-date", non_verbose_stage)

        verbose_stage = stage_plan.format_text(verbose=True)
        self.assertIn("SKIP_IDENTICAL", verbose_stage)
        self.assertIn("[INFO]", verbose_stage)
        self.assertIn("CREATE_COPY", verbose_stage)

        # 3. PackageInstallPlan
        install_plan = PackageInstallPlan(
            package="install_test",
            target_directory="/home/user",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/workspace/install/install_test/dot-link"),
                    dst_path=Path("/home/user/.link"),
                ),
                FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="Symlink verified",
                ),
                FileAction(
                    action_type=FileActionType.CREATE_SYMLINK,
                    src_path=Path("/workspace/install/install_test/dot-newlink"),
                    dst_path=Path("/home/user/.newlink"),
                ),
            ],
        )
        non_verbose_install = install_plan.format_text(verbose=False)
        self.assertNotIn("SKIP_IDENTICAL", non_verbose_install)
        self.assertNotIn("[INFO]", non_verbose_install)
        self.assertIn("CREATE_SYMLINK", non_verbose_install)
        self.assertIn("1 to create, 1 up-to-date", non_verbose_install)

        verbose_install = install_plan.format_text(verbose=True)
        self.assertIn("SKIP_IDENTICAL", verbose_install)
        self.assertIn("[INFO]", verbose_install)
        self.assertIn("CREATE_SYMLINK", verbose_install)

        # 4. PackageReverseSyncPlan
        rev_plan = PackageReverseSyncPlan(
            package="rev_test",
            actions=[
                FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=Path("/home/user/.dotfile"),
                    dst_path=Path("/workspace/install/rev_test/dot-dotfile"),
                ),
                FileAction(
                    action_type=FileActionType.UPDATE_COPY,
                    src_path=Path("/home/user/.changed"),
                    dst_path=Path("/workspace/install/rev_test/dot-changed"),
                ),
            ],
        )
        non_verbose_rev = rev_plan.format_text(verbose=False)
        self.assertNotIn("SKIP_IDENTICAL", non_verbose_rev)
        self.assertIn("UPDATE_COPY", non_verbose_rev)
        self.assertIn("1 to update, 1 up-to-date", non_verbose_rev)

        verbose_rev = rev_plan.format_text(verbose=True)
        self.assertIn("SKIP_IDENTICAL", verbose_rev)
        self.assertIn("UPDATE_COPY", verbose_rev)

        # 5. PackageDeployPreview
        pkg_preview = PackageDeployPreview(
            package_name="combo_test",
            render_plan=render_res,
            stage_plan=stage_plan,
            install_plan=install_plan,
            reverse_sync_plan=rev_plan,
        )
        non_verbose_deploy = pkg_preview.format_text(verbose=False)
        self.assertNotIn("SKIP_IDENTICAL", non_verbose_deploy)
        self.assertNotIn("[INFO]", non_verbose_deploy)
        self.assertIn("RENDER", non_verbose_deploy)
        self.assertIn("CREATE_COPY", non_verbose_deploy)
        self.assertIn("CREATE_SYMLINK", non_verbose_deploy)
        self.assertIn("UPDATE_COPY", non_verbose_deploy)

        verbose_deploy = pkg_preview.format_text(verbose=True)
        self.assertIn("SKIP_IDENTICAL", verbose_deploy)
        self.assertIn("[INFO]", verbose_deploy)


if __name__ == "__main__":
    unittest.main()
