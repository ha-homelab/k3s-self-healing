"""Verify the public archive against its inventory and exercise isolated sources."""

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def verify(archive, root):
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    files = sorted(json.loads((root / "publication.json").read_text())["files"])
    with tempfile.TemporaryDirectory(prefix="public-k3s-source-") as temporary:
        destination = Path(temporary) / "k3s-self-healing"
        with tarfile.open(archive) as package:
            members = package.getmembers()
            if [member.name for member in members] != [
                "k3s-self-healing/" + name for name in files
            ]:
                raise ValueError(
                    "Archive does not match the approved publication inventory"
                )
            for member, name in zip(members, files):
                content = (root / name).read_bytes()
                if not member.isfile() or member.size != len(content):
                    raise ValueError("Invalid archive file type or size")
                stream = package.extractfile(member)
                if stream is None or stream.read(len(content) + 1) != content:
                    raise ValueError("Archive bytes differ from the validated source")
                path = destination / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        commands = [
            ["tools/privacy_scan.py"],
            ["tools/render.py", "--help"],
            ["native_backup/restore.py", "--help"],
            ["-m", "unittest", "discover", "-s", "tests", "-p", "test_native*.py"],
            ["-m", "unittest", "discover", "-s", "tests", "-p", "test_fuzz_oracles.py"],
        ]
        for arguments in commands:
            subprocess.run([sys.executable, *arguments], cwd=destination, check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("Archive changed during verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    verify(args.archive, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
