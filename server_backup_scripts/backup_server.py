#!/usr/bin/env python3
import click
import os

DEFAULT_BACKUP_DIR: str = "/opt/backup-management"


@click.command()
@click.option(
    "-d",
    "--backup-dir",
    type=click.Path(),
    default=DEFAULT_BACKUP_DIR,
    help="backup directory that should be managed",
)
def main(backup_dir: str):
    print(f"Hello World {backup_dir}")


class Collect:
    def __init__(self):
        self.dir_manager = DirManager()

    def gitlab(self):
        pass

    def postgres(self):
        pass

    def vaultwarden(self):
        pass

    def etc(self):
        pass


class DirManager:
    backups: list[str] = list()

    def __init__(self, backup_dir: str = DEFAULT_BACKUP_DIR):
        self.backup_dir = backup_dir

    def get_backup_dirs(self):
        for f in os.listdir(self.backup_dir):
            full_path = os.path.join(self.backup_dir, f)

            if os.path.isdir(full_path):
                self.backups.append(full_path)


if __name__ == "__main__":
    main()
