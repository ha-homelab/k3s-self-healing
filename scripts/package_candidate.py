"""Run the existing public-source packager with a verified frozen candidate plan."""

import argparse
import sys
from pathlib import Path

from release_version_adapter import checked_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from package_release import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("channel", choices=("beta", "rc", "nightly"))
    args = parser.parse_args()
    version = checked_version(ROOT, args.base, args.channel)
    output = ROOT / "release-dist"
    output.mkdir(exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Release output must be empty")
    build(ROOT, output / f"k3s-self-healing-v{version}.tar.gz", args.base, args.channel)


if __name__ == "__main__":
    main()
