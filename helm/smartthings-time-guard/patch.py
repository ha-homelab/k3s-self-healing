#!/usr/local/bin/python3
"""Guard the known SmartThings DND getter without replacing the integration."""

import ast
import os
from pathlib import Path
import stat
import sys
import tempfile

TARGET = Path("/usr/src/homeassistant/homeassistant/components/smartthings/time.py")
BEFORE = '''    @property
    @override
    def native_value(self) -> time:
        """Return the time value."""
        state = self.get_attribute_value(
            Capability.CUSTOM_DO_NOT_DISTURB_MODE, self.entity_description.attribute
        )
        return time(int(state[:2]), int(state[3:5]))'''
AFTER = BEFORE.replace("-> time:", "-> time | None:").replace(
    "        return time(",
    "        if state is None:\n            return None\n        return time(",
)


def patched_source(source):
    """Only modify the verified getter; leave newer/different implementations alone."""
    tree = ast.parse(source)
    candidates = [
        node for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "SmartThingsDnDTime"
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "native_value"
    ]
    if len(candidates) != 1:
        return source, "unsupported"
    getter = candidates[0]
    start = min([getter.lineno] + [d.lineno for d in getter.decorator_list]) - 1
    lines = source.splitlines(keepends=True)
    current = "".join(lines[start:getter.end_lineno]).rstrip("\r\n")
    if current == AFTER:
        return source, "already-patched"
    if current != BEFORE:
        return source, "unsupported"
    result = "".join(lines[:start]) + AFTER + "\n" + "".join(lines[getter.end_lineno:])
    ast.parse(result)
    return result, "patched"


def patch_file(path):
    """Replace atomically only after parsing; preserve permissions."""
    source = path.read_text()
    result, status = patched_source(source)
    if status == "patched":
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(result)
        try:
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return status


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    status = patch_file(target)
    print(f"smartthings-time-guard: {status}", flush=True)
    if status == "unsupported":
        print("WARNING: upstream getter changed; review this hotfix. Source was left intact.",
              file=sys.stderr, flush=True)
