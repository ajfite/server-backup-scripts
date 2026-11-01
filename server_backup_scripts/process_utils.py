import os
import subprocess
import tempfile
from typing import IO, Sequence

from loguru import logger

from .file_utils import TEMP_SUFFIX

ZSTD_COMMAND: list[str] = ["zstd", "-19", "-T0", "-q", "-c"]
MAX_LOGGED_STDERR_BYTES: int = 64 * 1024


def _log_stderr(label: str, stderr: str, succeeded: bool) -> None:
    text = stderr.strip()
    if not text:
        return
    if succeeded:
        logger.warning(f"{label} stderr: {text}")
    else:
        logger.error(f"{label} stderr: {text}")


def run_process_with_stdout(cmd: list[str]) -> tuple[str, int]:
    logger.info(f"Running {cmd}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except OSError as e:
        logger.error(f"Could not start {cmd}: {e}")
        return "", 127

    out, error = process.communicate()

    if out.strip():
        logger.info(out)
    _log_stderr(cmd[0], error, process.returncode == 0)
    logger.info(f"Completed {cmd} with status {process.returncode}")

    if process.returncode != 0:
        logger.critical(f"Command {cmd} did not run successfully :(")

    return out, process.returncode


def _read_stderr_tail(stderr_file: IO[bytes]) -> str:
    size = stderr_file.seek(0, os.SEEK_END)
    stderr_file.seek(max(0, size - MAX_LOGGED_STDERR_BYTES))
    return stderr_file.read().decode(errors="replace")


def run_pipeline_to_zstd(
    producer: list[str], dest: str, ok_codes: Sequence[int] = (0,)
) -> bool:
    """
    Stream `producer` through zstd into `dest`, which only appears once complete.

    The archive is written to a private temp file, verified with `zstd -t`, then
    renamed into place. A failed run leaves nothing behind that rotation or export
    could mistake for a backup.

    Args:
        producer: Command whose stdout is compressed.
        dest: Final archive path.
        ok_codes: Producer exit codes treated as success. zstd must always exit 0.

    Returns:
        True if `dest` now holds a verified archive.
    """
    tmp = f"{dest}{TEMP_SUFFIX}"
    logger.info(f"Running {producer} | {ZSTD_COMMAND} > {tmp}")

    try:
        with (
            tempfile.TemporaryFile() as producer_err,
            tempfile.TemporaryFile() as zstd_err,
        ):
            out_fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                producer_proc = subprocess.Popen(
                    producer, stdout=subprocess.PIPE, stderr=producer_err
                )
                try:
                    zstd_proc = subprocess.Popen(
                        ZSTD_COMMAND,
                        stdin=producer_proc.stdout,
                        stdout=out_fd,
                        stderr=zstd_err,
                    )
                except OSError:
                    producer_proc.kill()
                    producer_proc.wait()
                    raise
                finally:
                    # zstd holds its own copy; ours must close so the producer
                    # gets SIGPIPE if zstd dies.
                    producer_proc.stdout.close()
                zstd_code = zstd_proc.wait()
                producer_code = producer_proc.wait()
            finally:
                os.close(out_fd)

            succeeded = producer_code in ok_codes and zstd_code == 0
            _log_stderr(producer[0], _read_stderr_tail(producer_err), succeeded)
            _log_stderr("zstd", _read_stderr_tail(zstd_err), succeeded)

        logger.info(f"Producer exited {producer_code}, zstd exited {zstd_code}")
        if not succeeded:
            logger.critical(f"Pipeline for {dest} did not run successfully :(")
            return False

        _, verify_code = run_process_with_stdout(["zstd", "-t", "-q", tmp])
        if verify_code != 0:
            logger.critical(f"Archive verification failed for {tmp}")
            return False

        os.replace(tmp, dest)
        return True
    except OSError as e:
        logger.critical(f"Pipeline for {dest} failed: {e}")
        return False
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"Could not remove temp file {tmp}: {e}")
