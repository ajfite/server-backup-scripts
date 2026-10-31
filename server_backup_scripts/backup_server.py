#!/usr/bin/env python3
import glob
import os
import shlex
import shutil
import time
from typing import Optional

import click
from loguru import logger

from .file_utils import (
    get_backup_files,
    get_most_recent_files,
    link_file_space_efficient,
    rotate_directory_backups,
    set_permissions_and_owner,
)
from .process_utils import run_process_with_stdout

DEFAULT_BACKUP_DIR: str = "/opt/backup-management"
DEFAULT_EXPORT_DIR: str = "/home/backup-user/backups"
DEFAULT_EXPORT_USER: str = "backup-user"
DEFAULT_AMP_DIR: str = "/home/amp/.ampdata/instances"
DEFAULT_KEEP: int = 5
GITLAB_BACKUP_DIR: str = "/var/opt/gitlab/backups"
VAULTWARDEN_BACKUP_DIR: str = "/opt/docker/vaultwarden/vw-data"

STANDARD_BACKUP_TYPES: list[str] = [
    "gitlab",
    "postgres",
    "vaultwarden",
    "etc",
    "www",
    "mariadb",
]
SPECIAL_BACKUP_TYPES: list[str] = [
    "amp",
]


def get_datetime_str() -> str:
    """Return formatted UTC datetime string."""
    return time.strftime("%Y%m%d-%H%M%S%z", time.gmtime())


DATETIME_STR: str = get_datetime_str()


@click.command()
@click.option(
    "-d",
    "--backup-dir",
    type=click.Path(),
    default=DEFAULT_BACKUP_DIR,
    help="Backup directory that should be managed",
)
@click.option(
    "-e",
    "--export-dir",
    type=click.Path(),
    default=DEFAULT_EXPORT_DIR,
    help="Directory where backups are exported for non-root user",
)
@click.option(
    "-u",
    "--export-user",
    type=str,
    default=DEFAULT_EXPORT_USER,
    help="Non-root user who needs access to the exported backups",
)
@click.option(
    "-k",
    "--keep",
    type=int,
    default=DEFAULT_KEEP,
    help="Number of backups to keep per type during rotation",
)
@click.option(
    "--amp-dir",
    type=click.Path(),
    default=DEFAULT_AMP_DIR,
    help="Path to AMP instances directory",
)
@click.option(
    "--skip-collect",
    is_flag=True,
    default=False,
    help="Skip running collectors, only rotate and sync export directory",
)
@logger.catch
def main(
    backup_dir: str,
    export_dir: str,
    export_user: str,
    keep: int,
    amp_dir: str,
    skip_collect: bool,
):
    logger.add(f"{backup_dir}/backup_service.log", rotation="100 KB")
    logger.info(f"Managing backups at: {backup_dir}")

    collector = Collect(backup_dir=backup_dir, amp_dir=amp_dir)
    if not skip_collect:
        collector.gitlab()
        collector.postgres()
        collector.vaultwarden()
        collector.etc()
        collector.www()
        collector.mariadb()
        collector.amp()

    rotator = Rotate(backup_dir=backup_dir, keep=keep)
    rotator.rotate_all()

    exporter = ExportManager(
        backup_dir=backup_dir,
        export_dir=export_dir,
        export_user=export_user,
    )
    exporter.sync_all()


