#!/usr/local/bin/python3
"""Wait for the managed go2rtc APIs before HA caches stream capabilities."""

import ast
import os
from pathlib import Path
import stat
import sys
import tempfile

TARGET = Path("/usr/src/homeassistant/homeassistant/components/go2rtc/server.py")
IMPORT = "from go2rtc_client import Go2RtcRestClient\n"
PATCHED_IMPORT = IMPORT + "from go2rtc_client.exceptions import Go2RtcClientError\n"
BEFORE = '''        # Check the server version
        client = Go2RtcRestClient(self._session, HA_MANAGED_URL)
        await client.validate_server_version()'''
AFTER = BEFORE + '''

        # The API listener starts before go2rtc registers all source modules.
        # Do not let HA cache an incomplete list of supported stream schemes.
        try:
            async with asyncio.timeout(_SETUP_TIMEOUT):
                while True:
                    try:
                        schemes = await client.schemes.list()
                        if {"exec", "ffmpeg", "http", "onvif", "rtsp", "webrtc"} <= schemes:
                            await client.streams.list()
                            await client.preload.list()
                            break
                    except Go2RtcClientError as err:
                        if getattr(err.__cause__, "status", None) not in (404, 503):
                            raise
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception as err:
            await self._stop()
            raise Go2RTCServerStartError("go2rtc stream APIs did not become ready") from err'''


def patched_source(source):
    """Patch only the known managed-server startup, leaving other versions intact."""
    tree = ast.parse(source)
    methods = [node for cls in tree.body
               if isinstance(cls, ast.ClassDef) and cls.name == "Server"
               for node in cls.body
               if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start"]
    if len(methods) != 1:
        return source, "unsupported"
    method = methods[0]
    lines = source.splitlines(keepends=True)
    current = "".join(lines[method.lineno - 1:method.end_lineno]).rstrip("\r\n")
    if current.endswith(AFTER) and PATCHED_IMPORT in source:
        return source, "already-patched"
    if (not current.endswith(BEFORE) or current.count(BEFORE) != 1
            or source.count(IMPORT) != 1 or PATCHED_IMPORT in source):
        return source, "unsupported"
    # Require the known readiness signal, timeout and module configuration too.
    required = ('_SUCCESSFUL_BOOT_MESSAGE = "INF [api] listen addr="',
                "await self._startup_complete.wait()", '    "ffmpeg",',
                '    "onvif",', '    "webrtc",')
    if not all(item in source for item in required):
        return source, "unsupported"
    result = ("".join(lines[:method.lineno - 1]) + current.replace(BEFORE, AFTER)
              + "\n" + "".join(lines[method.end_lineno:]))
    result = result.replace(IMPORT, PATCHED_IMPORT, 1)
    ast.parse(result)
    return result, "patched"


def patch_file(path):
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
    status = patch_file(target) if target.exists() else "unsupported"
    print(f"go2rtc-startup-guard: {status}", flush=True)
    if status == "unsupported":
        print("WARNING: upstream go2rtc startup changed; review this guard. Source left intact.",
              file=sys.stderr, flush=True)
