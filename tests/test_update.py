import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from server_backup_scripts.process_utils import run_process_with_stdout
from server_backup_scripts.update_server import Update, main

MODULE = "server_backup_scripts.update_server"


class FakeCommands:
    """Records commands and fails or answers those containing a given word."""

    def __init__(self, fail=(), running=True):
        self.calls = []
        self.envs = []
        self.fail = fail
        self.running = running

    def __call__(self, cmd, env=None):
        self.calls.append(cmd)
        self.envs.append(env)
        joined = " ".join(cmd)
        if any(word in joined for word in self.fail):
            return "", 1
        if cmd[-2:] == ["ps", "-q"]:
            return ("abc123\n" if self.running else ""), 0
        return "", 0

    def verbs(self, compose_file):
        return [
            cmd[cmd.index(compose_file) + 1 :]
            for cmd in self.calls
            if compose_file in cmd
        ]


class UpdateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.compose_dir = os.path.join(tmp.name, "docker")
        os.makedirs(self.compose_dir)
        self.reboot_file = os.path.join(tmp.name, "reboot-required")
        patcher = patch(f"{MODULE}.REBOOT_REQUIRED_FILE", self.reboot_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def project(self, name, filename="docker-compose.yml"):
        path = os.path.join(self.compose_dir, name, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("services: {}\n")
        return path

    def fake(self, **kwargs):
        commands = FakeCommands(**kwargs)
        patcher = patch(f"{MODULE}.run_process_with_stdout", commands)
        patcher.start()
        self.addCleanup(patcher.stop)
        return commands

    def run_cli(self, *extra):
        return CliRunner().invoke(main, ["--compose-dir", self.compose_dir, *extra])


class TestApt(UpdateTestCase):
    def test_update_runs_before_noninteractive_upgrade(self):
        commands = self.fake()
        self.assertTrue(Update().apt())
        self.assertEqual(commands.calls[0][0], "apt-get")
        self.assertEqual(commands.calls[0][-2:], ["update", "--error-on=any"])
        self.assertEqual(commands.calls[1][-1], "upgrade")
        self.assertIn("-y", commands.calls[1])
        self.assertIn("Dpkg::Options::=--force-confold", commands.calls[1])
        for env in commands.envs:
            self.assertEqual(env["DEBIAN_FRONTEND"], "noninteractive")

    def test_never_removes_packages(self):
        commands = self.fake()
        Update().apt()
        joined = " ".join(" ".join(cmd) for cmd in commands.calls)
        for word in ("dist-upgrade", "full-upgrade", "autoremove", "remove"):
            self.assertNotIn(word, joined)

    def test_failed_list_refresh_is_reported_but_still_upgrades(self):
        commands = self.fake(fail=["--error-on=any"])
        self.assertFalse(Update().apt())
        self.assertEqual(commands.calls[-1][-1], "upgrade")

    def test_failed_upgrade_is_reported(self):
        self.fake(fail=["upgrade"])
        self.assertFalse(Update().apt())


class TestCompose(UpdateTestCase):
    def test_finds_one_file_per_project(self):
        redis = self.project("redis")
        vault = self.project("vaultwarden", "compose.yaml")
        self.project("vaultwarden", "docker-compose.yml")
        os.makedirs(os.path.join(self.compose_dir, "not-a-project"))
        with open(os.path.join(self.compose_dir, "stray-file"), "w") as f:
            f.write("")
        self.assertEqual(
            Update(compose_dir=self.compose_dir).find_compose_files(), [redis, vault]
        )

    def test_missing_directory_finds_nothing(self):
        self.assertEqual(Update(compose_dir="/no/such/dir").find_compose_files(), [])

    def test_running_project_is_pulled_then_started(self):
        path = self.project("redis")
        commands = self.fake()
        self.assertEqual(Update(compose_dir=self.compose_dir).compose(), [])
        self.assertEqual(
            commands.verbs(path),
            [["ps", "-q"], ["pull", "--ignore-buildable"], ["up", "-d"]],
        )

    def test_stopped_project_is_left_stopped(self):
        path = self.project("redis")
        commands = self.fake(running=False)
        self.assertEqual(Update(compose_dir=self.compose_dir).compose(), [])
        self.assertEqual(commands.verbs(path), [["ps", "-q"]])

    def test_failed_pull_does_not_recreate_and_others_continue(self):
        bad = self.project("aaa-bad")
        good = self.project("zzz-good")
        commands = self.fake()
        original = commands.__call__

        def fail_bad_pull(cmd, env=None):
            if bad in cmd and "pull" in cmd:
                commands.calls.append(cmd)
                return "", 1
            return original(cmd, env)

        with patch(f"{MODULE}.run_process_with_stdout", fail_bad_pull):
            self.assertEqual(
                Update(compose_dir=self.compose_dir).compose(), ["aaa-bad"]
            )
        self.assertNotIn(["up", "-d"], commands.verbs(bad))
        self.assertIn(["up", "-d"], commands.verbs(good))

    def test_prune_only_removes_dangling_images(self):
        self.project("redis")
        commands = self.fake()
        Update(compose_dir=self.compose_dir).compose()
        self.assertEqual(commands.calls[-1], ["docker", "image", "prune", "-f"])

    def test_failed_prune_is_not_a_failure(self):
        self.project("redis")
        self.fake(fail=["prune"])
        self.assertEqual(Update(compose_dir=self.compose_dir).compose(), [])


class TestCli(UpdateTestCase):
    def test_success_exits_zero(self):
        self.project("redis")
        self.fake()
        self.assertEqual(self.run_cli().exit_code, 0)

    def test_apt_failure_exits_nonzero_but_still_updates_compose(self):
        path = self.project("redis")
        commands = self.fake(fail=["apt-get"])
        self.assertEqual(self.run_cli().exit_code, 1)
        self.assertIn(["up", "-d"], commands.verbs(path))

    def test_compose_failure_exits_nonzero(self):
        self.project("redis")
        self.fake(fail=["pull"])
        self.assertEqual(self.run_cli().exit_code, 1)

    def test_skip_flags(self):
        path = self.project("redis")
        commands = self.fake()
        self.run_cli("--skip-apt")
        self.assertFalse([c for c in commands.calls if c[0] == "apt-get"])
        commands.calls.clear()
        self.run_cli("--skip-compose")
        self.assertEqual(commands.verbs(path), [])
        self.assertTrue(commands.calls)

    def test_unhandled_exception_exits_nonzero(self):
        with patch.object(Update, "apt", side_effect=RuntimeError("boom")):
            self.assertNotEqual(self.run_cli().exit_code, 0)

    def test_reboot_required_never_reboots_or_fails(self):
        with open(self.reboot_file, "w") as f:
            f.write("")
        commands = self.fake()
        self.assertEqual(self.run_cli().exit_code, 0)
        for cmd in commands.calls:
            self.assertNotIn("reboot", cmd)


class TestEnvironment(unittest.TestCase):
    def test_env_adds_to_existing_environment(self):
        with patch.dict(os.environ, {"KEPT_VAR": "kept"}):
            out, code = run_process_with_stdout(
                ["sh", "-c", 'echo "$KEPT_VAR $ADDED_VAR"'], env={"ADDED_VAR": "added"}
            )
        self.assertEqual((out.strip(), code), ("kept added", 0))


if __name__ == "__main__":
    unittest.main()
