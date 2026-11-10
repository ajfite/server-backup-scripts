import errno
import glob
import os
import shutil
import stat
import time
from typing import Collection, Optional

from loguru import logger

TEMP_SUFFIX = ".tmp"
PRIVATE_DIR_MODE = 0o700
EXPORT_DIR_MODE = 0o750
EXPORT_FILE_MODE = 0o640

DEFAULT_BACKUP_EXTENSIONS = (
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.zst",
    ".tar.zst.age",
    ".sql",
    ".sql.gz",
    ".sql.bz2",
    ".sql.zst",
    ".sql.zst.age",
    ".sqlite3",
    ".sqlite3.bz2",
    ".sqlite3.zst",
    ".zip",
)

_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _newest_first(paths: list[str]) -> list[str]:
    """Keep regular files only, sorted newest first, tolerating vanished entries."""
    found = []
    for path in paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            found.append((st.st_mtime, path))

    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def get_most_recent_files(
    path: str, count: Optional[int] = None, glob_str: str = "*"
) -> list[str]:
    """
    Get the most recently modified files from a directory.

    Args:
        path: Directory to search in.
        count: Number of files to return, or None for all of them.
        glob_str: the glob to search for in the directory (if any)

    Returns:
        A list of paths to the most recent files.
    """
    return _newest_first(glob.glob(os.path.join(path, glob_str)))[:count]


def _is_backup_name(name: str, extensions: tuple[str, ...]) -> bool:
    return not name.startswith(".") and name.endswith(extensions)


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
    return _newest_first(
        [
            os.path.join(path, entry)
            for entry in os.listdir(path)
            if _is_backup_name(entry, exts)
        ]
    )


def rotate_directory_backups(
    path: str, keep_count: int, extensions: Optional[tuple[str, ...]] = None
) -> list[str]:
    """
    Rotate backups in a directory, keeping the newest `keep_count` files.

    Args:
        path: Directory containing backups to rotate.
        keep_count: Number of newest backups to retain, at least 1.
        extensions: Optional tuple of file extensions to consider backups.

    Returns:
        List of paths that were removed.
    """
    if keep_count < 1:
        logger.warning(f"keep_count must be >= 1, got {keep_count}")
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


def make_private_dir(path: str) -> None:
    """Create `path` if needed and make it accessible to its owner only."""
    os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)
    os.chmod(path, PRIVATE_DIR_MODE)


def make_private_file(path: str) -> None:
    """Make a file in a trusted directory owned by and private to this process."""
    os.chown(path, os.geteuid(), os.getegid(), follow_symlinks=False)
    os.chmod(path, 0o600)


def remove_stale_temp_files(path: str) -> None:
    """Remove partial archives left behind by a run that was killed."""
    for stale in glob.glob(os.path.join(path, f"*{TEMP_SUFFIX}")):
        try:
            os.remove(stale)
            logger.warning(f"Removed partial archive from an earlier run: {stale}")
        except OSError as e:
            logger.warning(f"Could not remove partial archive {stale}: {e}")


def move_file_atomic(src: str, dest: str) -> None:
    """Move a file so that `dest` only ever appears complete, even across devices."""
    try:
        os.rename(src, dest)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise

    tmp = f"{dest}{TEMP_SUFFIX}"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.remove(src)


def open_dir_nofollow(name: str, dir_fd: Optional[int] = None) -> int:
    """Open a directory, refusing to follow a symlink in the final component."""
    return os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)


def _set_owner_and_mode(
    fd: int, mode: int, uid: Optional[int], gid: Optional[int]
) -> None:
    if uid is not None or gid is not None:
        os.fchown(fd, -1 if uid is None else uid, -1 if gid is None else gid)
    os.fchmod(fd, mode)


