import subprocess
from loguru import logger


def run_process_with_logging(cmd: list[str]):
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    stdout, stderr = process.communicate()
    logger.info(stdout)
    logger.error(stderr)
