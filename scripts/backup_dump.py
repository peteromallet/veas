"""Create a Postgres dump and adjacent SHA-256 checksum."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="backups")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dump_path = out_dir / f"veas-{stamp}.dump"
    subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(dump_path), database_url],
        check=True,
    )
    checksum = sha256_file(dump_path)
    checksum_path = dump_path.with_suffix(dump_path.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {dump_path.name}\n")
    print(dump_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
