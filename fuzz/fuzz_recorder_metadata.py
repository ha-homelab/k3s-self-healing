"""Run the recorder metadata oracle under Atheris on Linux."""

import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with atheris.instrument_imports():
    from fuzz.oracles import recorder_metadata


if __name__ == "__main__":
    atheris.Setup(sys.argv, atheris.instrument_func(recorder_metadata))
    atheris.Fuzz()
