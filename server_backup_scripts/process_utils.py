import os
import subprocess
import tempfile
from typing import IO, Mapping, Optional, Sequence

from loguru import logger

from .file_utils import TEMP_SUFFIX

ZSTD_COMMAND: list[str] = ["zstd", "-19", "-T0", "-q", "-c"]
ZSTD_VERIFY_COMMAND: list[str] = ["zstd", "-t", "-q"]
# Followed by the recipients file. age reads stdin and writes to stdout.
AGE_COMMAND: list[str] = ["age", "--encrypt", "--recipients-file"]
PIPE_CHUNK_BYTES: int = 1024 * 1024
MAX_LOGGED_STDERR_BYTES: int = 64 * 1024


def _log_stderr(label: str, stderr: str, succeeded: bool) -> None:
    text = stderr.strip()
    if not text:
        return
    if succeeded:
        logger.warning(f"{label} stderr: {text}")
    else:
        logger.error(f"{label} stderr: {text}")


def run_process_with_stdout(
    cmd: list[str], env: Optional[Mapping[str, str]] = None
) -> tuple[str, int]:
    logger.info(f"Running {cmd}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**os.environ, **env} if env else None,
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


def _tee(source: IO[bytes], sinks: Sequence[IO[bytes]]) -> bool:
    """
    Copy `source` to every sink, then close them all.

    Returns:
        False if a sink stopped reading before the end.
    """
    copied = True
    try:
        while chunk := source.read(PIPE_CHUNK_BYTES):
            for sink in sinks:
                sink.write(chunk)
    except BrokenPipeError:
        copied = False
    finally:
        # Closing the source as well lets the upstream processes see SIGPIPE and
        # exit if a sink died part way.
        source.close()
        for sink in sinks:
            try:
                sink.close()
            except BrokenPipeError:
                copied = False
    return copied


def run_encrypted_pipeline(
    producer: list[str],
    dest: str,
    recipients_file: str,
    ok_codes: Sequence[int] = (0,),
) -> bool:
    """
    Stream `producer` through zstd and age into `dest`, which only appears once
    complete.

    age encrypts to the public keys in `recipients_file`, so nothing on this host can
    read the archive back. The compressed stream is therefore tested with `zstd -t`
    as it is written instead of afterwards. The archive goes to a private temp file
    and is renamed into place only if every stage succeeded, so a failed run leaves
    nothing behind that rotation or export could mistake for a backup.

    Args:
        producer: Command whose stdout is compressed.
        dest: Final archive path.
        recipients_file: age recipients file holding the public keys to encrypt to.
        ok_codes: Producer exit codes treated as success. Every other stage must
            always exit 0.

    Returns:
        True if `dest` now holds a complete, verified and encrypted archive.
    """
    tmp = f"{dest}{TEMP_SUFFIX}"
    age_command = [*AGE_COMMAND, recipients_file]
    logger.info(f"Running {producer} | {ZSTD_COMMAND} | {age_command} > {tmp}")

    try:
        with (
            tempfile.TemporaryFile() as producer_err,
            tempfile.TemporaryFile() as zstd_err,
            tempfile.TemporaryFile() as age_err,
            tempfile.TemporaryFile() as verify_err,
        ):
            out_fd = os.open(
                tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            procs: list[subprocess.Popen] = []

            def start(cmd: list[str], **kwargs) -> subprocess.Popen:
                proc = subprocess.Popen(cmd, **kwargs)
                procs.append(proc)
                return proc

            try:
                try:
                    producer_proc = start(
                        producer, stdout=subprocess.PIPE, stderr=producer_err
                    )
                    try:
                        zstd_proc = start(
                            ZSTD_COMMAND,
                            stdin=producer_proc.stdout,
                            stdout=subprocess.PIPE,
                            stderr=zstd_err,
                        )
                    finally:
                        # zstd holds its own copy; ours must close so the producer
                        # gets SIGPIPE if zstd dies.
                        producer_proc.stdout.close()
                    age_proc = start(
                        age_command,
                        stdin=subprocess.PIPE,
                        stdout=out_fd,
                        stderr=age_err,
                    )
                    verify_proc = start(
                        ZSTD_VERIFY_COMMAND,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=verify_err,
                    )
                except OSError:
                    for proc in procs:
                        for stream in (proc.stdin, proc.stdout):
                            if stream:
                                stream.close()
                        proc.kill()
                        proc.wait()
                    raise

                copied = _tee(zstd_proc.stdout, [age_proc.stdin, verify_proc.stdin])
                codes = {
                    "producer": producer_proc.wait(),
                    "zstd": zstd_proc.wait(),
                    "age": age_proc.wait(),
                    "zstd -t": verify_proc.wait(),
                }
            finally:
                os.close(out_fd)

            succeeded = (
                copied
                and codes["producer"] in ok_codes
                and all(code == 0 for name, code in codes.items() if name != "producer")
            )
            _log_stderr(producer[0], _read_stderr_tail(producer_err), succeeded)
            _log_stderr("zstd", _read_stderr_tail(zstd_err), succeeded)
            _log_stderr("age", _read_stderr_tail(age_err), succeeded)
            _log_stderr("zstd -t", _read_stderr_tail(verify_err), succeeded)

        logger.info(f"Pipeline exit codes: {codes}")
        if not succeeded:
            logger.critical(f"Pipeline for {dest} did not run successfully :(")
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
