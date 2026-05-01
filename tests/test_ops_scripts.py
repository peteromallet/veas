from pathlib import Path

from scripts.backup_dump import sha256_file


def test_sha256_file_matches_known_content(tmp_path: Path) -> None:
    path = tmp_path / "dump"
    path.write_bytes(b"veas backup")

    assert sha256_file(path) == "bdc1a887db21bbb0f413ef3d6ed2056e4689cb09cb652fb00cdee3b50a4dd25e"
