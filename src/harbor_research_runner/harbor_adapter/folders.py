from pathlib import Path


class HarborFolderError(ValueError):
    """A Harbor job folder cannot be read consistently."""


def trial_folders(job_dir: Path) -> list[Path]:
    """Return trial directories with a config file, ordered by folder name."""
    try:
        folders = [
            child
            for child in job_dir.iterdir()
            if child.is_dir() and (child / "config.json").is_file()
        ]
    except OSError:
        return []
    return sorted(folders, key=lambda path: path.name)
