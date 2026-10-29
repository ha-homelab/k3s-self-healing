"""Validate a disposable target image with LAN access blocked by NetworkPolicy."""
from pathlib import Path
import json
import os
import stat
import shutil
import time
import subprocess
import sys


ROOT_EXCLUDED = {"backups", "recorder_backup", "tmp_backups", ".git", "__pycache__", "media", "tts"}
SCHEMA_ONLY_RUNTIME_DIRECTORIES = {
    ".storage/home_floorplan_generated_assets",
    ".storage/home_floorplan_studio_assets",
    ".storage/home_floorplan/studio",
    ".home-studio-backups",
}
SCHEMA_ONLY_ROOT_DIRECTORY_PREFIXES = (
    ".home-studio-install-",
    ".asterisk-telephony-backup-",
    ".epg-channel-backup-",
    ".epg-media-backup-",
    ".ha-stale-retirement-",
    ".recorder-quality-backup-",
)
PROTECTED_PLACEHOLDER_ROOTS = ("/config", "/app", "/work", "/ha-source", "/usr", "/etc", "/proc", "/sys", "/dev", "/bin", "/sbin", "/lib", "/lib64", "/root", "/tmp", "/var", "/run", "/opt")
CHECK_CONFIG_BOOTSTRAP = (
    "import logging,runpy,sys; "
    "logging.basicConfig(stream=sys.stderr,level=logging.INFO); "
    "sys.argv=sys.argv[1:]; "
    "runpy.run_module('homeassistant',run_name='__main__')"
)


def schema_only_runtime_directory(relative, mode):
    """Omit generated visualization/rollback directories only for check_config.

    A full Home Studio startup needs its runtime assets. Do not reuse this policy
    for startup canaries or backups. Store files, component code, and aliases
    still take the ordinary copy and confinement paths even at matching names.
    """
    relative = Path(relative)
    return stat.S_ISDIR(mode) and (
        relative.as_posix() in SCHEMA_ONLY_RUNTIME_DIRECTORIES
        or (len(relative.parts) == 1 and relative.name.startswith(SCHEMA_ONLY_ROOT_DIRECTORY_PREFIXES))
    )


def validate_placeholder_directories(directories, live_mounts):
    """Allow explicit empty paths only within a real, non-system HA mount."""
    if not isinstance(directories, list) or not isinstance(live_mounts, list):
        raise RuntimeError("Preflight placeholder settings must be lists")

    def safe_path(value):
        if not isinstance(value, str) or "\0" in value:
            return None
        path = Path(value)
        if not path.is_absolute() or str(path) != value or ".." in path.parts:
            return None
        if any(path.is_relative_to(root) or Path(root).is_relative_to(path)
               for root in PROTECTED_PLACEHOLDER_ROOTS):
            return None
        return path

    mounts = [path for value in live_mounts if (path := safe_path(value))]
    result = []
    for value in directories:
        directory = safe_path(value)
        matching = [mount for mount in mounts if directory is not None and directory.is_relative_to(mount)]
        if not matching:
            raise RuntimeError("Preflight placeholder is outside an allowed live external mount")
        mount = max(matching, key=lambda path: len(path.parts))
        item = {"path": str(directory), "mount": str(mount)}
        if item not in result:
            result.append(item)
    return result


def prepare_placeholder_directories(specifications, container_root=Path("/")):
    """Create empty stand-ins, never mount or overwrite the live shared data."""
    if not isinstance(specifications, list) or any(not isinstance(item, dict) or set(item) != {"path", "mount"} for item in specifications):
        raise RuntimeError("Invalid preflight placeholder specification")
    checked = validate_placeholder_directories([item["path"] for item in specifications], [item["mount"] for item in specifications])
    roots = {item["mount"] for item in checked}
    for mount in roots:
        path = container_root / mount.lstrip("/")
        for part in (path, *path.parents):
            if part == container_root.parent:
                break
            if part.is_symlink():
                raise RuntimeError("Preflight placeholder would follow a symlink")
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise RuntimeError("Preflight placeholder would overwrite existing image data")
    for item in checked:
        (container_root / item["path"].lstrip("/")).mkdir(mode=0o700, parents=True, exist_ok=True)
    return len(checked)


def copy_reserve_bytes(total):
    """Match native_backup.runner.reserve_bytes without importing its worker."""
    mib = 1024**2
    threshold = (total + 20 * mib - 1) // (20 * mib) * mib
    return max(3 * 1024**3, threshold + 1024**3)


