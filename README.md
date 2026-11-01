# Server Backup Scripts

A python script set for managing backups on my server.

## Deploying

Install or upgrade the `backup` and `update` commands as root, install the units,
and enable only the timers:

```sh
sudo pipx install --force git+https://git.nclf.net/ajfite/server-backup-scripts.git
sudo cp systemd/server-*.service systemd/server-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-backup.timer server-update.timer
```

## Behaviour

- `backup` exits non-zero if any collector, rotation or export step fails, so the
  failure shows on `server-backup.service`. Missing tools or directories are
  logged as skips and are not failures.
- A backup type that failed is not rotated that run.
- `/opt/backup-management` is private to root. `backup-user` reads backups through
  the root-owned export directory.
- GitLab backups are moved out of `/var/opt/gitlab/backups` rather than copied.
- AMP backups are exported but never rotated. Set retention in AMP.
- `update` runs `apt-get update` and `apt-get upgrade`, then pulls and restarts each
  running compose project under `/opt/docker`. Stopped projects stay stopped. It
  keeps existing config files, never removes packages, and never reboots: check the
  log for a warning that a reboot is required. It exits non-zero if anything failed.

Do not run the tests as root on a production host.
