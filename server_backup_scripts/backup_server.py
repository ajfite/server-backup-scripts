#!/usr/bin/env python3
import zstd
import os
import time

import click
from loguru import logger
import shutil

from .file_utils import get_most_recent_files
from .process_utils import run_process_with_stdout

DEFAULT_BACKUP_DIR: str = "/opt/backup-management"
GITLAB_BACKUP_DIR: str = "/var/opt/gitlab/backups"
VAULTWARDEN_BACKUP_DIR: str = "/opt/docker/vaultwarden/vw-data"
DATETIME_STR: str = time.strftime("%Y%m%d-%H%M%S%z", time.gmtime())


# TODO: Big caveat - I'm currently assuming that the commands just work
# aside from logging failures I'm doing nothing about them
# Should handle and alert failures, email probably
@click.command()
@click.option(
    "-d",
    "--backup-dir",
    type=click.Path(),
    default=DEFAULT_BACKUP_DIR,
    help="backup directory that should be managed",
)
@logger.catch
def main(backup_dir: str):
    logger.add(f"{backup_dir}/backup_service.log", rotation="100 KB")
    logger.info(f"Managing backups at@: {backup_dir}")

    collector = Collect(backup_dir)
    #collector.gitlab()
    #collector.postgres()
    collector.vaultwarden()
    collector.etc()
    collector.mariadb()
    collector.amp()


class Collect:
    backup_dir: str
    backups: list[str] = []
    standard_backup_types: list[str] = [
        "gitlab",
        "postgres",
        "vaultwarden",
        "etc",
        "mariadb",
    ]
    special_backup_types: list[str] = [
        "amp",
    ]

    def __init__(self, backup_dir: str):
        self.backup_dir = backup_dir
        self._create_directories()
        self._init_amp_links()

    def _init_amp_links(self):
        # Assemble list of amp instances
        # Go into each amp instance and link the Backups directory
        # Delete any links for instances that don't exist
        pass

    def _create_directories(self):
        for backup_type in self.standard_backup_types + self.special_backup_types:
            path = f"{self.backup_dir}/{backup_type}"

            # Check for backup path existence, create it if it doesn't exist
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
                os.mkdir(f"{self.backup_dir}/{backup_type}")

    def gitlab(self):
        logger.info("Running Gitlab backup")
        # force a manual backup
        log, _ = run_process_with_stdout(["gitlab-backup", "create"])
        logger.info(log)
        backup = get_most_recent_files(GITLAB_BACKUP_DIR, 1)
        shutil.copy(backup[0], f"{self.backup_dir}/gitlab")

    def postgres(self):
        # pg_dumpall doesn't require the server to be stopped per docs
        # to get a good backup
        backup_location = f"{self.backup_dir}/postgres/{DATETIME_STR}-postgres.sql.zst"
        out, _ = run_process_with_stdout(
            [
                f"sudo -u postgres pg_dumpall | zstd -o {backup_location}",
            ],
            True,
        )
        logger.info(out)
        logger.info(f"Wrote postgres backup to {backup_location}")

    def vaultwarden(self):
        # Backup database
        # docker exec -it vaultwarden /vaultwarden backup
        logger.info("Backing up Vaultwarden")
        logger.info("Generating sqlite backup")
        out, _ = run_process_with_stdout(
            ["docker", "exec", "-it", "vaultwarden", "/vaultwarden", "backup"]
        )
        logger.info(out)

        sqlite_backup = get_most_recent_files(
            count=1, path=VAULTWARDEN_BACKUP_DIR, glob_str="*.sqlite3"
        )

        with open(sqlite_backup[0], "rb") as f:
            compressed = zstd.compress(f.read())
            with open(
                f"{self.backup_dir}/vaultwarden/{DATETIME_STR}-vaultwarden.sqlite3.zst",
                "wb",
            ) as g:
                g.write(compressed)
                logger.info(f"Wrote Vaultwarden backup to {g.name}")

        os.remove(sqlite_backup[0])

        try:
            logger.info("Creating Vaultwarden sends directory in case it doesn't exist")
            os.mkdir(f"{VAULTWARDEN_BACKUP_DIR}/sends")
        except FileExistsError:
            logger.info("Sends directory exists!")

        # Backup attachments dir
        # Backup sends dir
        # Backup config.json
        # Backup rsa_key*
        vaultwarden_backup_file = (
            f"{self.backup_dir}/vaultwarden/{DATETIME_STR}-vaultwarden.tar.zst"
        )
        out, _ = run_process_with_stdout(
            [
                "tar",
                "-cv",
                "--zstd",
                f"--file={vaultwarden_backup_file}",
                f"--directory={VAULTWARDEN_BACKUP_DIR}",
                "attachments",
                "config.json",
                "rsa_key*",
                "sends",
            ]
        )
        logger.info(out)
        logger.info(f"backed up Vaultwarden to {vaultwarden_backup_file}")

    def etc(self):
        pass

    def mariadb(self):
        pass

    def amp(self):
        pass


if __name__ == "__main__":
    main()
