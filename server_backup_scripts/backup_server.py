#!/usr/bin/env python3
import enum
import glob
import os
import pwd
import shutil
from datetime import datetime, timezone
from typing import Callable, Collection, Optional

import click
from loguru import logger

from .file_utils import (
    export_directory,
    get_most_recent_files,
    make_private_dir,
    make_private_file,
    move_file_atomic,
    open_dir_nofollow,
    open_export_dir,
    remove_stale_temp_files,
    rotate_directory_backups,
)
from .process_utils import run_pipeline_to_zstd, run_process_with_stdout

DEFAULT_BACKUP_DIR: str = "/opt/backup-management"
DEFAULT_EXPORT_DIR: str = "/home/backup-user/backups"
DEFAULT_EXPORT_USER: str = "backup-user"
DEFAULT_AMP_DIR: str = "/home/amp/.ampdata/instances"
DEFAULT_KEEP: int = 5
GITLAB_BACKUP_DIR: str = "/var/opt/gitlab/backups"
GITLAB_BACKUP_COMMAND: str = "/opt/gitlab/bin/gitlab-backup"
VAULTWARDEN_BACKUP_DIR: str = "/opt/docker/vaultwarden/vw-data"
VAULTWARDEN_SNAPSHOT_GLOB: str = "db_*.sqlite3"
VAULTWARDEN_OPTIONAL_MEMBERS: tuple[str, ...] = ("attachments", "config.json", "sends")
# tar exits 1 when files changed as they were read, which is normal on live data.
TAR_OK_CODES: tuple[int, ...] = (0, 1)
# AMP may still be writing a recently modified zip.
AMP_MIN_AGE_SECONDS: int = 300

STANDARD_BACKUP_TYPES: list[str] = [
    "gitlab",
    "postgres",
    "vaultwarden",
    "etc",
    "www",
    "mariadb",
]


