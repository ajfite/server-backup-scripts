import os
import pwd
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from server_backup_scripts.backup_server import (
    KEEP_BY_TYPE,
    STANDARD_BACKUP_TYPES,
    Collect,
    CollectResult,
    ExportManager,
    Rotate,
    main,
)
from server_backup_scripts.file_utils import (
    get_backup_files,
    get_most_recent_files,
    move_file_atomic,
    remove_stale_temp_files,
    rotate_directory_backups,
)
from server_backup_scripts.process_utils import (
    run_encrypted_pipeline,
    run_process_with_stdout,
)

MODULE = "server_backup_scripts.backup_server"
OLD_TIME = 1000

# A throwaway key pair, made once for the whole run
_key_dir = None
IDENTITY = None
RECIPIENTS = None


def setUpModule():
    global _key_dir, IDENTITY, RECIPIENTS
    _key_dir = tempfile.TemporaryDirectory()
    IDENTITY = os.path.join(_key_dir.name, "identity.txt")
    RECIPIENTS = os.path.join(_key_dir.name, "recipients.txt")
    subprocess.run(["age-keygen", "-o", IDENTITY], capture_output=True, check=True)
    with open(RECIPIENTS, "w") as f:
        subprocess.run(["age-keygen", "-y", IDENTITY], stdout=f, check=True)


def tearDownModule():
    _key_dir.cleanup()


def pipeline(producer, dest, **kwargs):
    return run_encrypted_pipeline(producer, dest, RECIPIENTS, **kwargs)


