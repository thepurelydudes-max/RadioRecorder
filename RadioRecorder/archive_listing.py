"""On-demand archive display data, safe to collect outside the Tk thread."""
from __future__ import annotations

import os
from pathlib import Path


def read_archive_rows(folder: str | Path) -> list[str]:
    """Return the same reverse-name MP3 rows shown by the archive window.

    DirEntry reuses the directory scan's metadata on Windows. No archive
    paths or metadata are kept after this on-demand snapshot is built.
    """
    try:
        with os.scandir(folder) as scan:
            entries = [entry for entry in scan
                       if os.path.normcase(entry.name).endswith('.mp3')]
    except OSError:
        return []

    entries.sort(key=lambda entry: os.path.normcase(entry.name), reverse=True)
    rows = []
    for entry in entries:
        try:
            size = entry.stat().st_size / 1024 / 1024
        except OSError:
            size = 0
        cue = Path(entry.path).with_suffix('.cue').exists()
        rows.append(f'{entry.name:<45} {size:>7.1f} МБ' + ('  [CUE]' if cue else ''))
    return rows
