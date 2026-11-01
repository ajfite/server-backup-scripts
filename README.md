# Server Backup Scripts

A python script set for managing backups on my server.

## Deploying

Install or upgrade the `backup` command as root, install the units, and enable
only the timer:

```sh
sudo pipx install --force git+https://git.nclf.net/ajfite/server-backup-scripts.git
sudo cp systemd/server-backup.service systemd/server-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-backup.timer
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

Do not run the tests as root on a production host.
