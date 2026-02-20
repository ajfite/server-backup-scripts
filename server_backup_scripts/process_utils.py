import subprocess
from loguru import logger


def run_process_with_stdout(cmd: list[str]) -> tuple[str, int]:
    logger.info(f"Running {cmd}")
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    logger.info(f"Completed {cmd} with status {process.returncode}")

    if process.returncode != 0:
        logger.critical(f"Command {cmd} did not run successfully :(")

    out, error = process.communicate()
    logger.error(error)
    return out, process.returncode
