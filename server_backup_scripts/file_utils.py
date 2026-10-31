import glob
import os
from typing import Optional

from loguru import logger

DEFAULT_BACKUP_EXTENSIONS = (
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.zst",
    ".sql",
    ".sql.gz",
    ".sql.bz2",
    ".sql.zst",
    ".sqlite3",
    ".sqlite3.zst",
    ".zip",
)


def get_most_recent_files(path: str, count: int, glob_str: str = "*") -> list[str]:
    """
    Get the most recently modified files from a directory.

    Args:
        path: Directory to search in.
        count: Number of files to return.
        glob_str: the glob to search for in the directory (if any)

    Returns:
        A list of paths to the most recent files.
    """
    search_path = os.path.join(path, glob_str)
    all_entries = glob.glob(search_path)

    files = [f for f in all_entries if os.path.isfile(f)]
    files.sort(key=os.path.getmtime, reverse=True)

    return files[:count]


def get_backup_files(
    path: str, extensions: Optional[tuple[str, ...]] = None
) -> list[str]:
    """
    Get all backup archive files from a directory, sorted newest first.

    Args:
        path: Directory to search in.
        extensions: Tuple of allowed file extensions. If None, uses
            DEFAULT_BACKUP_EXTENSIONS.

    Returns:
        List of file paths sorted by modification time descending.
    """
    if not os.path.isdir(path):
        return []

    exts = extensions if extensions is not None else DEFAULT_BACKUP_EXTENSIONS
    files = []
    for entry in os.listdir(path):
        if entry.startswith(".") or entry.endswith(".log"):
            continue
        full_path = os.path.join(path, entry)
        if os.path.isfile(full_path):
            if any(entry.endswith(ext) for ext in exts):
                files.append(full_path)

    files.sort(key=os.path.getmtime, reverse=True)
    return files


def rotate_directory_backups(
    path: str, keep_count: int, extensions: Optional[tuple[str, ...]] = None
) -> list[str]:
    """
    Rotate backups in a directory, keeping the newest `keep_count` files.

    Args:
        path: Directory containing backups to rotate.
        keep_count: Number of newest backups to retain.
        extensions: Optional tuple of file extensions to consider backups.

    Returns:
        List of paths that were removed.
    """
    if keep_count < 0:
        logger.warning(f"keep_count must be >= 0, got {keep_count}")
        return []

    backups = get_backup_files(path, extensions=extensions)
    if len(backups) <= keep_count:
        logger.info(
            f"No rotation needed for {path}: {len(backups)} backups <= {keep_count}"
        )
        return []

    to_remove = backups[keep_count:]
    removed = []
    for file_path in to_remove:
        try:
            os.remove(file_path)
            removed.append(file_path)
            logger.info(f"Rotated out old backup: {file_path}")
        except OSError as e:
            logger.error(f"Failed to remove {file_path}: {e}")

    return removed


def link_file_space_efficient(src: str, dst: str) -> bool:
    """
    Link a file to dst using a hard link without wasting disk space.
    Falls back to symlink if cross-device link fails.

    Args:
        src: Source file path.
        dst: Destination link path.

    Returns:
        True if linking succeeded, False otherwise.
    """
    if not os.path.exists(src):
        logger.error(f"Cannot link non-existent source: {src}")
        return False

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if os.path.exists(dst) or os.path.islink(dst):
        try:
            if os.path.samefile(src, dst):
                return True
        except OSError:
            pass
        try:
            os.remove(dst)
        except OSError as e:
            logger.error(f"Failed to remove existing destination {dst}: {e}")
            return False

    try:
        os.link(src, dst)
        logger.debug(f"Hard-linked {src} -> {dst}")
        return True
    except OSError as e:
        logger.warning(f"Hard link failed ({e}), falling back to symlink for {dst}")
        try:
            os.symlink(src, dst)
            return True
        except OSError as sym_err:
            logger.error(f"Symlink failed ({sym_err}) for {dst}")
            return False


def set_permissions_and_owner(
    path: str, mode: int = 0o644, user: Optional[str] = None
) -> None:
    """
    Set file/dir permissions and optionally change owner if running with privileges.
    """
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.debug(f"Could not chmod {path} to {oct(mode)}: {e}")

    if user and os.geteuid() == 0:
        import pwd

        try:
            pw = pwd.getpwnam(user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except KeyError:
            logger.warning(f"User {user} does not exist on this system")
        except OSError as e:
            logger.warning(f"Could not chown {path} to {user}: {e}")
