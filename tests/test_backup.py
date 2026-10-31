import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from server_backup_scripts.backup_server import (
    Collect,
    ExportManager,
    Rotate,
    main,
)
from server_backup_scripts.file_utils import (
    get_backup_files,
    link_file_space_efficient,
    rotate_directory_backups,
)


class TestFileUtils(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_get_backup_files_and_rotation(self):
        dir_path = self.test_dir.name
        files = []
        for i in range(5):
            f_path = os.path.join(dir_path, f"backup_{i}.tar.zst")
            with open(f_path, "w") as f:
                f.write(f"data {i}")
            os.utime(f_path, (1000 + i * 10, 1000 + i * 10))
            files.append(f_path)

        log_path = os.path.join(dir_path, "backup_service.log")
        with open(log_path, "w") as f:
            f.write("log data")

        discovered = get_backup_files(dir_path)
        self.assertEqual(len(discovered), 5)
        self.assertEqual(discovered[0], files[4])
        self.assertEqual(discovered[-1], files[0])

        removed = rotate_directory_backups(dir_path, keep_count=3)
        self.assertEqual(len(removed), 2)
        self.assertIn(files[0], removed)
        self.assertIn(files[1], removed)

        remaining = get_backup_files(dir_path)
        self.assertEqual(len(remaining), 3)
        self.assertTrue(os.path.exists(log_path))

    def test_link_file_space_efficient(self):
        src = os.path.join(self.test_dir.name, "source.tar.zst")
        dst = os.path.join(self.test_dir.name, "export", "source.tar.zst")
        with open(src, "w") as f:
            f.write("content")

        success = link_file_space_efficient(src, dst)
        self.assertTrue(success)
        self.assertTrue(os.path.exists(dst))

        src_stat = os.stat(src)
        dst_stat = os.stat(dst)
        self.assertEqual(src_stat.st_ino, dst_stat.st_ino)

        self.assertTrue(link_file_space_efficient(src, dst))


class TestBackupCollect(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.backup_dir = os.path.join(self.test_dir.name, "backups")
        self.amp_dir = os.path.join(self.test_dir.name, "amp_instances")
        os.makedirs(self.backup_dir)
        os.makedirs(self.amp_dir)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_create_directories_and_amp_links(self):
        inst_dir = os.path.join(self.amp_dir, "MyInstance", "Backups")
        os.makedirs(inst_dir)
        with open(os.path.join(inst_dir, "backup1.zip"), "w") as f:
            f.write("amp backup")

        collector = Collect(backup_dir=self.backup_dir, amp_dir=self.amp_dir)

        for b_type in collector.standard_backup_types:
            self.assertTrue(os.path.isdir(os.path.join(self.backup_dir, b_type)))

        amp_link = os.path.join(self.backup_dir, "amp", "MyInstance")
        self.assertTrue(os.path.islink(amp_link))
        self.assertEqual(os.readlink(amp_link), inst_dir)

    @patch("shutil.which")
    @patch("server_backup_scripts.backup_server.run_process_with_stdout")
    def test_mariadb_collector(self, mock_run, mock_which):
        mock_which.side_effect = lambda cmd: (
            f"/usr/bin/{cmd}" if cmd == "mariadb-dump" else None
        )
        mock_run.return_value = ("dump output", 0)

        collector = Collect(backup_dir=self.backup_dir, amp_dir=self.amp_dir)
        mariadb_dir = os.path.join(self.backup_dir, "mariadb")
        with patch(
            "server_backup_scripts.backup_server.get_datetime_str",
            return_value="20261031-120000+0000",
        ):
            expected_file = os.path.join(
                mariadb_dir, "20261031-120000+0000-mariadb.sql.zst"
            )
            with open(expected_file, "w") as f:
                f.write("sql dump")
            success = collector.mariadb()

        self.assertTrue(success)
        mock_run.assert_called_once()
        self.assertIn("mariadb-dump --all-databases", mock_run.call_args[0][0][0])

    @patch("shutil.which")
    @patch("server_backup_scripts.backup_server.run_process_with_stdout")
    def test_etc_collector(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/tar"
        mock_run.return_value = ("tar output", 0)

        collector = Collect(backup_dir=self.backup_dir, amp_dir=self.amp_dir)
        etc_dir = os.path.join(self.backup_dir, "etc")
        with patch(
            "server_backup_scripts.backup_server.get_datetime_str",
            return_value="20261031-120000+0000",
        ):
            expected_file = os.path.join(etc_dir, "20261031-120000+0000-etc.tar.zst")
            with open(expected_file, "w") as f:
                f.write("tar archive")
            success = collector.etc()

        self.assertTrue(success)
        self.assertIn("tar", mock_run.call_args[0][0][0])

    @patch("shutil.which")
    @patch("server_backup_scripts.backup_server.run_process_with_stdout")
    def test_postgres_collector(self, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/pg_dumpall"
        mock_run.return_value = ("pg output", 0)

        collector = Collect(backup_dir=self.backup_dir, amp_dir=self.amp_dir)
        pg_dir = os.path.join(self.backup_dir, "postgres")
        with patch(
            "server_backup_scripts.backup_server.get_datetime_str",
            return_value="20261031-120000+0000",
        ):
            expected_file = os.path.join(
                pg_dir, "20261031-120000+0000-postgres.sql.zst"
            )
            with open(expected_file, "w") as f:
                f.write("pg dump")
            success = collector.postgres()

        self.assertTrue(success)
        self.assertIn("pg_dumpall", mock_run.call_args[0][0][0])

    @patch("shutil.which")
    @patch("server_backup_scripts.backup_server.run_process_with_stdout")
    @patch("server_backup_scripts.backup_server.get_most_recent_files")
    def test_gitlab_collector(self, mock_recent, mock_run, mock_which):
        mock_which.return_value = "/usr/bin/gitlab-backup"
        mock_run.return_value = ("gitlab output", 0)

        src_backup = os.path.join(self.test_dir.name, "gitlab_backup.tar")
        with open(src_backup, "w") as f:
            f.write("gitlab archive")
        mock_recent.return_value = [src_backup]

        collector = Collect(backup_dir=self.backup_dir, amp_dir=self.amp_dir)
        success = collector.gitlab()

        self.assertTrue(success)
        expected_dest = os.path.join(self.backup_dir, "gitlab", "gitlab_backup.tar")
        self.assertTrue(os.path.exists(expected_dest))


class TestExportManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.backup_dir = os.path.join(self.test_dir.name, "backups")
        self.export_dir = os.path.join(self.test_dir.name, "export")
        os.makedirs(self.backup_dir)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_sync_and_prune(self):
        vw_dir = os.path.join(self.backup_dir, "vaultwarden")
        os.makedirs(vw_dir)
        b1 = os.path.join(vw_dir, "backup1.tar.zst")
        b2 = os.path.join(vw_dir, "backup2.tar.zst")
        with open(b1, "w") as f:
            f.write("b1")
        with open(b2, "w") as f:
            f.write("b2")

        exporter = ExportManager(
            backup_dir=self.backup_dir,
            export_dir=self.export_dir,
            export_user="testuser",
        )
        exporter.sync_all()

        exp_b1 = os.path.join(self.export_dir, "vaultwarden", "backup1.tar.zst")
        exp_b2 = os.path.join(self.export_dir, "vaultwarden", "backup2.tar.zst")
        self.assertTrue(os.path.exists(exp_b1))
        self.assertTrue(os.path.exists(exp_b2))

        self.assertEqual(os.stat(b1).st_ino, os.stat(exp_b1).st_ino)

        os.remove(b1)
        exporter.sync_all()

        self.assertFalse(os.path.exists(exp_b1))
        self.assertTrue(os.path.exists(exp_b2))


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.backup_dir = os.path.join(self.test_dir.name, "backups")
        os.makedirs(self.backup_dir)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_rotate_all(self):
        pg_dir = os.path.join(self.backup_dir, "postgres")
        os.makedirs(pg_dir)
        created = []
        for i in range(4):
            path = os.path.join(pg_dir, f"file_{i}.sql.zst")
            with open(path, "w") as f:
                f.write("pg")
            os.utime(path, (1000 + i * 10, 1000 + i * 10))
            created.append(path)

        rotator = Rotate(backup_dir=self.backup_dir, keep=2)
        results = rotator.rotate_all()

        self.assertEqual(len(results.get("postgres", [])), 2)
        remaining = os.listdir(pg_dir)
        self.assertEqual(len(remaining), 2)
        self.assertIn("file_3.sql.zst", remaining)
        self.assertIn("file_2.sql.zst", remaining)


class TestCliIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.backup_dir = os.path.join(self.test_dir.name, "backups")
        self.export_dir = os.path.join(self.test_dir.name, "export")

    def tearDown(self):
        self.test_dir.cleanup()

    def test_cli_skip_collect_rotation_export(self):
        runner = CliRunner()
        # Create directories and mock backups beforehand
        os.makedirs(os.path.join(self.backup_dir, "etc"))
        for i in range(4):
            f_path = os.path.join(self.backup_dir, "etc", f"etc_{i}.tar.zst")
            with open(f_path, "w") as f:
                f.write(f"etc {i}")
            os.utime(f_path, (1000 + i * 10, 1000 + i * 10))

        result = runner.invoke(
            main,
            [
                "--backup-dir",
                self.backup_dir,
                "--export-dir",
                self.export_dir,
                "--keep",
                "2",
                "--skip-collect",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        # Check rotation occurred
        remaining = os.listdir(os.path.join(self.backup_dir, "etc"))
        self.assertEqual(len(remaining), 2)
        # Check export directory created and contains remaining 2 backups
        exported = os.listdir(os.path.join(self.export_dir, "etc"))
        self.assertEqual(len(exported), 2)


if __name__ == "__main__":
    unittest.main()
