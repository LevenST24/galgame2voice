from unittest.mock import patch

from galgame2voice.routers.system import (
    _apply_update_sync,
    _check_version_sync,
)


class TestUpdateRetryAndRecovery:
    def test_required_build_failure_persists_pending_and_recovers_on_retry(self, tmp_path):
        """
        Verifies:
        1. When git pull advances HEAD and has frontend changes, but build fails:
           - success is False
           - rebuilt_frontend is False
           - error contains actionable diagnosis
           - restart_required is True (backend .py changed in pull)
           - build_pending is True
        2. Version check reports build_pending=True across fresh queries / restarts.
        3. Retry update when git is already at latest commit:
           - re-attempts frontend build without needing force_rebuild_frontend
           - when build succeeds, success is True, rebuilt_frontend is True
           - restart_required remains True (preserved from initial pull)
           - pending state is cleared
        4. Subsequent update when clean:
           - does not trigger redundant builds
        """
        before_hash = "1111111111111111111111111111111111111111"
        after_hash = "2222222222222222222222222222222222222222"
        head_hash = before_hash
        git_calls = []

        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            nonlocal head_hash
            if args[:2] == ["remote", "get-url"]:
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            git_calls.append(args[0])
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "2222222" if head_hash == after_hash else "1111111", ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, head_hash, ""
            if args[0] == "status":
                return 0, "", ""
            if args[0] == "pull":
                prev = head_hash
                head_hash = after_hash
                if prev != after_hash:
                    return 0, "Updating 1111111..2222222\nFast-forward", ""
                return 0, "Already up to date.", ""
            if args[0] == "diff":
                if len(args) > 3 and args[2] == before_hash and args[3] == after_hash:
                    return 0, "frontend/src/settings.js\ngalgame2voice/main.py", ""
                return 0, "", ""
            return 0, "", ""

        build_succeeds = False
        build_calls = 0

        def fake_rebuild(cwd, timeout=120.0):
            nonlocal build_calls
            build_calls += 1
            if not build_succeeds:
                return False, "npm ERR! Missing npm or build failure"
            return True, "Vite deployed 3 assets"

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
             patch("galgame2voice.routers.system._rebuild_frontend_sync", side_effect=fake_rebuild):

            # --- Turn 1: Git pulls new commits, frontend changed, build fails ---
            res1, changed1 = _apply_update_sync(tmp_path)
            assert res1.success is False, f"Expected success=False on build failure, got {res1.success}"
            assert getattr(res1, "build_pending", False) is True, "Expected build_pending=True on build failure"
            assert res1.rebuilt_frontend is False
            assert res1.error is not None
            assert res1.current_version == "2222222"
            assert res1.restart_required is True
            assert build_calls == 1

            # --- Intermediate: Query version on a fresh call (recovering from disk) ---
            v_info = _check_version_sync(tmp_path, check_remote=False)
            assert getattr(v_info, "build_pending", False) is True
            assert v_info.current_version == "2222222"

            # --- Turn 2: User retries after fixing build environment (Git already up to date) ---
            build_succeeds = True
            res2, changed2 = _apply_update_sync(tmp_path)
            assert build_calls == 2, f"Expected second build call on retry, got {build_calls}"
            assert res2.success is True
            assert getattr(res2, "build_pending", False) is False
            assert res2.rebuilt_frontend is True
            assert res2.restart_required is True, "restart_required from turn 1's backend change must be retained"
            assert res2.error is None

            # Check version again: pending state should be cleared
            v_info2 = _check_version_sync(tmp_path, check_remote=False)
            assert getattr(v_info2, "build_pending", False) is False

            # --- Turn 3: Subsequent update when already up to date and no pending build ---
            res3, changed3 = _apply_update_sync(tmp_path)
            assert res3.success is True
            assert res3.rebuilt_frontend is False
            assert build_calls == 2, "Should not trigger rebuild when clean and up to date"

    def test_pulled_bundle_clears_pending_build_without_npm(self, tmp_path):
        """Verifies that if a future pull brings a pre-built static bundle, pending build is cleared without needing npm."""
        from galgame2voice.services.update_state import save_update_pending_state, load_update_pending_state

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        save_update_pending_state(data_dir, {
            "build_pending": True,
            "target_version": "1111111",
            "error": "prior build failed",
            "restart_required": False,
        })
        assert load_update_pending_state(data_dir) is not None

        calls = []
        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            calls.append(args[0])
            if args[:2] == ["remote", "get-url"]:
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "2222222" if "pull" in calls else "1111111", ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, ("2" if "pull" in calls else "1") * 40, ""
            if args[0] == "status":
                return 0, "", ""
            if args[0] == "pull":
                return 0, "Fast-forward", ""
            if args[0] == "diff":
                return 0, "frontend/src/settings.js\ngalgame2voice/static/index.html\ngalgame2voice/static/assets/index-new.js", ""
            return 0, "", ""

        build_calls = 0
        def fake_rebuild(cwd, timeout=120.0):
            nonlocal build_calls
            build_calls += 1
            return False, "npm not available"

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
             patch("galgame2voice.routers.system._rebuild_frontend_sync", side_effect=fake_rebuild):
            res, changed = _apply_update_sync(tmp_path)
            assert res.success is True
            assert res.rebuilt_frontend is False
            assert getattr(res, "build_pending", False) is False
            assert build_calls == 0
            assert load_update_pending_state(data_dir) is None

    def test_disk_save_failure_blocks_git_pull_and_build(self, tmp_path):
        """Fault injection: If state persistence to disk fails before pull, pull and build must not execute."""
        git_calls = []

        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            if args[:2] == ["remote", "get-url"]:
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            git_calls.append(args[0])
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "1111111", ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            return 0, "", ""

        build_calls = 0

        def fake_rebuild(cwd, timeout=120.0):
            nonlocal build_calls
            build_calls += 1
            return True, "deployed"

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
             patch("galgame2voice.routers.system._rebuild_frontend_sync", side_effect=fake_rebuild), \
             patch("galgame2voice.routers.system.save_update_pending_state", return_value=False):

            res, changed = _apply_update_sync(tmp_path)
            assert res.success is False
            assert "pull" not in git_calls, "Git pull must not be called if intent cannot be persisted"
            assert build_calls == 0, "Frontend build must not be called"
            assert "持久化失败" in res.output or "persistence failed" in (res.error or "").lower()

    def test_disk_clear_failure_does_not_report_full_success(self, tmp_path):
        """Fault injection: If build succeeds but pending file cannot be cleared, full success must not be reported."""
        calls = []

        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            calls.append(args[0])
            if args[:2] == ["remote", "get-url"]:
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "2222222" if "pull" in calls else "1111111", ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, ("2" if "pull" in calls else "1") * 40, ""
            if args[0] == "pull":
                return 0, "Fast-forward", ""
            if args[0] == "diff":
                return 0, "frontend/src/ui.js", ""
            return 0, "", ""

        build_calls = 0

        def fake_rebuild(cwd, timeout=120.0):
            nonlocal build_calls
            build_calls += 1
            return True, "Vite deployed 3 assets"

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
             patch("galgame2voice.routers.system._rebuild_frontend_sync", side_effect=fake_rebuild), \
             patch("galgame2voice.routers.system.clear_update_pending_state", return_value=False):

            res, changed = _apply_update_sync(tmp_path)
            assert build_calls == 1, "Must enter the frontend rebuild path and execute build"
            assert res.success is False, "Must not report full success if clear_update_pending_state failed"
            assert res.build_pending is True
            assert res.rebuilt_frontend is True
            assert "清除失败" in (res.error or "") or "清除失败" in res.output

    def test_second_save_disk_failure_preserves_restart_required_across_restart(self, tmp_path):
        """
        Fault injection:
        1. First save (pre_pull intent) succeeds.
        2. Git pull brings frontend + backend (.py) changes.
        3. Build fails.
        4. Second save (saving the failure state) fails due to disk error.
        5. Simulated server restart / fresh check: disk still has pre_pull intent.
        6. Retry update: build succeeds.
        7. restart_required must remain True (backend change from turn 1 must not be lost!).
        """
        before_hash = "1111111111111111111111111111111111111111"
        after_hash = "2222222222222222222222222222222222222222"
        head_hash = before_hash
        git_calls = []

        def fake_git(args, cwd, timeout=30.0, env_overrides=None):
            nonlocal head_hash
            if args[:2] == ["remote", "get-url"]:
                return 0, "https://github.com/LevenST24/galgame2voice.git", ""
            git_calls.append(args[0])
            if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args[:2] == ["rev-parse", "--short"]:
                return 0, "2222222" if head_hash == after_hash else "1111111", ""
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "main", ""
            if args[:2] == ["rev-parse", "HEAD"]:
                return 0, head_hash, ""
            if args[0] == "status":
                return 0, "", ""
            if args[0] == "pull":
                prev = head_hash
                head_hash = after_hash
                if prev != after_hash:
                    return 0, "Updating 1111111..2222222\nFast-forward", ""
                return 0, "Already up to date.", ""
            if args[0] == "diff":
                if len(args) > 3 and args[2] == before_hash and args[3] == after_hash:
                    return 0, "frontend/src/settings.js\ngalgame2voice/main.py", ""
                return 0, "", ""
            return 0, "", ""

        build_succeeds = False

        def fake_rebuild(cwd, timeout=120.0):
            if not build_succeeds:
                return False, "npm build failed"
            return True, "Vite deployed"

        from galgame2voice.services.update_state import save_update_pending_state as real_save

        save_call_count = 0

        def conditionally_failing_save(data_dir, state):
            nonlocal save_call_count
            save_call_count += 1
            if save_call_count == 2:
                # Second save (after build failure) fails!
                return False
            return real_save(data_dir, state)

        with patch("galgame2voice.routers.system._run_git_cmd", side_effect=fake_git), \
             patch("galgame2voice.routers.system._rebuild_frontend_sync", side_effect=fake_rebuild), \
             patch("galgame2voice.routers.system.save_update_pending_state", side_effect=conditionally_failing_save):

            # Turn 1: Pull with backend change, build fails, 2nd save fails
            res1, _ = _apply_update_sync(tmp_path)
            assert res1.success is False

            # Simulate restart: fresh query from disk
            v_info = _check_version_sync(tmp_path, check_remote=False)
            assert v_info.build_pending is True

            # Turn 2: Retry with fixed build, already at latest commit
            build_succeeds = True
            res2, _ = _apply_update_sync(tmp_path)
            assert res2.success is True
            assert res2.rebuilt_frontend is True
            assert res2.restart_required is True, "Must retain restart_required even if second save in turn 1 failed!"
