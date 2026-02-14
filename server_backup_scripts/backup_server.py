#!/usr/bin/env python3
import os

import click
from loguru import logger
import shutil

from .file_utils import get_most_recent_files
from .process_utils import run_process_with_logging

DEFAULT_BACKUP_DIR: str = "/opt/backup-management"
GITLAB_BACKUP_DIR: str = "/var/opt/gitlab/backups"


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
    collector.gitlab()
    collector.postgres()
    collector.vaultwarden()
    collector.etc()
    collector.mariadb()


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
        run_process_with_logging(["gitlab-backup", "create"])
        backup = get_most_recent_files(GITLAB_BACKUP_DIR, 1)
        shutil.copy(backup[0], f"{self.backup_dir}/gitlab")

    def postgres(self):
        pass

    def vaultwarden(self):
        pass

    def etc(self):
        pass

    def mariadb(self):
        pass

    def amp(self):
        pass


if __name__ == "__main__":
    main()
