#!/usr/bin/env python3
import os

import click
from loguru import logger

from .process_utils import run_process_with_stdout

DEFAULT_COMPOSE_DIR: str = "/opt/docker"
# In the order docker compose itself prefers them.
COMPOSE_FILE_NAMES: tuple[str, ...] = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
REBOOT_REQUIRED_FILE: str = "/var/run/reboot-required"
APT_ENV: dict[str, str] = {
    "DEBIAN_FRONTEND": "noninteractive",
    # Restart services using replaced libraries rather than asking.
    "NEEDRESTART_MODE": "a",
}
APT_GET: list[str] = [
    "apt-get",
    # unattended-upgrades may be holding the lock when this starts.
    "-o",
    "DPkg::Lock::Timeout=600",
]
# Keep locally modified config files rather than prompting about them.
APT_UPGRADE_ARGS: list[str] = [
    "-y",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
    "upgrade",
]


@click.command()
@click.option(
    "--compose-dir",
    type=click.Path(),
    default=DEFAULT_COMPOSE_DIR,
    help="Directory whose subdirectories each hold a docker compose project",
)
@click.option("--skip-apt", is_flag=True, default=False, help="Skip apt upgrades")
@click.option(
    "--skip-compose", is_flag=True, default=False, help="Skip docker compose updates"
)
@logger.catch(reraise=True)
def main(compose_dir: str, skip_apt: bool, skip_compose: bool):
    logger.info("Running update service")
    updater = Update(compose_dir=compose_dir)

    failures: list[str] = []
    if not skip_apt and not updater.apt():
        failures.append("apt")
    if not skip_compose:
        failures.extend(updater.compose())

    if os.path.exists(REBOOT_REQUIRED_FILE):
        logger.warning("A reboot is required to finish applying these updates")

    if failures:
        logger.critical(f"Update run finished with failures: {failures}")
        raise SystemExit(1)
    logger.info("Update run finished successfully")


class Update:
    compose_dir: str

    def __init__(self, compose_dir: str = DEFAULT_COMPOSE_DIR):
        self.compose_dir = compose_dir

    def apt(self) -> bool:
        """
        Refresh the package lists and upgrade without removing any packages.

        apt-get update otherwise exits 0 when a repository cannot be reached,
        which would quietly stop updates from it. One repository being down still
        should not hold back the others, so the upgrade runs either way.
        """
        logger.info("Updating apt packages")
        succeeded = True
        _, code = run_process_with_stdout(
            [*APT_GET, "update", "--error-on=any"], env=APT_ENV
        )
        if code != 0:
            logger.error("apt-get update failed")
            succeeded = False

        _, code = run_process_with_stdout([*APT_GET, *APT_UPGRADE_ARGS], env=APT_ENV)
        if code != 0:
            logger.error("apt-get upgrade failed")
            succeeded = False
        return succeeded

    def find_compose_files(self) -> list[str]:
        """Find the compose file of each project directly under compose_dir."""
        if not os.path.isdir(self.compose_dir):
            logger.warning(f"Compose directory {self.compose_dir} does not exist")
            return []

        compose_files = []
        for entry in sorted(os.listdir(self.compose_dir)):
            for name in COMPOSE_FILE_NAMES:
                path = os.path.join(self.compose_dir, entry, name)
                if os.path.isfile(path):
                    compose_files.append(path)
                    break
        return compose_files

    def compose(self) -> list[str]:
        """
        Update every running compose project, so one failing cannot stop the others.

        Returns:
            Projects that failed.
        """
        failed = []
        for compose_file in self.find_compose_files():
            project = os.path.basename(os.path.dirname(compose_file))
            logger.info(f"Updating compose project {project}")
            if not self._update_project(compose_file, project):
                logger.error(f"Failed to update compose project {project}")
                failed.append(project)

        # Each pull of a moved tag such as :latest orphans the previous image.
        _, code = run_process_with_stdout(["docker", "image", "prune", "-f"])
        if code != 0:
            logger.warning("Could not prune old docker images")
        return failed

    def _update_project(self, compose_file: str, project: str) -> bool:
        compose = ["docker", "compose", "--progress", "quiet", "-f", compose_file]

        out, code = run_process_with_stdout([*compose, "ps", "-q"])
        if code != 0:
            return False
        if not out.strip():
            logger.info(f"Compose project {project} is not running, leaving it stopped")
            return True

        _, code = run_process_with_stdout([*compose, "pull", "--ignore-buildable"])
        if code != 0:
            return False

        # Only recreates containers whose image or configuration changed.
        _, code = run_process_with_stdout([*compose, "up", "-d"])
        return code == 0


if __name__ == "__main__":
    main()