class CollectResult(enum.Enum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


def get_datetime_str() -> str:
    """Return formatted UTC datetime string."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%z")


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
    type=click.IntRange(min=1),
    default=DEFAULT_KEEP,
    help="Number of backups to keep per type during rotation",
)
@click.option(
    "--amp-dir",
    type=click.Path(),
    default=DEFAULT_AMP_DIR,
    help="Path to AMP instances directory, exported but never rotated",
)
@click.option(
    "--skip-collect",
    is_flag=True,
    default=False,
    help="Skip running collectors, only rotate and sync export directory",
)
@logger.catch(reraise=True)
def main(
    backup_dir: str,
    export_dir: str,
    export_user: str,
    keep: int,
    amp_dir: str,
    skip_collect: bool,
):
    make_private_dir(backup_dir)
    logger.add(f"{backup_dir}/backup_service.log", rotation="1 MB", retention=30)
    logger.info(f"Managing backups at: {backup_dir}")

    failures: list[str] = []
    failed_types: list[str] = []
    if not skip_collect:
        failed_types = Collect(backup_dir=backup_dir).collect_all()
        failures.extend(failed_types)

    # A type that failed keeps all its backups until it next succeeds.
    rotator = Rotate(backup_dir=backup_dir, keep=keep)
    if not _run_step("rotation", lambda: rotator.rotate_all(failed_types)):
        failures.append("rotation")

    exporter = ExportManager(
        backup_dir=backup_dir,
        export_dir=export_dir,
        export_user=export_user,
        amp_dir=amp_dir,
    )
    if not _run_step("export", lambda: failures.extend(exporter.sync_all())):
        failures.append("export")

    if failures:
        logger.critical(f"Backup run finished with failures: {failures}")
        raise SystemExit(1)
    logger.info("Backup run finished successfully")


def _run_step(name: str, step: Callable[[], object]) -> bool:
    try:
        step()
        return True
    except Exception:
        logger.exception(f"{name} failed")
        return False


class Collect:
    backup_dir: str

    def __init__(self, backup_dir: str = DEFAULT_BACKUP_DIR):
        self.backup_dir = backup_dir

    def prepare(self) -> None:
        """Create private type directories and clear leftovers from earlier runs."""
        make_private_dir(self.backup_dir)
        for backup_type in STANDARD_BACKUP_TYPES:
            path = os.path.join(self.backup_dir, backup_type)
            make_private_dir(path)
            remove_stale_temp_files(path)
        self._remove_legacy_amp_links()

    def _remove_legacy_amp_links(self) -> None:
        """AMP is now read directly, so the old symlink directory is not needed."""
        amp_links_dir = os.path.join(self.backup_dir, "amp")
        if not os.path.isdir(amp_links_dir):
            return

        for entry in os.listdir(amp_links_dir):
            entry_path = os.path.join(amp_links_dir, entry)
            if os.path.islink(entry_path):
                os.unlink(entry_path)
        try:
            os.rmdir(amp_links_dir)
        except OSError as e:
            logger.warning(f"Could not remove {amp_links_dir}: {e}")

    def collect_all(self) -> list[str]:
        """
        Run every collector, so one failing cannot stop the others.

        Returns:
            Backup types that failed.
        """
        self.prepare()

        failed = []
        for backup_type in STANDARD_BACKUP_TYPES:
            collector: Callable[[], CollectResult] = getattr(self, backup_type)
            try:
                result = collector()
            except Exception:
                logger.exception(f"{backup_type} backup raised an exception")
                result = CollectResult.FAILED
            logger.info(f"{backup_type} backup result: {result.value}")
            if result is CollectResult.FAILED:
                failed.append(backup_type)
        return failed

    def _archive_path(self, backup_type: str, suffix: str) -> str:
        return os.path.join(
            self.backup_dir, backup_type, f"{get_datetime_str()}-{backup_type}{suffix}"
        )

    def gitlab(self) -> CollectResult:
        logger.info("Running Gitlab backup")
        gitlab_backup = shutil.which("gitlab-backup")
        if not gitlab_backup and os.path.exists(GITLAB_BACKUP_COMMAND):
            gitlab_backup = GITLAB_BACKUP_COMMAND
        if not gitlab_backup:
            logger.warning("gitlab-backup command not found, skipping Gitlab")
            return CollectResult.SKIPPED

        existing_backups = set(get_most_recent_files(GITLAB_BACKUP_DIR))

        _, code = run_process_with_stdout([gitlab_backup, "create"])
        if code != 0:
            logger.error("Gitlab backup creation failed")
            return CollectResult.FAILED

        new_backups = [
            f
            for f in get_most_recent_files(GITLAB_BACKUP_DIR)
            if f not in existing_backups
        ]
        if not new_backups:
            logger.error(
                f"gitlab-backup exited 0 but no new backup in {GITLAB_BACKUP_DIR}"
            )
            return CollectResult.FAILED

        # Moved rather than copied so that it is not stored twice and does not
        # accumulate in GitLab's own directory, which nothing here rotates.
        dest = os.path.join(self.backup_dir, "gitlab", os.path.basename(new_backups[0]))
        move_file_atomic(new_backups[0], dest)
        make_private_file(dest)
        logger.info(f"Moved Gitlab backup {new_backups[0]} to {dest}")
        return CollectResult.OK

    def postgres(self) -> CollectResult:
        logger.info("Running PostgreSQL backup")
        if not shutil.which("pg_dumpall"):
            logger.warning("pg_dumpall not found, skipping PostgreSQL")
            return CollectResult.SKIPPED

        backup_location = self._archive_path("postgres", ".sql.zst")
        if run_pipeline_to_zstd(
            ["sudo", "-u", "postgres", "pg_dumpall"], backup_location
        ):
            logger.info(f"Wrote postgres backup to {backup_location}")
            return CollectResult.OK

        logger.error("PostgreSQL backup failed")
        return CollectResult.FAILED

    def vaultwarden(self) -> CollectResult:
        logger.info("Backing up Vaultwarden")
        if not shutil.which("docker"):
            logger.warning("docker command not found, skipping Vaultwarden")
            return CollectResult.SKIPPED

        existing_snapshots = set(
            get_most_recent_files(
                VAULTWARDEN_BACKUP_DIR, glob_str=VAULTWARDEN_SNAPSHOT_GLOB
            )
        )

        logger.info("Generating sqlite backup")
        _, code = run_process_with_stdout(
            ["docker", "exec", "vaultwarden", "/vaultwarden", "backup"]
        )
        if code != 0:
            logger.error("Vaultwarden sqlite backup failed")
            return CollectResult.FAILED

        # Only ever a snapshot made by this run: the live db.sqlite3 and earlier
        # snapshots must be neither archived as current nor deleted below.
        new_snapshots = [
            f
            for f in get_most_recent_files(
                VAULTWARDEN_BACKUP_DIR, glob_str=VAULTWARDEN_SNAPSHOT_GLOB
            )
            if f not in existing_snapshots
        ]
        if not new_snapshots:
            logger.error("Vaultwarden backup exited 0 but wrote no new snapshot")
            return CollectResult.FAILED

        snapshot = new_snapshots[0]
        try:
            members = [
                member
                for member in VAULTWARDEN_OPTIONAL_MEMBERS
                if os.path.exists(os.path.join(VAULTWARDEN_BACKUP_DIR, member))
            ]
            members.append(os.path.basename(snapshot))
            members.extend(
                sorted(glob.glob("rsa_key*", root_dir=VAULTWARDEN_BACKUP_DIR))
            )

            backup_location = self._archive_path("vaultwarden", ".tar.zst")
            if run_pipeline_to_zstd(
                ["tar", "-c", f"--directory={VAULTWARDEN_BACKUP_DIR}", *members],
                backup_location,
                ok_codes=TAR_OK_CODES,
            ):
                logger.info(f"Backed up Vaultwarden to {backup_location}")
                return CollectResult.OK

            logger.error("Vaultwarden tar archive creation failed")
            return CollectResult.FAILED
        finally:
            try:
                os.remove(snapshot)
                logger.info(f"Removed sqlite backup at {snapshot}")
            except OSError as e:
                logger.warning(f"Failed to remove temp sqlite backup: {e}")

    def archive_directory(
        self, target_dir: str, backup_type: Optional[str] = None
    ) -> CollectResult:
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
            return CollectResult.SKIPPED

        if not os.path.isdir(target_dir):
            logger.warning(f"Directory {target_dir} does not exist, skipping {b_type}")
            return CollectResult.SKIPPED

        backup_location = self._archive_path(b_type, ".tar.zst")
        parent_dir = os.path.dirname(os.path.abspath(target_dir))
        dir_name = os.path.basename(os.path.abspath(target_dir))

        if run_pipeline_to_zstd(
            ["tar", "-c", "-C", parent_dir, dir_name],
            backup_location,
            ok_codes=TAR_OK_CODES,
        ):
            logger.info(f"Backed up {target_dir} to {backup_location}")
            return CollectResult.OK

        logger.error(f"Failed to backup {target_dir}")
        return CollectResult.FAILED

    def etc(self) -> CollectResult:
        return self.archive_directory("/etc", "etc")

    def www(self) -> CollectResult:
        return self.archive_directory("/www", "www")

    def mariadb(self) -> CollectResult:
        logger.info("Backing up MariaDB")
        dump_tool = None
        for candidate in ("mariadb-dump", "mysqldump"):
            if shutil.which(candidate):
                dump_tool = candidate
                break

        if not dump_tool:
            logger.warning("mariadb-dump or mysqldump not found, skipping MariaDB")
            return CollectResult.SKIPPED

        backup_location = self._archive_path("mariadb", ".sql.zst")
        if run_pipeline_to_zstd(
            [dump_tool, "--all-databases", "--single-transaction", "--quick"],
            backup_location,
        ):
            logger.info(f"Wrote MariaDB backup to {backup_location}")
            return CollectResult.OK

        logger.error("MariaDB backup failed")
        return CollectResult.FAILED


class Rotate:
    backup_dir: str
    keep: int

    def __init__(self, backup_dir: str, keep: int = DEFAULT_KEEP):
        self.backup_dir = backup_dir
        self.keep = keep

    def rotate_all(self, skip_types: Collection[str] = ()) -> dict[str, list[str]]:
        """
        Rotate backups across all standard types. AMP manages its own retention
        and holds the only copy of its backups, so it is deliberately left out.

        Args:
            skip_types: Backup types to leave untouched.

        Returns:
            Dictionary mapping backup type to list of removed files.
        """
        results: dict[str, list[str]] = {}
        for b_type in STANDARD_BACKUP_TYPES:
            if b_type in skip_types:
                logger.warning(f"Not rotating {b_type} because its backup failed")
                continue
            type_dir = os.path.join(self.backup_dir, b_type)
            if os.path.isdir(type_dir):
                results[b_type] = rotate_directory_backups(type_dir, self.keep)

        return results


class ExportManager:
    backup_dir: str
    export_dir: str
    export_user: Optional[str]
    amp_dir: str

    def __init__(
        self,
        backup_dir: str,
        export_dir: str,
        export_user: Optional[str] = DEFAULT_EXPORT_USER,
        amp_dir: str = DEFAULT_AMP_DIR,
    ):
        self.backup_dir = backup_dir
        self.export_dir = export_dir
        self.export_user = export_user
        self.amp_dir = amp_dir
        self._root_uid: Optional[int] = None
        self._export_uid: Optional[int] = None
        self._export_gid: Optional[int] = None

    def _resolve_export_user(self) -> None:
        """Ownership can only be applied as root, and the user must then exist."""
        if self.export_user and os.geteuid() == 0:
            pw = pwd.getpwnam(self.export_user)
            self._root_uid = 0
            self._export_uid = pw.pw_uid
            self._export_gid = pw.pw_gid

    def sync_all(self) -> list[str]:
        """
        Export all active backups into export_dir using hard links, pruning
        rotated ones. The export is owned by root and readable by the export
        user's group, so that user can neither alter the backups nor have root
        act on symlinks it planted.

        Returns:
            Export areas that had failures.
        """
        self._resolve_export_user()

        export_path = os.path.abspath(self.export_dir)
        os.makedirs(os.path.dirname(export_path), exist_ok=True)
        export_fd = open_export_dir(
            export_path, uid=self._root_uid, gid=self._export_gid
        )
        try:
            failures = []
            for b_type in STANDARD_BACKUP_TYPES:
                if not self._sync_type(b_type, export_fd):
                    failures.append(f"export:{b_type}")
            if not self._sync_amp(export_fd):
                failures.append("export:amp")
            return failures
        finally:
            os.close(export_fd)

    def _sync_type(self, b_type: str, export_fd: int) -> bool:
        src_type_dir = os.path.join(self.backup_dir, b_type)
        if not os.path.isdir(src_type_dir):
            return True

        try:
            src_fd = open_dir_nofollow(src_type_dir)
            try:
                dst_fd = open_export_dir(
                    b_type, export_fd, self._root_uid, self._export_gid
                )
                try:
                    return (
                        export_directory(
                            src_fd, dst_fd, self._root_uid, self._export_gid
                        )
                        == 0
                    )
                finally:
                    os.close(dst_fd)
            finally:
                os.close(src_fd)
        except OSError as e:
            logger.error(f"Failed to export {b_type}: {e}")
            return False

    def _sync_amp(self, export_fd: int) -> bool:
        if not os.path.isdir(self.amp_dir):
            logger.info(f"AMP instances dir not found at {self.amp_dir}, skipping")
            return True

        try:
            amp_fd = os.open(self.amp_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                dst_amp_fd = open_export_dir(
                    "amp", export_fd, self._root_uid, self._export_gid
                )
                try:
                    return self._sync_amp_instances(amp_fd, dst_amp_fd)
                finally:
                    os.close(dst_amp_fd)
            finally:
                os.close(amp_fd)
        except OSError as e:
            logger.error(f"Failed to export AMP backups: {e}")
            return False

    def _sync_amp_instances(self, amp_fd: int, dst_amp_fd: int) -> bool:
        succeeded = True
        active_instances = set()
        for instance in os.listdir(amp_fd):
            try:
                instance_fd = open_dir_nofollow(instance, dir_fd=amp_fd)
            except OSError:
                continue
            try:
                backups_fd = open_dir_nofollow("Backups", dir_fd=instance_fd)
            except OSError:
                continue
            finally:
                os.close(instance_fd)

            active_instances.add(instance)
            try:
                if not self._sync_amp_instance(instance, backups_fd, dst_amp_fd):
                    succeeded = False
            except OSError as e:
                logger.error(f"Failed to export AMP instance {instance}: {e}")
                succeeded = False
            finally:
                os.close(backups_fd)

        if not active_instances:
            logger.warning("No AMP instances found, leaving the export as it is")
            return succeeded

        for existing in os.listdir(dst_amp_fd):
            if existing in active_instances:
                continue
            try:
                shutil.rmtree(existing, dir_fd=dst_amp_fd)
                logger.info(f"Pruned deleted AMP instance from export: {existing}")
            except OSError as e:
                logger.error(f"Could not prune AMP instance {existing}: {e}")
                succeeded = False
        return succeeded

    def _sync_amp_instance(
        self, instance: str, backups_fd: int, dst_amp_fd: int
    ) -> bool:
        """
        AMP's directories are controlled by its own, less trusted user. Only
        files owned by that user are exported, and it keeps owning them.
        """
        amp_uid = os.fstat(backups_fd).st_uid
        if amp_uid == 0:
            logger.error(f"AMP instance {instance}: Backups is owned by root, refusing")
            return False

        # Earlier versions chowned AMP's zips to the export user; this undoes that.
        allowed_uids = {amp_uid}
        if self._export_uid is not None:
            allowed_uids.add(self._export_uid)

        dst_fd = open_export_dir(instance, dst_amp_fd, self._root_uid, self._export_gid)
        try:
            return (
                export_directory(
                    backups_fd,
                    dst_fd,
                    amp_uid if self._root_uid is not None else None,
                    self._export_gid,
                    allowed_uids=allowed_uids,
                    min_age_seconds=AMP_MIN_AGE_SECONDS,
                )
                == 0
            )
        finally:
            os.close(dst_fd)


if __name__ == "__main__":
    main()