class Collect:
    backup_dir: str
    amp_dir: str
    standard_backup_types: list[str] = STANDARD_BACKUP_TYPES
    special_backup_types: list[str] = SPECIAL_BACKUP_TYPES

    def __init__(
        self,
        backup_dir: str = DEFAULT_BACKUP_DIR,
        amp_dir: str = DEFAULT_AMP_DIR,
    ):
        self.backup_dir = backup_dir
        self.amp_dir = amp_dir
        self._create_directories()
        self._init_amp_links()

    def _init_amp_links(self):
        """
        Assemble list of amp instances, link each Backups directory into
        self.backup_dir/amp, and prune broken links.
        """
        amp_target_dir = os.path.join(self.backup_dir, "amp")
        if not os.path.isdir(self.amp_dir):
            logger.info(
                f"AMP instances dir not found at {self.amp_dir}, skipping link init"
            )
            return

        for instance_name in os.listdir(self.amp_dir):
            instance_path = os.path.join(self.amp_dir, instance_name)
            backups_path = os.path.join(instance_path, "Backups")
            if os.path.isdir(backups_path):
                link_path = os.path.join(amp_target_dir, instance_name)
                if os.path.islink(link_path):
                    if os.readlink(link_path) == backups_path:
                        continue
                    os.unlink(link_path)
                elif os.path.exists(link_path):
                    logger.warning(f"Non-symlink file exists at {link_path}, skipping")
                    continue

                try:
                    os.symlink(backups_path, link_path)
                    logger.info(f"Linked AMP backups: {link_path} -> {backups_path}")
                except OSError as e:
                    logger.error(
                        f"Failed to symlink AMP backups for {instance_name}: {e}"
                    )

        if os.path.isdir(amp_target_dir):
            for entry in os.listdir(amp_target_dir):
                entry_path = os.path.join(amp_target_dir, entry)
                if os.path.islink(entry_path) and not os.path.exists(entry_path):
                    logger.info(f"Removing stale AMP backup link: {entry_path}")
                    os.unlink(entry_path)

    def _create_directories(self):
        all_types = self.standard_backup_types + self.special_backup_types
        for backup_type in all_types:
            path = f"{self.backup_dir}/{backup_type}"

            if os.path.exists(path):
                if os.path.isdir(path):
                    logger.info(f"Directory for {backup_type} already exists :)")
                else:
                    logger.critical(f"{path} exists but it is not a directory")
                    logger.critical(
                        f"Failed to create backup directory for {backup_type}"
                    )
                    exit(100)
            else:
                logger.info(f"Creating backup path for {backup_type}")
                os.makedirs(path, exist_ok=True)

    def gitlab(self) -> bool:
        logger.info("Running Gitlab backup")
        if not shutil.which("gitlab-backup") and not os.path.exists(
            "/opt/gitlab/bin/gitlab-backup"
        ):
            logger.warning("gitlab-backup command not found, skipping Gitlab")
            return False

        log, code = run_process_with_stdout(["gitlab-backup", "create"])
        logger.info(log)
        if code != 0:
            logger.error("Gitlab backup creation failed")
            return False

        backups = get_most_recent_files(GITLAB_BACKUP_DIR, 1)
        if not backups:
            logger.error(f"No Gitlab backup files found in {GITLAB_BACKUP_DIR}")
            return False

        dest = os.path.join(self.backup_dir, "gitlab")
        copied_file = shutil.copy(backups[0], dest)
        set_permissions_and_owner(copied_file, mode=0o644)
        logger.info(f"Copied Gitlab backup {backups[0]} to {dest}")
        return True

    def postgres(self) -> bool:
        logger.info("Running PostgreSQL backup")
        if not shutil.which("pg_dumpall"):
            logger.warning("pg_dumpall not found, skipping PostgreSQL")
            return False

        ts = get_datetime_str()
        backup_location = f"{self.backup_dir}/postgres/{ts}-postgres.sql.zst"
        out, code = run_process_with_stdout(
            [f"sudo -u postgres pg_dumpall | zstd -19 -T0 -o {backup_location}"],
            shell=True,
        )
        logger.info(out)
        if code == 0 and os.path.exists(backup_location):
            set_permissions_and_owner(backup_location, mode=0o644)
            logger.info(f"Wrote postgres backup to {backup_location}")
            return True
        logger.error("PostgreSQL backup failed")
        return False

    def vaultwarden(self) -> bool:
        logger.info("Backing up Vaultwarden")
        if not shutil.which("docker"):
            logger.warning("docker command not found, skipping Vaultwarden")
            return False

        logger.info("Generating sqlite backup")
        out, code = run_process_with_stdout(
            ["docker", "exec", "vaultwarden", "/vaultwarden", "backup"]
        )
        logger.info(out)
        if code != 0:
            logger.error("Vaultwarden sqlite backup failed")
            return False

        try:
            logger.info("Creating Vaultwarden sends directory in case it doesn't exist")
            os.makedirs(f"{VAULTWARDEN_BACKUP_DIR}/sends", exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create sends directory: {e}")

        ts = get_datetime_str()
        vaultwarden_backup_file = (
            f"{self.backup_dir}/vaultwarden/{ts}-vaultwarden.tar.zst"
        )

        sqlite_backup = get_most_recent_files(
            count=1, path=VAULTWARDEN_BACKUP_DIR, glob_str="*.sqlite3"
        )
        if not sqlite_backup:
            logger.error("No Vaultwarden sqlite file found to archive")
            return False

        tar_args = [
            "tar",
            "-cv",
            f"--directory={VAULTWARDEN_BACKUP_DIR}",
            "attachments",
            "config.json",
            "sends",
            sqlite_backup[0],
        ]
        tar_args.extend(
            glob.glob("rsa_key*", recursive=True, root_dir=VAULTWARDEN_BACKUP_DIR)
        )
        cmd_str = f"{shlex.join(tar_args)} | zstd -19 -T0 -o {vaultwarden_backup_file}"

        out, tar_code = run_process_with_stdout([cmd_str], shell=True)
        logger.info(out)
        try:
            os.remove(sqlite_backup[0])
            logger.info(f"Removed sqlite backup at {sqlite_backup[0]}")
        except OSError as e:
            logger.warning(f"Failed to remove temp sqlite backup: {e}")

        if tar_code == 0 and os.path.exists(vaultwarden_backup_file):
            set_permissions_and_owner(vaultwarden_backup_file, mode=0o644)
            logger.info(f"Backed up Vaultwarden to {vaultwarden_backup_file}")
            return True

        logger.error("Vaultwarden tar archive creation failed")
        return False

    def archive_directory(
        self, target_dir: str, backup_type: Optional[str] = None
    ) -> bool:
        """
        Archive a directory using tar and zstd compression.
        """
        b_type = (
            backup_type
            if backup_type is not None
            else os.path.basename(target_dir.rstrip("/"))
        )
        logger.info(f"Backing up {target_dir} ({b_type})")

        if not shutil.which("tar"):
            logger.warning(f"tar not found, skipping {b_type}")
            return False

        if not os.path.isdir(target_dir):
            logger.warning(f"Directory {target_dir} does not exist, skipping {b_type}")
            return False

        ts = get_datetime_str()
        backup_location = f"{self.backup_dir}/{b_type}/{ts}-{b_type}.tar.zst"
        parent_dir = os.path.dirname(os.path.abspath(target_dir))
        dir_name = os.path.basename(os.path.abspath(target_dir))

        cmd = [
            f"tar -cv -C {parent_dir} {dir_name} | zstd -19 -T0 -o {backup_location}"
        ]
        out, code = run_process_with_stdout(cmd, shell=True)
        logger.info(out)
        # tar exit code 1 indicates some files changed as read, which is
        # normal on live server directories. Code >= 2 is a fatal error.
        if code in (0, 1) and os.path.exists(backup_location):
            set_permissions_and_owner(backup_location, mode=0o644)
            logger.info(f"Backed up {target_dir} to {backup_location}")
            return True

        logger.error(f"Failed to backup {target_dir}, code {code}")
        return False

    def etc(self) -> bool:
        return self.archive_directory("/etc", "etc")

    def www(self) -> bool:
        return self.archive_directory("/www", "www")

    def mariadb(self) -> bool:
        logger.info("Backing up MariaDB")
        dump_tool = None
        for candidate in ("mariadb-dump", "mysqldump"):
            if shutil.which(candidate):
                dump_tool = candidate
                break

        if not dump_tool:
            logger.warning("mariadb-dump or mysqldump not found, skipping MariaDB")
            return False

        ts = get_datetime_str()
        backup_location = f"{self.backup_dir}/mariadb/{ts}-mariadb.sql.zst"
        cmd_str = (
            f"{dump_tool} --all-databases --single-transaction --quick "
            f"| zstd -19 -T0 -o {backup_location}"
        )
        out, code = run_process_with_stdout([cmd_str], shell=True)
        logger.info(out)
        if code == 0 and os.path.exists(backup_location):
            set_permissions_and_owner(backup_location, mode=0o644)
            logger.info(f"Wrote MariaDB backup to {backup_location}")
            return True

        logger.error("MariaDB backup failed")
        return False

    def amp(self):
        logger.info("Checking AMP instances and backups")
        self._init_amp_links()


class Rotate:
    backup_dir: str
    keep: int
    standard_backup_types: list[str] = STANDARD_BACKUP_TYPES

    def __init__(self, backup_dir: str, keep: int = DEFAULT_KEEP):
        self.backup_dir = backup_dir
        self.keep = keep

    def rotate_all(self) -> dict[str, list[str]]:
        """
        Rotate backups across all standard types and AMP instances.

        Returns:
            Dictionary mapping backup type to list of removed files.
        """
        results: dict[str, list[str]] = {}
        for b_type in self.standard_backup_types:
            type_dir = os.path.join(self.backup_dir, b_type)
            if os.path.isdir(type_dir):
                removed = rotate_directory_backups(type_dir, self.keep)
                results[b_type] = removed

        # Rotate AMP instances
        amp_dir = os.path.join(self.backup_dir, "amp")
        if os.path.isdir(amp_dir):
            for entry in os.listdir(amp_dir):
                instance_dir = os.path.join(amp_dir, entry)
                if os.path.isdir(instance_dir):
                    removed = rotate_directory_backups(
                        instance_dir, self.keep, extensions=(".zip",)
                    )
                    results[f"amp/{entry}"] = removed

        return results


class ExportManager:
    backup_dir: str
    export_dir: str
    export_user: Optional[str]
    standard_backup_types: list[str] = STANDARD_BACKUP_TYPES

    def __init__(
        self,
        backup_dir: str,
        export_dir: str,
        export_user: Optional[str] = DEFAULT_EXPORT_USER,
    ):
        self.backup_dir = backup_dir
        self.export_dir = export_dir
        self.export_user = export_user

    def sync_all(self) -> None:
        """
        Export all active backups into export_dir using hard links.
        Prunes old/rotated backups from export_dir and ensures proper
        permissions for the non-root user.
        """
        os.makedirs(self.export_dir, exist_ok=True)
        set_permissions_and_owner(self.export_dir, mode=0o755, user=self.export_user)

        for b_type in self.standard_backup_types:
            src_type_dir = os.path.join(self.backup_dir, b_type)
            dst_type_dir = os.path.join(self.export_dir, b_type)
            if not os.path.isdir(src_type_dir):
                continue

            os.makedirs(dst_type_dir, exist_ok=True)
            set_permissions_and_owner(dst_type_dir, mode=0o755, user=self.export_user)

            current_backups = get_backup_files(src_type_dir)
            active_basenames = set()
            for src_file in current_backups:
                basename = os.path.basename(src_file)
                active_basenames.add(basename)
                dst_file = os.path.join(dst_type_dir, basename)
                if link_file_space_efficient(src_file, dst_file):
                    set_permissions_and_owner(
                        dst_file, mode=0o644, user=self.export_user
                    )

            # Prune removed backups from export dir
            for existing_name in os.listdir(dst_type_dir):
                if existing_name not in active_basenames:
                    dst_path = os.path.join(dst_type_dir, existing_name)
                    try:
                        os.remove(dst_path)
                        logger.info(f"Pruned rotated backup from export: {dst_path}")
                    except OSError as e:
                        logger.warning(f"Could not prune {dst_path}: {e}")

        # Export AMP instances
        src_amp_dir = os.path.join(self.backup_dir, "amp")
        dst_amp_dir = os.path.join(self.export_dir, "amp")
        if os.path.isdir(src_amp_dir):
            os.makedirs(dst_amp_dir, exist_ok=True)
            set_permissions_and_owner(dst_amp_dir, mode=0o755, user=self.export_user)

            active_instances = set()
            for entry in os.listdir(src_amp_dir):
                src_inst = os.path.join(src_amp_dir, entry)
                if not os.path.isdir(src_inst):
                    continue

                active_instances.add(entry)
                dst_inst = os.path.join(dst_amp_dir, entry)
                os.makedirs(dst_inst, exist_ok=True)
                set_permissions_and_owner(dst_inst, mode=0o755, user=self.export_user)

                current_zips = get_backup_files(src_inst, extensions=(".zip",))
                active_zips = set()
                for zip_file in current_zips:
                    basename = os.path.basename(zip_file)
                    active_zips.add(basename)
                    dst_zip = os.path.join(dst_inst, basename)
                    if link_file_space_efficient(zip_file, dst_zip):
                        set_permissions_and_owner(
                            dst_zip, mode=0o644, user=self.export_user
                        )

                # Prune removed backups for this instance
                for existing_name in os.listdir(dst_inst):
                    if existing_name not in active_zips:
                        dst_path = os.path.join(dst_inst, existing_name)
                        try:
                            os.remove(dst_path)
                            logger.info(
                                f"Pruned rotated AMP backup from export: {dst_path}"
                            )
                        except OSError as e:
                            logger.warning(f"Could not prune {dst_path}: {e}")

            # Prune obsolete instance dirs in export/amp
            for existing_inst in os.listdir(dst_amp_dir):
                if existing_inst not in active_instances:
                    stale_dir = os.path.join(dst_amp_dir, existing_inst)
                    try:
                        shutil.rmtree(stale_dir)
                        logger.info(
                            f"Pruned deleted AMP instance directory: {stale_dir}"
                        )
                    except OSError as e:
                        logger.warning(f"Could not prune {stale_dir}: {e}")


if __name__ == "__main__":
    main()