def wait_for_copy_capacity(path, payload_bytes, wait_seconds=300):
    """Wait only for capacity; never delete data or reduce the host reserve."""
    deadline = time.monotonic() + wait_seconds
    while True:
        disk = shutil.disk_usage(path)
        required = copy_reserve_bytes(disk.total) + payload_bytes + 64 * 1024**2
        if disk.free >= required:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Insufficient free space for preflight copy and host reserve: required {required} bytes, available {disk.free} bytes")
        print(json.dumps({"preflight_stage": "waiting_for_copy_capacity",
                          "required_bytes": required, "free_bytes": disk.free}), flush=True)
        time.sleep(min(5, remaining))


def copy_configuration(source, target, limit=3 * 1024**3):
    """Copy regular files, then recreate only aliases confined to the copy.

    Source directories/files are opened relative to held directory descriptors
    with O_NOFOLLOW. Symlinks are collected without following them, resolved in
    their logical /config namespace, and created only after copying all files.
    Neither /config/config nor historical staging aliases can recurse or escape.
    """
    source, target = Path(source).absolute(), Path(target).absolute()
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(source.resolve()):
        raise RuntimeError("Preflight copy requires a new destination outside the source")
    total, files = 0, 0
    allocation_unit = max(4096, os.statvfs(target.parent).f_frsize)
    allocated = allocation_unit  # The new configuration root directory.
    links = {}

    def identity(item):
        return (item.st_dev, item.st_ino, item.st_mode, item.st_size, item.st_mtime_ns, item.st_ctime_ns)

    def visit(directory, relative, copying):
        nonlocal total, files, allocated
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if not relative.parts and (name in ROOT_EXCLUDED or name.startswith(("home-assistant.log", "home-assistant_v2.db"))):
                continue
            child = relative / name
            destination = target / child
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if schema_only_runtime_directory(child, before.st_mode):
                continue
            if stat.S_ISLNK(before.st_mode):
                if not copying:
                    allocated += allocation_unit
                links[child.as_posix()] = os.readlink(name, dir_fd=directory)
                if identity(before) != identity(os.stat(name, dir_fd=directory, follow_symlinks=False)):
                    raise RuntimeError("Configuration link changed during preflight copy")
            elif stat.S_ISDIR(before.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if identity(before) != identity(os.fstat(descriptor)):
                        raise RuntimeError("Configuration directory changed during preflight copy")
                    if copying:
                        wait_for_copy_capacity(target.parent, allocation_unit, wait_seconds=0)
                        destination.mkdir(mode=0o700)
                    else:
                        allocated += allocation_unit
                    visit(descriptor, child, copying)
                finally:
                    os.close(descriptor)
            elif stat.S_ISREG(before.st_mode):
                if total + before.st_size > limit:
                    raise RuntimeError("Configuration copy exceeds its validation byte budget")
                if not copying:
                    total += before.st_size
                    files += 1
                    allocated += allocation_unit + ((before.st_size + allocation_unit - 1) // allocation_unit) * allocation_unit
                    continue
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as original:
                    if identity(before) != identity(os.fstat(original.fileno())):
                        raise RuntimeError("Configuration file changed during preflight copy")
                    wait_for_copy_capacity(target.parent, allocation_unit, wait_seconds=0)
                    with destination.open("xb") as copied:
                        destination.chmod(0o700 if before.st_mode & stat.S_IXUSR else 0o600)
                        while block := original.read(1024**2):
                            total += len(block)
                            if total > limit:
                                raise RuntimeError("Configuration copy exceeds its validation byte budget")
                            wait_for_copy_capacity(target.parent, ((len(block) + allocation_unit - 1) // allocation_unit) * allocation_unit, wait_seconds=0)
                            copied.write(block)
                    if identity(before) != identity(os.fstat(original.fileno())):
                        raise RuntimeError("Configuration file changed during preflight copy")
                files += 1
            else:
                raise RuntimeError("Configuration contains a special file; preflight copy refused")

    root = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Both passes use the same fd-based traversal and exact selection policy.
        # Admission happens before the destination or any payload file is created.
        visit(root, Path(), copying=False)
        wait_for_copy_capacity(target.parent, allocated)
        total, files, links = 0, 0, {}
        target.mkdir(mode=0o700)
        visit(root, Path(), copying=True)
    finally:
        os.close(root)

    def link_parts(raw):
        if not raw.startswith("/"):
            return False, raw.split("/")
        for prefix in ("/config", str(source)):
            if raw == prefix:
                return True, []
            if raw.startswith(prefix + "/"):
                return True, raw[len(prefix) + 1:].split("/")
        raise RuntimeError("Configuration contains an external symlink; preflight copy refused")

    def resolve_link(name):
        # Resolve components before '..', like filesystem traversal. A lexical
        # normpath before expanding a link could conceal an escape through it.
        remaining = name.split("/")
        resolved, hops = [], 0
        while remaining:
            part = remaining.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise RuntimeError("Configuration symlink escapes its root")
                resolved.pop()
                continue
            resolved.append(part)
            key = "/".join(resolved)
            if key in links:
                hops += 1
                if hops > 40:
                    raise RuntimeError("Configuration symlink chain is cyclic or too long")
                resolved.pop()
                absolute, parts = link_parts(links[key])
                if absolute:
                    resolved = []
                remaining = parts + remaining
        return target.joinpath(*resolved)

    resolved_links = {name: resolve_link(name) for name in links}
    for name, destination in resolved_links.items():
        alias = target / name
        wait_for_copy_capacity(target.parent, allocation_unit, wait_seconds=0)
        alias.symlink_to(os.path.relpath(destination, alias.parent))
    return {"configuration_bytes": total, "files": files, "symlinks": len(links)}


def expose_disposable_config(target, alias=Path("/config")):
    """Keep absolute /config paths confined to the disposable configuration."""
    target, alias = Path(target), Path(alias)
    if not target.is_dir() or target.is_symlink() or alias.is_mount() or alias.is_symlink():
        raise RuntimeError("Cannot expose the disposable configuration at /config")
    if alias.exists():
        if not alias.is_dir() or any(alias.iterdir()):
            raise RuntimeError("Image /config directory is not empty; refusing to replace it")
        # The official image sets WORKDIR=/config. Removing the current working
        # directory would leave child processes unable to call os.getcwd().
        if Path.cwd().resolve() == alias.resolve():
            os.chdir(target)
        alias.rmdir()
    alias.symlink_to(target, target_is_directory=True)


def validate_config_report(report, returncode=0):
    """HA reports some validation failures as warnings; block those explicitly."""
    if not isinstance(report, dict) or not isinstance(report.get("warnings"), dict):
        raise RuntimeError("Home Assistant did not return its structured configuration report")
    if returncode or report.get("total_errors") != 0 or report.get("errors") != {}:
        raise RuntimeError("Home Assistant configuration validation failed")
    for messages in report["warnings"].values():
        if not isinstance(messages, list):
            raise RuntimeError("Home Assistant configuration warning format changed")
        for message in messages:
            if isinstance(message, str) and message.startswith("Setup of package '") and " failed:" in message:
                raise RuntimeError("Home Assistant package merge validation failed")
            if isinstance(message, str) and message.startswith(("Integration error:", "Component error:", "Platform error '", "Invalid config for '")):
                raise RuntimeError("Home Assistant integration configuration validation failed")
    warnings = report.get("total_warnings")
    if not isinstance(warnings, int) or isinstance(warnings, bool) or warnings < 0:
        raise RuntimeError("Home Assistant configuration warning count is invalid")
    return warnings


def main():
    os.umask(0o077)
    subprocess.run([sys.executable, "/app/backup_contract.py"], check=True, timeout=60)
    source, target = Path("/ha-source"), Path("/work/config")
    copied = copy_configuration(source, target)
    os.chdir(target)
    expose_disposable_config(target)
    placeholders = prepare_placeholder_directories(json.loads(os.environ.get("HA_PREFLIGHT_PLACEHOLDERS", "[]")))
    print(json.dumps({"preflight_stage": "copied", **copied}), flush=True)
    # Apply exactly the mounted live compatibility hooks, against this disposable image.
    for hook in sorted(Path("/etc/cont-init.d").glob("*.py")):
        if hook.name.startswith(("50-", "51-", "52-")):
            subprocess.run([sys.executable, str(hook)], check=True, timeout=60)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    # Reports can contain resolved configuration values. Keep them in private
    # disposable files; only counts and error categories go into the Job log.
    report_path, errors_path = Path("/work/check-config.json"), Path("/work/check-config.stderr")
    with report_path.open("w") as output, errors_path.open("w") as errors:
        checked = subprocess.run([sys.executable, "-c", CHECK_CONFIG_BOOTSTRAP, "homeassistant", "--script", "check_config", "--config", str(target), "--json"],
                                 stdout=output, stderr=errors, check=False, timeout=1200, env=env)
    try:
        report = json.loads(report_path.read_text())
    except (ValueError, OSError) as error:
        print(json.dumps({"preflight_stage": "invalid_report", "checker_returncode": checked.returncode,
                          "report_bytes": report_path.stat().st_size, "stderr_bytes": errors_path.stat().st_size}), flush=True)
        raise RuntimeError("Home Assistant configuration report is unavailable; inspect the private preflight workspace") from error
    warnings = validate_config_report(report, checked.returncode)
    print(json.dumps({"preflight": "passed", "configuration_warnings": warnings, "empty_placeholder_directories": placeholders, **copied}), flush=True)


if __name__ == "__main__":
    main()