def open_export_dir(
    name: str,
    parent_fd: Optional[int] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> int:
    """
    Create and open an export directory without following symlinks, then apply
    its owner and mode through the descriptor so the path cannot be swapped.
    """
    try:
        os.mkdir(name, EXPORT_DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass

    fd = open_dir_nofollow(name, dir_fd=parent_fd)
    try:
        _set_owner_and_mode(fd, EXPORT_DIR_MODE, uid, gid)
    except OSError:
        os.close(fd)
        raise
    return fd


def _link_into(name: str, src_fd: int, dst_fd: int, src_ino: int) -> None:
    """Hard-link `name` between directories, replacing any different destination."""
    try:
        if os.lstat(name, dir_fd=dst_fd).st_ino == src_ino:
            return
        os.unlink(name, dir_fd=dst_fd)
    except FileNotFoundError:
        pass

    os.link(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)

    # The source may be in a directory another user controls, so confirm the
    # link is the inode that was just checked.
    if os.lstat(name, dir_fd=dst_fd).st_ino != src_ino:
        os.unlink(name, dir_fd=dst_fd)
        raise OSError(f"{name} changed while it was being exported")


def _export_file(
    name: str,
    src_fd: int,
    dst_fd: int,
    owner_uid: Optional[int],
    gid: Optional[int],
    allowed_uids: Optional[Collection[int]],
    min_age_seconds: int,
) -> bool:
    """
    Export one file. Returns False on failure. Skipped files are not failures.
    """
    try:
        fd = os.open(name, _FILE_OPEN_FLAGS, dir_fd=src_fd)
    except FileNotFoundError:
        return True
    except OSError as e:
        # Includes a symlink, which has no business being in a backup directory.
        logger.error(f"Refusing to export {name}: {e}")
        return False

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning(f"Not exporting {name}: not a regular file")
            return True
        if allowed_uids is not None and st.st_uid not in allowed_uids:
            logger.warning(f"Not exporting {name}: unexpected owner {st.st_uid}")
            return True
        if time.time() - st.st_mtime < min_age_seconds:
            logger.info(f"Not exporting {name} yet: may still be being written")
            return True

        _set_owner_and_mode(fd, EXPORT_FILE_MODE, owner_uid, gid)
        _link_into(name, src_fd, dst_fd, st.st_ino)
        return True
    except OSError as e:
        logger.error(f"Failed to export {name}: {e}")
        return False
    finally:
        os.close(fd)


def export_directory(
    src_fd: int,
    dst_fd: int,
    owner_uid: Optional[int] = None,
    gid: Optional[int] = None,
    allowed_uids: Optional[Collection[int]] = None,
    min_age_seconds: int = 0,
) -> int:
    """
    Mirror backup files from one open directory to another using hard links.

    Every operation goes through directory descriptors and never follows a
    symlink, so this is safe where another user controls the source. Files are
    exported as `owner_uid`:`gid` with mode 0640. A hard link shares its inode, so
    this also applies to the source file.

    Nothing is pruned when the source has no backups, so an unmounted or wiped
    source does not also empty the export.

    Args:
        src_fd: Open source directory.
        dst_fd: Open destination directory.
        owner_uid: Owner to apply, or None to leave unchanged.
        gid: Group to apply, or None to leave unchanged.
        allowed_uids: If set, only export files owned by one of these users.
        min_age_seconds: Skip files modified more recently than this.

    Returns:
        Number of entries that could not be exported or pruned.
    """
    failures = 0
    active = set()
    for name in os.listdir(src_fd):
        if not _is_backup_name(name, DEFAULT_BACKUP_EXTENSIONS):
            continue
        active.add(name)
        if not _export_file(
            name, src_fd, dst_fd, owner_uid, gid, allowed_uids, min_age_seconds
        ):
            failures += 1

    if not active:
        logger.warning("Source has no backups, leaving the export as it is")
        return failures

    for name in os.listdir(dst_fd):
        if name in active or not _is_backup_name(name, DEFAULT_BACKUP_EXTENSIONS):
            continue
        try:
            os.unlink(name, dir_fd=dst_fd)
            logger.info(f"Pruned rotated backup from export: {name}")
        except OSError as e:
            logger.error(f"Could not prune {name}: {e}")
            failures += 1

    return failures
