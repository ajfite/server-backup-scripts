import glob
import os


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
