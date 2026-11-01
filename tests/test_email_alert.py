import email
import email.policy
import os
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from server_backup_scripts.email_alert import (
    DEFAULT_ALERT_FROM,
    DEFAULT_ALERT_TO,
    build_alert,
    main,
    send_email,
)

MODULE = "server_backup_scripts.email_alert"
MONITOR_ENV = {
    "MONITOR_SERVICE_RESULT": "exit-code",
    "MONITOR_EXIT_CODE": "exited",
    "MONITOR_EXIT_STATUS": "1",
    "MONITOR_INVOCATION_ID": "abc123",
}


class SendmailTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.args_path = os.path.join(tmp.name, "args")
        self.message_path = os.path.join(tmp.name, "message")
        self.sendmail = os.path.join(tmp.name, "sendmail")
        self.write_sendmail("exit 0")

    def write_sendmail(self, ending):
        with open(self.sendmail, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'echo "$@" > "{self.args_path}"\n'
                f'cat > "{self.message_path}"\n'
                f"{ending}\n"
            )
        os.chmod(self.sendmail, 0o755)
        patcher = patch(f"{MODULE}.SENDMAIL", self.sendmail)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent_args(self):
        with open(self.args_path) as f:
            return f.read().split()

    def sent_message(self):
        with open(self.message_path, "rb") as f:
            return email.message_from_binary_file(f, policy=email.policy.default)


class TestSendEmail(SendmailTestCase):
    def test_uses_explicit_envelope_and_header_sender(self):
        self.assertTrue(send_email("Subject", "Body\n"))
        self.assertEqual(
            self.sent_args(), ["-i", "-f", DEFAULT_ALERT_FROM, DEFAULT_ALERT_TO]
        )
        message = self.sent_message()
        self.assertEqual(message["From"], DEFAULT_ALERT_FROM)
        self.assertEqual(message["To"], DEFAULT_ALERT_TO)
        self.assertEqual(message["Subject"], "Subject")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertIsNotNone(message["Date"])
        self.assertEqual(message.get_content(), "Body\n")

    def test_default_sender_passes_nclf_net_spf_not_local_origin(self):
        self.assertTrue(DEFAULT_ALERT_FROM.endswith("@nclf.net"))

    def test_non_ascii_and_long_lines_survive(self):
        body = "café � " + "x" * 2000 + "\n"
        self.assertTrue(send_email("Subject", body))
        self.assertEqual(self.sent_message().get_content(), body)

    def test_sendmail_failure_is_reported(self):
        self.write_sendmail("exit 75")
        self.assertFalse(send_email("Subject", "Body"))

    def test_missing_sendmail_is_reported(self):
        with patch(f"{MODULE}.SENDMAIL", "/no/such/sendmail"):
            self.assertFalse(send_email("Subject", "Body"))


class TestBuildAlert(unittest.TestCase):
    def journal(self, lines):
        return patch(f"{MODULE}.read_journal", return_value=lines)

    def test_subject_and_details_come_from_systemd(self):
        with patch.dict(os.environ, MONITOR_ENV), self.journal([]) as journal:
            subject, body = build_alert("server-backup.service", "ovhalpha")
        self.assertEqual(subject, "[ovhalpha] server-backup.service failed (exit-code)")
        self.assertIn("Result: exit-code (exited, status 1)", body)
        self.assertIn("journalctl -u server-backup.service", body)
        journal.assert_called_once_with("server-backup.service", "abc123")

    def test_timeout_result_is_named(self):
        env = {**MONITOR_ENV, "MONITOR_SERVICE_RESULT": "timeout"}
        with patch.dict(os.environ, env), self.journal([]):
            subject, _ = build_alert("server-update.service", "ovhalpha")
        self.assertIn("(timeout)", subject)

    def test_errors_are_listed_ahead_of_the_tail(self):
        noise = [f"t backup[1]: INFO     | noise {i}" for i in range(500)]
        error = "t backup[1]: 2026 | ERROR    | mod:fn:1 - pg_dumpall broke"
        lines = [error, *noise]
        with patch.dict(os.environ, MONITOR_ENV), self.journal(lines):
            _, body = build_alert("u.service", "h")
        self.assertLess(body.index("pg_dumpall broke"), body.index("End of the log"))
        self.assertIn("noise 499", body)
        self.assertNotIn("noise 300", body)

    def test_clean_log_has_no_errors_section(self):
        with patch.dict(os.environ, MONITOR_ENV), self.journal(["t only info"]):
            _, body = build_alert("u.service", "h")
        self.assertNotIn("Errors from this run", body)

    def test_works_without_systemd_details(self):
        cleared = {k: v for k, v in os.environ.items() if not k.startswith("MONITOR_")}
        with patch.dict(os.environ, cleared, clear=True), self.journal([]) as journal:
            subject, _ = build_alert("u.service", "h")
        self.assertIn("(unknown)", subject)
        journal.assert_called_once_with("u.service", None)


class TestCli(SendmailTestCase):
    def test_sends_alert(self):
        with (
            patch.dict(os.environ, MONITOR_ENV),
            patch(f"{MODULE}.read_journal", return_value=["a log line"]),
        ):
            result = CliRunner().invoke(main, ["--unit", "server-backup.service"])
        self.assertEqual(result.exit_code, 0)
        message = self.sent_message()
        self.assertIn("server-backup.service failed", message["Subject"])
        self.assertIn("a log line", message.get_content())

    def test_recipient_can_be_overridden(self):
        with patch(f"{MODULE}.read_journal", return_value=[]):
            CliRunner().invoke(main, ["--unit", "u", "--to", "other@nclf.net"])
        self.assertEqual(self.sent_args()[-1], "other@nclf.net")

    def test_send_failure_exits_nonzero(self):
        self.write_sendmail("exit 1")
        with patch(f"{MODULE}.read_journal", return_value=[]):
            result = CliRunner().invoke(main, ["--unit", "u"])
        self.assertEqual(result.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
