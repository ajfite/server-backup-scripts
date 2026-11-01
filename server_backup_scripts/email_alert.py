#!/usr/bin/env python3
import os
import re
import socket
import subprocess
from email.message import EmailMessage
from email.utils import formatdate
from typing import Optional

import click
from loguru import logger

# Submitted through sendmail rather than SMTP so that an alert is queued even while
# Postfix is down, and is then relayed once it is back.
SENDMAIL: str = "/usr/sbin/sendmail"
# The relay is the Outlook tenant's inbound endpoint, so the recipient must be in
# that tenant. nclf.net's SPF covers this server and its DMARC policy quarantines,
# so the sender must not fall back to the unroutable ovhalpha.localdomain.
DEFAULT_ALERT_TO: str = "aj@nclf.net"
DEFAULT_ALERT_FROM: str = "server-alerts@nclf.net"
SENDMAIL_TIMEOUT_SECONDS: int = 60

JOURNAL_FETCH_LINES: int = 2000
PROBLEM_LINES: int = 40
TAIL_LINES: int = 100
MAX_LINE_LENGTH: int = 500
PROBLEM_LINE = re.compile(r"\| (ERROR|CRITICAL) +\|")


def send_email(
    subject: str,
    body: str,
    to: str = DEFAULT_ALERT_TO,
    sender: str = DEFAULT_ALERT_FROM,
) -> bool:
    """
    Queue an email for delivery. Success means Postfix accepted it, not that it
    has been delivered.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message["Date"] = formatdate()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(body)

    try:
        result = subprocess.run(
            [SENDMAIL, "-i", "-f", sender, to],
            input=message.as_bytes(),
            capture_output=True,
            timeout=SENDMAIL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.critical(f"Could not run {SENDMAIL}: {e}")
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        logger.critical(f"sendmail exited {result.returncode}: {stderr}")
        return False
    return True


def read_journal(unit: str, invocation_id: Optional[str]) -> list[str]:
    """Read the log of one run of a unit, or its latest entries if not known."""
    selector = (
        f"_SYSTEMD_INVOCATION_ID={invocation_id}" if invocation_id else f"--unit={unit}"
    )
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--no-pager",
                "--output=short-iso",
                f"--lines={JOURNAL_FETCH_LINES}",
                selector,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SENDMAIL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"Could not read the journal: {e}"]

    if result.returncode != 0:
        return [f"Could not read the journal: {result.stderr.strip()}"]
    return [line[:MAX_LINE_LENGTH] for line in result.stdout.splitlines()]


def build_alert(unit: str, host: str) -> tuple[str, str]:
    """Build the subject and body, using the details systemd gives OnFailure units."""
    result = os.environ.get("MONITOR_SERVICE_RESULT", "unknown")
    exit_code = os.environ.get("MONITOR_EXIT_CODE", "unknown")
    exit_status = os.environ.get("MONITOR_EXIT_STATUS", "unknown")
    lines = read_journal(unit, os.environ.get("MONITOR_INVOCATION_ID"))
    problems = [line for line in lines if PROBLEM_LINE.search(line)]

    sections = [
        f"{unit} failed on {host}.\n\n"
        f"Result: {result} ({exit_code}, status {exit_status})\n"
        f"Full log: journalctl -u {unit}"
    ]
    if problems:
        sections.append(
            "Errors from this run:\n" + "\n".join(problems[-PROBLEM_LINES:])
        )
    sections.append("End of the log:\n" + "\n".join(lines[-TAIL_LINES:]))

    return f"[{host}] {unit} failed ({result})", "\n\n".join(sections) + "\n"


@click.command()
@click.option("--unit", type=str, required=True, help="The systemd unit that failed")
@click.option(
    "--to",
    "recipient",
    type=str,
    default=DEFAULT_ALERT_TO,
    help="Recipient, which must be in the mail relay's Outlook tenant",
)
@click.option(
    "--from",
    "sender",
    type=str,
    default=DEFAULT_ALERT_FROM,
    help="Sender, whose domain's SPF must allow this server",
)
@logger.catch(reraise=True)
def main(unit: str, recipient: str, sender: str):
    subject, body = build_alert(unit, socket.gethostname())
    if not send_email(subject, body, to=recipient, sender=sender):
        raise SystemExit(1)
    logger.info(f"Queued alert for {unit} to {recipient}")


if __name__ == "__main__":
    main()
