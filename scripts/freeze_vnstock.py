#!/usr/bin/env python3
# Copyright (c) 2026 bachbnt. All rights reserved.
"""Save the vnstock 4.x copy installed in this environment so it can be reused.

vnstock 4.x is no longer published on PyPI or the vendor's package index, so a fresh environment (a new
machine, CI, a Docker build) cannot install it. This packs exactly the files of the installed `vnstock` and
`vnai` distributions into docker/vendor/, where the Dockerfile picks them up.

Run it with the project's virtualenv, which still has vnstock:

    .venv/bin/python scripts/freeze_vnstock.py

The archive contains third-party code. It stays on your machine (docker/vendor/ is git-ignored): do not
commit it and do not push a Docker image that contains it to a public registry.
"""

import sys
import tarfile
from importlib import metadata
from pathlib import Path

DISTRIBUTIONS = ('vnstock', 'vnai')
OUTPUT = Path(__file__).resolve().parent.parent / 'docker' / 'vendor' / 'vnstock-site-packages.tar.gz'


def collect(name: str) -> list[tuple[Path, str]]:
    """(absolute path, path inside site-packages) for every real file of a distribution."""
    dist = metadata.distribution(name)
    files = []
    for entry in dist.files or []:
        parts = entry.parts
        if '__pycache__' in parts or entry.suffix == '.pyc' or parts[0] == '..':
            continue  # skip bytecode and console scripts installed outside site-packages
        path = Path(dist.locate_file(entry))
        if path.is_file():
            files.append((path, str(entry)))
    return files


def main() -> int:
    try:
        versions = {name: metadata.version(name) for name in DISTRIBUTIONS}
    except metadata.PackageNotFoundError as e:
        print(f"error: {e.args[0]} is not installed in this Python ({sys.executable}).", file=sys.stderr)
        print("Run this with the virtualenv that still has vnstock: .venv/bin/python scripts/freeze_vnstock.py",
              file=sys.stderr)
        return 1
    if not versions['vnstock'].startswith('4.'):
        print(f"error: expected vnstock 4.x, found {versions['vnstock']}", file=sys.stderr)
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(OUTPUT, 'w:gz') as archive:
        for name in DISTRIBUTIONS:
            for path, arcname in collect(name):
                archive.add(path, arcname=arcname, recursive=False)
                count += 1
    size = OUTPUT.stat().st_size / 1024
    print(f"Saved {count} files ({size:.0f} KiB): " + ', '.join(f'{n} {v}' for n, v in versions.items()))
    print(f"  -> {OUTPUT}")
    print("Keep it local: it is git-ignored and must not be redistributed.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
