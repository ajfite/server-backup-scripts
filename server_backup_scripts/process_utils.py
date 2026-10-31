import subprocess

from loguru import logger


def run_process_with_stdout(cmd: list[str], shell: bool = False) -> tuple[str, int]:
    logger.info(f"Running {cmd}")

    if shell:
        logger.info("Shell mode is enabled for this command")

    exec_cmd = cmd[0] if shell and len(cmd) == 1 else cmd
    process = subprocess.Popen(
        exec_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=shell,
    )
    out, error = process.communicate()

    if len(error) > 0:
        logger.error(error)
    logger.info(f"Completed {cmd} with status {process.returncode}")

    if process.returncode != 0:
        logger.critical(f"Command {cmd} did not run successfully :(")

    return out, process.returncode