def write_file(path, content="data", mtime=None, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    if mode is not None:
        os.chmod(path, mode)
    return path


def file_mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def read_archive(path):
    return subprocess.run(
        f"age -d -i '{IDENTITY}' '{path}' | zstd -dc",
        shell=True,
        capture_output=True,
        check=True,
    ).stdout


def archive_members(path):
    return subprocess.run(
        f"age -d -i '{IDENTITY}' '{path}' | zstd -dc | tar -t",
        shell=True,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.backup_dir = os.path.join(self.root, "backups")
        self.export_dir = os.path.join(self.root, "export")
        self.amp_dir = os.path.join(self.root, "amp_instances")

    def fake_binary(self, name, script):
        bin_dir = os.path.join(self.root, "bin")
        write_file(os.path.join(bin_dir, name), f"#!/bin/sh\n{script}\n", mode=0o755)
        patcher = patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestFileUtils(TempDirTestCase):
    def test_get_backup_files_and_rotation(self):
        files = [
            write_file(
                os.path.join(self.root, f"backup_{i}.tar.zst"),
                mtime=OLD_TIME + i * 10,
            )
            for i in range(5)
        ]
        log_path = write_file(os.path.join(self.root, "backup_service.log"))
        tmp_path = write_file(os.path.join(self.root, "partial.tar.zst.tmp"))

        discovered = get_backup_files(self.root)
        self.assertEqual(discovered, list(reversed(files)))

        removed = rotate_directory_backups(self.root, keep_count=3)
        self.assertCountEqual(removed, files[:2])
        self.assertEqual(len(get_backup_files(self.root)), 3)
        self.assertTrue(os.path.exists(log_path))
        self.assertTrue(os.path.exists(tmp_path))

    def test_rotation_refuses_to_keep_nothing(self):
        path = write_file(os.path.join(self.root, "a.tar.zst"))
        self.assertEqual(rotate_directory_backups(self.root, keep_count=0), [])
        self.assertTrue(os.path.exists(path))

    def test_listing_ignores_symlinks(self):
        target = write_file(os.path.join(self.root, "target.txt"))
        os.symlink(target, os.path.join(self.root, "link.zip"))
        self.assertEqual(get_backup_files(self.root), [])
        self.assertEqual(get_most_recent_files(self.root, glob_str="*.zip"), [])

    def test_old_bz2_snapshots_can_rotate(self):
        path = write_file(os.path.join(self.root, "old.sqlite3.bz2"))
        self.assertEqual(get_backup_files(self.root), [path])

    def test_encrypted_old_snapshots_can_rotate(self):
        path = write_file(os.path.join(self.root, "old.sqlite3.zst.age"))
        self.assertEqual(get_backup_files(self.root), [path])

    def test_plain_and_encrypted_backups_rotate_together(self):
        plain = write_file(os.path.join(self.root, "a.sql.zst"), mtime=OLD_TIME)
        encrypted = write_file(os.path.join(self.root, "b.sql.zst.age"))
        self.assertEqual(rotate_directory_backups(self.root, 1), [plain])
        self.assertEqual(get_backup_files(self.root), [encrypted])

    def test_remove_stale_temp_files(self):
        stale = write_file(os.path.join(self.root, "a.sql.zst.tmp"))
        kept = write_file(os.path.join(self.root, "b.sql.zst"))
        remove_stale_temp_files(self.root)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(kept))

    def test_move_file_atomic_across_devices(self):
        src = write_file(os.path.join(self.root, "src.tar"), "content")
        dest = os.path.join(self.root, "dest.tar")
        with patch("os.rename", side_effect=OSError(18, "cross-device")):
            move_file_atomic(src, dest)
        self.assertFalse(os.path.exists(src))
        self.assertFalse(os.path.exists(f"{dest}.tmp"))
        with open(dest) as f:
            self.assertEqual(f.read(), "content")


class TestProcessUtils(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.root, "out.sql.zst.age")

    def test_pipeline_writes_private_verified_archive(self):
        self.assertTrue(pipeline(["echo", "hello"], self.dest))
        self.assertEqual(read_archive(self.dest), b"hello\n")
        self.assertEqual(file_mode(self.dest), 0o600)
        self.assertFalse(os.path.exists(f"{self.dest}.tmp"))

    def test_archive_is_unreadable_without_the_key(self):
        self.assertTrue(pipeline(["echo", "secret"], self.dest))
        with open(self.dest, "rb") as f:
            content = f.read()
        self.assertTrue(content.startswith(b"age-encryption.org/v1"))
        self.assertNotIn(b"secret", content)
        not_zstd = subprocess.run(["zstd", "-t", "-q", self.dest], capture_output=True)
        self.assertNotEqual(not_zstd.returncode, 0)

    def test_every_recipient_can_decrypt(self):
        second = os.path.join(self.root, "second.txt")
        subprocess.run(["age-keygen", "-o", second], capture_output=True, check=True)
        both = os.path.join(self.root, "both.txt")
        with open(both, "w") as f:
            f.write(open(RECIPIENTS).read())
            subprocess.run(["age-keygen", "-y", second], stdout=f, check=True)
        self.assertTrue(run_encrypted_pipeline(["echo", "hi"], self.dest, both))
        for identity in (IDENTITY, second):
            out = subprocess.run(
                f"age -d -i '{identity}' '{self.dest}' | zstd -dc",
                shell=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(out, b"hi\n")

    def test_missing_recipients_file_fails_and_writes_nothing(self):
        self.assertFalse(
            run_encrypted_pipeline(["echo", "x"], self.dest, "/no/such/recipients")
        )
        self.assertEqual(os.listdir(self.root), [])

    def test_age_failure_is_a_failure(self):
        self.fake_binary("age", "cat >/dev/null; exit 1")
        self.assertFalse(pipeline(["echo", "data"], self.dest))
        self.assertEqual(os.listdir(self.root), ["bin"])

    def test_age_dying_early_does_not_hang(self):
        self.fake_binary("age", "exit 1")
        self.assertFalse(
            pipeline(["head", "-c", "50000000", "/dev/urandom"], self.dest)
        )
        self.assertFalse(os.path.exists(self.dest))

    def test_producer_failure_leaves_nothing(self):
        self.assertFalse(pipeline(["false"], self.dest))
        self.assertEqual(os.listdir(self.root), [])

    def test_ok_codes_apply_to_producer(self):
        self.fake_binary("exit1", "echo data; exit 1")
        self.assertFalse(pipeline(["exit1"], self.dest))
        self.assertTrue(pipeline(["exit1"], self.dest, ok_codes=(0, 1)))
        self.assertEqual(read_archive(self.dest), b"data\n")

    def test_zstd_exit_one_is_never_acceptable(self):
        self.fake_binary("zstd", "cat >/dev/null; echo partial; exit 1")
        self.assertFalse(pipeline(["echo", "data"], self.dest, ok_codes=(0, 1)))
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(f"{self.dest}.tmp"))

    def test_corrupt_output_fails_verification(self):
        self.fake_binary(
            "zstd",
            'if [ "$1" = "-t" ]; then exit 1; fi; cat >/dev/null; echo garbage',
        )
        self.assertFalse(pipeline(["echo", "data"], self.dest))
        self.assertFalse(os.path.exists(self.dest))

    def test_missing_producer_fails_cleanly(self):
        self.assertFalse(pipeline(["no-such-command-xyz"], self.dest))
        self.assertEqual(os.listdir(self.root), [])

    def test_shell_metacharacters_are_not_interpreted(self):
        dest = os.path.join(self.root, "a b; touch pwned.zst")
        self.assertTrue(pipeline(["echo", "x"], dest))
        self.assertTrue(os.path.exists(dest))

    def test_missing_command_returns_127(self):
        self.assertEqual(run_process_with_stdout(["no-such-command-xyz"])[1], 127)

    def test_undecodable_output_does_not_raise(self):
        self.fake_binary("badbytes", r"printf '\377\376\n'")
        out, code = run_process_with_stdout(["badbytes"])
        self.assertEqual(code, 0)
        self.assertIn("�", out)


class TestCollectors(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.collector = Collect(backup_dir=self.backup_dir, recipients_file=RECIPIENTS)
        self.collector.prepare()

    def test_prepare_creates_private_dirs(self):
        self.assertEqual(file_mode(self.backup_dir), 0o700)
        for b_type in STANDARD_BACKUP_TYPES:
            self.assertEqual(file_mode(os.path.join(self.backup_dir, b_type)), 0o700)

    def test_prepare_removes_legacy_amp_links(self):
        legacy = os.path.join(self.backup_dir, "amp")
        os.makedirs(legacy)
        target = os.path.join(self.root, "amp_backups")
        os.makedirs(target)
        os.symlink(target, os.path.join(legacy, "Instance"))
        self.collector.prepare()
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.isdir(target))

    def test_prepare_rejects_file_in_place_of_directory(self):
        path = os.path.join(self.root, "other")
        write_file(os.path.join(path, "etc"))
        with self.assertRaises(OSError):
            Collect(backup_dir=path).prepare()

    def test_prepare_clears_partial_archives(self):
        stale = write_file(os.path.join(self.backup_dir, "etc", "x.tar.zst.tmp"))
        self.collector.prepare()
        self.assertFalse(os.path.exists(stale))

    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/mariadb-dump")
    @patch(f"{MODULE}.run_encrypted_pipeline", return_value=True)
    def test_mariadb_collector(self, mock_pipeline, _which):
        self.assertIs(self.collector.mariadb(), CollectResult.OK)
        self.assertEqual(
            mock_pipeline.call_args[0][0],
            ["mariadb-dump", "--all-databases", "--single-transaction", "--quick"],
        )

    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/pg_dumpall")
    @patch(f"{MODULE}.run_encrypted_pipeline", return_value=False)
    def test_postgres_failure_is_reported(self, mock_pipeline, _which):
        self.assertIs(self.collector.postgres(), CollectResult.FAILED)
        self.assertEqual(
            mock_pipeline.call_args[0][0], ["sudo", "-u", "postgres", "pg_dumpall"]
        )

    @patch(f"{MODULE}.shutil.which", return_value=None)
    def test_missing_tool_is_skip_not_failure(self, _which):
        with patch(f"{MODULE}.GITLAB_BACKUP_COMMAND", "/no/such/gitlab-backup"):
            self.assertEqual(self.collector.collect_all(), [])

    def test_archive_directory_real_pipeline(self):
        target = os.path.join(self.root, "src", "www")
        write_file(os.path.join(target, "index.html"))
        self.assertIs(self.collector.archive_directory(target, "www"), CollectResult.OK)
        archive = get_backup_files(os.path.join(self.backup_dir, "www"))[0]
        self.assertIn("www/index.html", archive_members(archive))
        self.assertEqual(file_mode(archive), 0o600)

    def test_archive_directory_tolerates_tar_exit_one(self):
        self.fake_binary("tar", "echo data; echo 'file changed' >&2; exit 1")
        target = os.path.join(self.root, "www")
        os.makedirs(target)
        self.assertIs(self.collector.archive_directory(target, "www"), CollectResult.OK)

    def test_archive_directory_rejects_tar_exit_two(self):
        self.fake_binary("tar", "echo data; exit 2")
        target = os.path.join(self.root, "www")
        os.makedirs(target)
        self.assertIs(
            self.collector.archive_directory(target, "www"), CollectResult.FAILED
        )
        self.assertEqual(os.listdir(os.path.join(self.backup_dir, "www")), [])

    def test_archive_directory_missing_target_skips(self):
        self.assertIs(
            self.collector.archive_directory("/no/such/dir", "www"),
            CollectResult.SKIPPED,
        )

    def test_one_collector_raising_does_not_stop_others(self):
        ran = []

        def record(name, result=CollectResult.OK):
            def collector():
                ran.append(name)
                return result

            return collector

        def boom():
            raise OSError("disk full")

        with (
            patch.object(self.collector, "gitlab", boom),
            patch.object(self.collector, "postgres", record("postgres")),
            patch.object(self.collector, "vaultwarden", record("vaultwarden")),
            patch.object(self.collector, "etc", record("etc")),
            patch.object(self.collector, "www", record("www")),
            patch.object(
                self.collector, "mariadb", record("mariadb", CollectResult.FAILED)
            ),
        ):
            self.assertEqual(self.collector.collect_all(), ["gitlab", "mariadb"])
        self.assertEqual(len(ran), 5)


class TestGitlabCollector(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.collector = Collect(backup_dir=self.backup_dir, recipients_file=RECIPIENTS)
        self.collector.prepare()
        self.gitlab_dir = os.path.join(self.root, "gitlab_src")
        self.old = write_file(
            os.path.join(self.gitlab_dir, "old_gitlab_backup.tar"), mtime=OLD_TIME
        )
        for patcher in (
            patch(f"{MODULE}.GITLAB_BACKUP_DIR", self.gitlab_dir),
            patch(f"{MODULE}.shutil.which", return_value="/usr/bin/gitlab-backup"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def creates_backup(self, _cmd):
        write_file(os.path.join(self.gitlab_dir, "new_gitlab_backup.tar"), mode=0o600)
        return "", 0

    def test_new_backup_is_moved_and_private(self):
        with patch(f"{MODULE}.run_process_with_stdout", self.creates_backup):
            self.assertIs(self.collector.gitlab(), CollectResult.OK)
        dest = os.path.join(self.backup_dir, "gitlab", "new_gitlab_backup.tar")
        self.assertEqual(file_mode(dest), 0o600)
        self.assertEqual(os.listdir(self.gitlab_dir), ["old_gitlab_backup.tar"])

    def test_old_backup_is_not_mistaken_for_new(self):
        with patch(f"{MODULE}.run_process_with_stdout", return_value=("", 0)):
            self.assertIs(self.collector.gitlab(), CollectResult.FAILED)
        self.assertEqual(os.listdir(os.path.join(self.backup_dir, "gitlab")), [])
        self.assertTrue(os.path.exists(self.old))

    def test_command_failure(self):
        with patch(f"{MODULE}.run_process_with_stdout", return_value=("", 1)):
            self.assertIs(self.collector.gitlab(), CollectResult.FAILED)

    def test_runs_the_path_that_was_found(self):
        omnibus = write_file(os.path.join(self.root, "opt", "gitlab-backup"))
        with (
            patch(f"{MODULE}.shutil.which", return_value=None),
            patch(f"{MODULE}.GITLAB_BACKUP_COMMAND", omnibus),
            patch(f"{MODULE}.run_process_with_stdout", return_value=("", 1)) as run,
        ):
            self.collector.gitlab()
        self.assertEqual(run.call_args[0][0], [omnibus, "create"])


class TestVaultwardenCollector(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.collector = Collect(backup_dir=self.backup_dir, recipients_file=RECIPIENTS)
        self.collector.prepare()
        self.vw_dir = os.path.join(self.root, "vw-data")
        self.live_db = write_file(os.path.join(self.vw_dir, "db.sqlite3"), "LIVE")
        write_file(os.path.join(self.vw_dir, "rsa_key.pem"))
        write_file(os.path.join(self.vw_dir, "attachments", "a.bin"))
        self.snapshot = os.path.join(self.vw_dir, "db_20261031_000000.sqlite3")
        for patcher in (
            patch(f"{MODULE}.VAULTWARDEN_BACKUP_DIR", self.vw_dir),
            patch(f"{MODULE}.shutil.which", return_value="/usr/bin/docker"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def docker_writes_snapshot(self, _cmd):
        # The live database is touched after the snapshot, so it is newest.
        write_file(self.snapshot, "SNAP", mtime=OLD_TIME)
        os.utime(self.live_db)
        return "", 0

    def archives(self):
        return get_backup_files(os.path.join(self.backup_dir, "vaultwarden"))

    def test_live_database_is_never_archived_or_deleted(self):
        with patch(f"{MODULE}.run_process_with_stdout", self.docker_writes_snapshot):
            self.assertIs(self.collector.vaultwarden(), CollectResult.OK)
        self.assertTrue(os.path.exists(self.live_db))
        self.assertFalse(os.path.exists(self.snapshot))
        members = archive_members(self.archives()[0])
        self.assertIn(os.path.basename(self.snapshot), members)
        self.assertIn("rsa_key.pem", members)
        self.assertNotIn("db.sqlite3", members)

    def test_archive_members_are_relative(self):
        with patch(f"{MODULE}.run_process_with_stdout", self.docker_writes_snapshot):
            self.collector.vaultwarden()
        for member in archive_members(self.archives()[0]):
            self.assertNotIn(self.vw_dir.lstrip("/"), member)

    def test_missing_optional_members_are_fine(self):
        self.assertFalse(os.path.exists(os.path.join(self.vw_dir, "config.json")))
        self.assertFalse(os.path.exists(os.path.join(self.vw_dir, "sends")))
        with patch(f"{MODULE}.run_process_with_stdout", self.docker_writes_snapshot):
            self.assertIs(self.collector.vaultwarden(), CollectResult.OK)
        self.assertFalse(os.path.exists(os.path.join(self.vw_dir, "sends")))

    def test_no_new_snapshot_fails_and_touches_nothing(self):
        stale = write_file(
            os.path.join(self.vw_dir, "db_20260221_070336.sqlite3"), mtime=OLD_TIME
        )
        with patch(f"{MODULE}.run_process_with_stdout", return_value=("", 0)):
            self.assertIs(self.collector.vaultwarden(), CollectResult.FAILED)
        self.assertTrue(os.path.exists(self.live_db))
        self.assertTrue(os.path.exists(stale))
        self.assertEqual(self.archives(), [])

    def test_tar_failure_cleans_up_snapshot(self):
        self.fake_binary("tar", "exit 2")
        with patch(f"{MODULE}.run_process_with_stdout", self.docker_writes_snapshot):
            self.assertIs(self.collector.vaultwarden(), CollectResult.FAILED)
        self.assertFalse(os.path.exists(self.snapshot))
        self.assertTrue(os.path.exists(self.live_db))
        self.assertEqual(self.archives(), [])


class TestRotation(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.pg_dir = os.path.join(self.backup_dir, "postgres")
        for i in range(4):
            write_file(
                os.path.join(self.pg_dir, f"file_{i}.sql.zst"),
                mtime=OLD_TIME + i * 10,
            )

    def test_rotate_all(self):
        results = Rotate(backup_dir=self.backup_dir, keep=2).rotate_all()
        self.assertEqual(len(results["postgres"]), 2)
        self.assertCountEqual(
            os.listdir(self.pg_dir), ["file_3.sql.zst", "file_2.sql.zst"]
        )

    def test_unselected_type_keeps_everything(self):
        Rotate(backup_dir=self.backup_dir, keep=2).rotate_all(["etc"])
        self.assertEqual(len(os.listdir(self.pg_dir)), 4)

    def test_keep_comes_from_type_unless_overridden(self):
        self.assertEqual(
            KEEP_BY_TYPE,
            {
                "vaultwarden": 30,
                "postgres": 5,
                "mariadb": 5,
                "etc": 4,
                "www": 4,
                "gitlab": 5,
            },
        )
        for b_type, count in (("vaultwarden", 32), ("etc", 6), ("gitlab", 7)):
            for i in range(count):
                write_file(
                    os.path.join(self.backup_dir, b_type, f"{i}.tar.zst"),
                    mtime=OLD_TIME + i,
                )
        Rotate(backup_dir=self.backup_dir).rotate_all()
        for b_type, kept in (("vaultwarden", 30), ("etc", 4), ("gitlab", 5)):
            remaining = os.listdir(os.path.join(self.backup_dir, b_type))
            self.assertEqual(len(remaining), kept, b_type)
        self.assertNotIn("0.tar.zst", os.listdir(os.path.join(self.backup_dir, "etc")))

    def test_skipped_type_keeps_everything(self):
        Rotate(backup_dir=self.backup_dir, keep=2).rotate_all(skip_types=["postgres"])
        self.assertEqual(len(os.listdir(self.pg_dir)), 4)


@unittest.skipIf(os.geteuid() == 0, "covered by TestExportOwnershipAsRoot")
class TestExportManager(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.vw_dir = os.path.join(self.backup_dir, "vaultwarden")
        self.b1 = write_file(os.path.join(self.vw_dir, "backup1.tar.zst"), mode=0o600)
        self.b2 = write_file(os.path.join(self.vw_dir, "backup2.tar.zst"), mode=0o600)
        self.exporter = ExportManager(
            backup_dir=self.backup_dir,
            export_dir=self.export_dir,
            export_user="testuser",
            amp_dir=self.amp_dir,
        )
        self.exp_vw = os.path.join(self.export_dir, "vaultwarden")

    def amp_backup(self, instance, name="save.zip", mtime=OLD_TIME):
        return write_file(
            os.path.join(self.amp_dir, instance, "Backups", name), mtime=mtime
        )

    def test_sync_and_prune(self):
        self.assertEqual(self.exporter.sync_all(), [])
        exp_b1 = os.path.join(self.exp_vw, "backup1.tar.zst")
        exp_b2 = os.path.join(self.exp_vw, "backup2.tar.zst")
        self.assertEqual(os.stat(self.b1).st_ino, os.stat(exp_b1).st_ino)
        self.assertEqual(file_mode(exp_b1), 0o640)
        self.assertEqual(file_mode(self.export_dir), 0o750)
        self.assertEqual(file_mode(self.exp_vw), 0o750)

        os.remove(self.b1)
        self.assertEqual(self.exporter.sync_all(), [])
        self.assertFalse(os.path.exists(exp_b1))
        self.assertTrue(os.path.exists(exp_b2))

    def test_only_selected_types_are_exported(self):
        pg = write_file(os.path.join(self.backup_dir, "postgres", "p.sql.zst"))
        self.amp_backup("Inst")
        self.assertEqual(self.exporter.sync_all(["postgres"]), [])
        self.assertTrue(os.path.exists(os.path.join(self.export_dir, "postgres")))
        self.assertTrue(os.path.exists(pg))
        for skipped in ("vaultwarden", "amp"):
            self.assertFalse(os.path.exists(os.path.join(self.export_dir, skipped)))

    def test_amp_can_be_selected_alone(self):
        self.amp_backup("Inst")
        self.assertEqual(self.exporter.sync_all(["amp"]), [])
        self.assertEqual(os.listdir(self.export_dir), ["amp"])

    def test_empty_source_does_not_wipe_export(self):
        self.exporter.sync_all()
        os.remove(self.b1)
        os.remove(self.b2)
        self.exporter.sync_all()
        self.assertEqual(len(os.listdir(self.exp_vw)), 2)

    def test_prune_leaves_non_backup_files(self):
        self.exporter.sync_all()
        marker = write_file(os.path.join(self.exp_vw, "sync-marker"))
        self.exporter.sync_all()
        self.assertTrue(os.path.exists(marker))

    def test_replaces_stale_file_of_same_name(self):
        stale = write_file(os.path.join(self.exp_vw, "backup1.tar.zst"), "stale")
        self.exporter.sync_all()
        self.assertEqual(os.stat(stale).st_ino, os.stat(self.b1).st_ino)

    def test_replaced_type_dir_symlink_is_refused(self):
        victim_dir = os.path.join(self.root, "victim")
        victim = write_file(os.path.join(victim_dir, "keep.tar.zst"), mode=0o600)
        os.chmod(victim_dir, 0o700)
        os.makedirs(self.export_dir)
        os.symlink(victim_dir, self.exp_vw)

        self.assertEqual(self.exporter.sync_all(), ["export:vaultwarden"])
        self.assertEqual(file_mode(victim_dir), 0o700)
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(os.listdir(victim_dir), ["keep.tar.zst"])

    def test_replaced_export_root_symlink_is_refused(self):
        victim_dir = os.path.join(self.root, "victim")
        os.makedirs(victim_dir, mode=0o700)
        os.symlink(victim_dir, self.export_dir)
        with self.assertRaises(OSError):
            self.exporter.sync_all()
        self.assertEqual(file_mode(victim_dir), 0o700)

    def test_amp_zip_exported_without_rotation(self):
        zips = [self.amp_backup("Inst", f"save{i}.zip", OLD_TIME + i) for i in range(7)]
        Rotate(backup_dir=self.backup_dir, keep=2).rotate_all()
        self.assertEqual(self.exporter.sync_all(), [])
        for path in zips:
            self.assertTrue(os.path.exists(path))
        exported = os.path.join(self.export_dir, "amp", "Inst", "save0.zip")
        self.assertEqual(os.stat(exported).st_ino, os.stat(zips[0]).st_ino)

    def test_amp_ignores_sidecars_and_recent_zips(self):
        self.amp_backup("Inst", "old.zip")
        self.amp_backup("Inst", "writing.zip", mtime=None)
        self.amp_backup("Inst", "manifest.json")
        self.exporter.sync_all()
        self.assertEqual(
            os.listdir(os.path.join(self.export_dir, "amp", "Inst")), ["old.zip"]
        )

    def test_amp_zip_symlink_cannot_change_target(self):
        victim = write_file(os.path.join(self.root, "shadow"), "secret", mode=0o600)
        backups = os.path.join(self.amp_dir, "Inst", "Backups")
        os.makedirs(backups)
        os.symlink(victim, os.path.join(backups, "evil.zip"))

        self.assertEqual(self.exporter.sync_all(), ["export:amp"])
        self.assertEqual(file_mode(victim), 0o600)
        self.assertEqual(os.listdir(os.path.join(self.export_dir, "amp", "Inst")), [])

    def test_amp_backups_symlink_is_ignored(self):
        outside = os.path.join(self.root, "outside")
        write_file(os.path.join(outside, "other.zip"), mode=0o600)
        os.makedirs(os.path.join(self.amp_dir, "Inst"))
        os.symlink(outside, os.path.join(self.amp_dir, "Inst", "Backups"))

        self.exporter.sync_all()
        self.assertEqual(os.listdir(outside), ["other.zip"])
        self.assertEqual(file_mode(os.path.join(outside, "other.zip")), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "amp", "Inst")))

    def test_amp_deleted_instance_is_pruned(self):
        self.amp_backup("Gone")
        self.amp_backup("Stays")
        self.exporter.sync_all()
        write_file(os.path.join(self.amp_dir, "Gone", "not-a-dir"))
        os.rename(
            os.path.join(self.amp_dir, "Gone", "Backups"),
            os.path.join(self.root, "moved"),
        )
        self.exporter.sync_all()
        self.assertEqual(os.listdir(os.path.join(self.export_dir, "amp")), ["Stays"])

    def test_amp_no_instances_does_not_wipe_export(self):
        self.amp_backup("Inst")
        self.exporter.sync_all()
        os.rename(self.amp_dir, f"{self.amp_dir}.moved")
        os.makedirs(self.amp_dir)
        self.exporter.sync_all()
        self.assertEqual(os.listdir(os.path.join(self.export_dir, "amp")), ["Inst"])


@unittest.skipUnless(os.geteuid() == 0, "ownership is only applied as root")
class TestExportOwnershipAsRoot(TempDirTestCase):
    def setUp(self):
        super().setUp()
        os.chmod(self.root, 0o755)
        self.export_user = pwd.getpwnam("nobody")
        self.amp_user = pwd.getpwnam("daemon")
        self.backup = write_file(
            os.path.join(self.backup_dir, "etc", "x.tar.zst"), mode=0o600
        )
        self.exporter = ExportManager(
            backup_dir=self.backup_dir,
            export_dir=self.export_dir,
            export_user="nobody",
            amp_dir=self.amp_dir,
        )

    def amp_zip(self, uid):
        backups = os.path.join(self.amp_dir, "Inst", "Backups")
        path = write_file(os.path.join(backups, "save.zip"), mtime=OLD_TIME)
        for owned in (os.path.dirname(backups), backups):
            os.chown(owned, self.amp_user.pw_uid, self.amp_user.pw_gid)
        os.chown(path, uid, uid)
        return path

    def test_export_is_root_owned_and_group_readable(self):
        self.assertEqual(self.exporter.sync_all(), [])
        for path in (self.export_dir, os.path.join(self.export_dir, "etc")):
            st = os.stat(path)
            self.assertEqual((st.st_uid, st.st_gid), (0, self.export_user.pw_gid), path)
        st = os.stat(self.backup)
        self.assertEqual((st.st_uid, st.st_gid), (0, self.export_user.pw_gid))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o640)

    def test_amp_zip_stays_owned_by_amp(self):
        path = self.amp_zip(self.amp_user.pw_uid)
        self.assertEqual(self.exporter.sync_all(), [])
        st = os.stat(path)
        self.assertEqual(
            (st.st_uid, st.st_gid), (self.amp_user.pw_uid, self.export_user.pw_gid)
        )

    def test_amp_zip_previously_chowned_to_export_user_is_restored(self):
        path = self.amp_zip(self.export_user.pw_uid)
        self.assertEqual(self.exporter.sync_all(), [])
        self.assertEqual(os.stat(path).st_uid, self.amp_user.pw_uid)

    def test_root_owned_file_in_amp_dir_is_not_exported(self):
        path = self.amp_zip(0)
        os.chmod(path, 0o600)
        self.exporter.sync_all()
        self.assertEqual(file_mode(path), 0o600)
        self.assertEqual(os.stat(path).st_uid, 0)
        self.assertEqual(os.listdir(os.path.join(self.export_dir, "amp", "Inst")), [])

    def test_symlink_zip_cannot_take_over_target(self):
        victim = write_file(os.path.join(self.root, "shadow"), "secret", mode=0o600)
        path = self.amp_zip(self.amp_user.pw_uid)
        os.remove(path)
        os.symlink(victim, path)
        self.assertEqual(self.exporter.sync_all(), ["export:amp"])
        st = os.stat(victim)
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (0, 0o600))

    def test_backups_dir_owned_by_root_is_refused(self):
        path = self.amp_zip(0)
        os.chown(os.path.dirname(path), 0, 0)
        self.assertEqual(self.exporter.sync_all(), ["export:amp"])

    def test_replaced_type_dir_symlink_is_refused(self):
        victim_dir = os.path.join(self.root, "victim")
        victim = write_file(os.path.join(victim_dir, "keep.tar.zst"), mode=0o600)
        os.chmod(victim_dir, 0o700)
        os.makedirs(self.export_dir)
        os.symlink(victim_dir, os.path.join(self.export_dir, "etc"))

        self.assertEqual(self.exporter.sync_all(), ["export:etc"])
        st = os.stat(victim_dir)
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (0, 0o700))
        self.assertEqual(os.stat(victim).st_uid, 0)
        self.assertEqual(os.listdir(victim_dir), ["keep.tar.zst"])

    def test_missing_export_user_fails(self):
        exporter = ExportManager(
            self.backup_dir, self.export_dir, "no-such-user-xyz", self.amp_dir
        )
        with self.assertRaises(KeyError):
            exporter.sync_all()


class TestCliIntegration(TempDirTestCase):
    def run_cli(self, *extra):
        return CliRunner().invoke(
            main,
            [
                "--backup-dir",
                self.backup_dir,
                "--export-dir",
                self.export_dir,
                "--amp-dir",
                self.amp_dir,
                "--export-user",
                "nobody",
                *extra,
            ],
        )

    def etc_backups(self, count=4):
        for i in range(count):
            write_file(
                os.path.join(self.backup_dir, "etc", f"etc_{i}.tar.zst"),
                mtime=OLD_TIME + i * 10,
            )

    def test_cli_skip_collect_rotation_export(self):
        self.etc_backups()
        result = self.run_cli("--keep", "2", "--skip-collect")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(os.listdir(os.path.join(self.backup_dir, "etc"))), 2)
        self.assertEqual(len(os.listdir(os.path.join(self.export_dir, "etc"))), 2)
        self.assertEqual(file_mode(self.backup_dir), 0o700)

    @unittest.skipIf(os.geteuid() == 0, "AMP dirs must not be root-owned")
    def test_skip_collect_does_not_touch_amp(self):
        zips = [
            write_file(
                os.path.join(self.amp_dir, "Inst", "Backups", f"{i}.zip"),
                mtime=OLD_TIME + i,
            )
            for i in range(4)
        ]
        self.assertEqual(self.run_cli("--keep", "1", "--skip-collect").exit_code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.backup_dir, "amp")))
        for path in zips:
            self.assertTrue(os.path.exists(path))

    def test_only_limits_collectors_rotation_and_export(self):
        self.etc_backups(6)
        write_file(os.path.join(self.backup_dir, "www", "w.tar.zst"))
        for i in range(8):
            write_file(
                os.path.join(self.backup_dir, "postgres", f"{i}.sql.zst"),
                mtime=OLD_TIME + i,
            )
        ran = []

        def collector(name):
            def run(_self):
                ran.append(name)
                return CollectResult.OK

            return run

        with (
            patch.object(Collect, "etc", collector("etc")),
            patch.object(Collect, "www", collector("www")),
            patch.object(Collect, "postgres", collector("postgres")),
            patch.object(Collect, "mariadb", collector("mariadb")),
            patch.object(Collect, "gitlab", collector("gitlab")),
            patch.object(Collect, "vaultwarden", collector("vaultwarden")),
        ):
            result = self.run_cli("--only", "etc", "--only", "www")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(ran, ["etc", "www"])
        self.assertEqual(len(os.listdir(os.path.join(self.backup_dir, "etc"))), 4)
        self.assertEqual(len(os.listdir(os.path.join(self.backup_dir, "postgres"))), 8)
        self.assertCountEqual(os.listdir(self.export_dir), ["etc", "www"])

    def test_only_rejects_unknown_type(self):
        self.assertEqual(self.run_cli("--only", "nope").exit_code, 2)

    def test_only_amp_runs_no_collectors(self):
        with patch.object(Collect, "gitlab", side_effect=AssertionError("ran")):
            result = self.run_cli("--only", "amp")
        self.assertEqual(result.exit_code, 0)

    def test_keep_zero_is_rejected(self):
        self.etc_backups()
        result = self.run_cli("--keep", "0", "--skip-collect")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(len(os.listdir(os.path.join(self.backup_dir, "etc"))), 4)

    def test_failed_collector_exits_nonzero_and_skips_its_rotation(self):
        self.etc_backups(6)
        with patch.object(Collect, "collect_all", return_value=["etc"]):
            result = self.run_cli("--keep", "2")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(os.listdir(os.path.join(self.backup_dir, "etc"))), 6)
        self.assertEqual(len(os.listdir(os.path.join(self.export_dir, "etc"))), 6)

    def test_rotation_exception_exits_nonzero_but_still_exports(self):
        self.etc_backups(1)
        with patch.object(Rotate, "rotate_all", side_effect=OSError("boom")):
            result = self.run_cli("--skip-collect")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(len(os.listdir(os.path.join(self.export_dir, "etc"))), 1)

    def test_export_failure_exits_nonzero(self):
        self.etc_backups(1)
        os.makedirs(self.export_dir)
        os.symlink(self.root, os.path.join(self.export_dir, "etc"))
        result = self.run_cli("--skip-collect")
        self.assertEqual(result.exit_code, 1)

    def test_unhandled_exception_exits_nonzero(self):
        with patch.object(Collect, "collect_all", side_effect=RuntimeError("boom")):
            result = self.run_cli()
        self.assertNotEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
