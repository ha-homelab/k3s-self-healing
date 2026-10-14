#!/usr/bin/env python3
# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Public, release-candidate-only fuzzing policy and bounded runner.

The manifest is reviewed source, not an untrusted command interface. Commands
are argument arrays, never shell fragments. Campaigns run without networking;
only dependency installation/build steps may use the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
# Reviewed build and fuzz commands use argv arrays, never shell interpolation.
import subprocess  # nosec B404
import time

MANIFEST = ".github/fuzz.json"
JOB = "fuzz / RC fuzzing"
REPORT = "fuzz-report.json"
SHA = re.compile(r"[a-f0-9]{40}")


def configuration(policy: dict) -> dict | None:
    """Opt-in requires a public, versioned release and immutable toolkit code."""
    value = policy.get("fuzzing")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"toolkit_ref"}
        or not isinstance(value["toolkit_ref"], str)
        or not SHA.fullmatch(value["toolkit_ref"])
        or policy.get("visibility", "public") != "public"
        or policy.get("mode") != "release"
        or policy.get("ci_execution", "github") != "github"
        or not isinstance(policy.get("versioning"), dict)
    ):
        raise ValueError("Fuzzing requires public versioned hosted releases and a pinned toolkit_ref")
    return value


def relative(value: object) -> str:
    """Restrict manifest paths to literal paths within the source tree."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        raise ValueError("Fuzz paths must be literal relative paths")
    if value != "." and (value.startswith("/") or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("Fuzz path escapes the source tree")
    return value


def argv(value: object) -> list[str]:
    """Reject missing commands, control characters and unbounded argument lists."""
    if (
        not isinstance(value, list) or not 1 <= len(value) <= 64
        or any(not isinstance(v, str) or not v or len(v) > 2048 or any(ord(c) < 32 for c in v) for v in value)
    ):
        raise ValueError("Fuzz commands must be nonempty bounded argument arrays")
    return value


def validate_manifest_shape(data: object) -> None:
    if (
        not isinstance(data, dict)
        or set(data) - {"schema", "language", "setup", "targets", "go-version-file"}
        or type(data.get("schema")) is not int or data["schema"] != 1
        or data.get("language") not in {"go", "rust", "python", "javascript", "c"}
        or not isinstance(data.get("setup"), list) or len(data["setup"]) > 16
        or not isinstance(data.get("targets"), list) or not 1 <= len(data["targets"]) <= 12
    ):
        raise ValueError("Invalid fuzz manifest")


def validate_target(root: Path, target: object, names: set[str]) -> None:
    if (
        not isinstance(target, dict)
        or set(target) - {"name", "engine", "command", "seconds", "cwd", "corpus"}
        or not isinstance(target.get("name"), str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", target["name"])
        or target["name"] in names
        or target.get("engine") not in {"go", "libfuzzer", "atheris"}
        or type(target.get("seconds")) is not int or not 1 <= target["seconds"] <= 120
    ):
        raise ValueError("Invalid, duplicated or unbounded fuzz target")
    names.add(target["name"])
    argv(target.get("command"))
    confined(root, relative(target.get("cwd", ".")))
    if "corpus" in target:
        confined(root, relative(target["corpus"]))
    # The runner owns the time/resource limits and crash destination.
    reserved = ("-fuzztime", "-fuzzminimizetime", "-parallel", "-timeout", "-max_total_time", "-rss_limit_mb", "-artifact_prefix", "-runs", "-jobs", "-workers")
    if any(arg.split("=", 1)[0] in reserved for arg in target["command"]):
        raise ValueError("Fuzz target cannot override runner limits")


def manifest(root: Path) -> dict:
    raw = (root / MANIFEST).read_bytes()
    if len(raw) > 65536:
        raise ValueError("Fuzz manifest is too large")
    data = json.loads(raw)
    validate_manifest_shape(data)
    for command in data["setup"]:
        argv(command)
    if "go-version-file" in data:
        if data["language"] != "go":
            raise ValueError("go-version-file is only valid for Go")
        confined(root, relative(data["go-version-file"]))
    names = set()
    for target in data["targets"]:
        validate_target(root, target, names)
    if sum(t["seconds"] for t in data["targets"]) > 600:
        raise ValueError("Fuzz campaign budget exceeds ten minutes")
    return data


def confined(root: Path, value: str) -> Path:
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Fuzz path resolves outside source")
    return path


def event_contract(event: dict, event_name: str, ref: str, repository: str, sha: str) -> None:
    """Fail closed even if someone calls the reusable workflow directly."""
    repo = event.get("repository", {})
    if (
        event_name != "workflow_dispatch"
        or event.get("inputs", {}).get("channel") != "rc"
        or repo.get("private") is not False
        or repo.get("full_name") != repository
        or ref != "refs/heads/" + str(repo.get("default_branch"))
        or not SHA.fullmatch(sha)
    ):
        raise ValueError("Fuzz campaigns require a public default-branch RC dispatch")


def check_run_jobs(jobs: list[dict], channel: str, sha: str) -> None:
    matches = [j for j in jobs if j.get("name") == JOB]
    if channel == "rc":
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in {
            "head_sha": sha, "status": "completed", "conclusion": "success"
        }.items()):
            raise ValueError("RC requires one successful fuzz job at the exact source SHA")
    elif any(j.get("conclusion") != "skipped" for j in matches):
        raise ValueError("Fuzzing must not run outside RC")


def profile(policy: dict, channel: str) -> dict | None:
    config = configuration(policy)
    if config is None or channel != "rc":
        return None
    return {"schema": 1, "channel": "rc", "manifest": MANIFEST, **config}


def verify_report(directory: Path, root: Path, repository: str, sha: str, run: int, attempt: int) -> None:
    data = json.loads((directory / REPORT).read_bytes())
    config = manifest(root)
    expected = {
        "schema": 1, "repository": repository, "source_sha": sha,
        "run_id": run, "run_attempt": attempt, "channel": "rc", "private": False,
        "manifest_sha256": hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest(),
        "status": "passed", "network": "isolated",
    }
    if any(type(data.get(k)) is not type(v) or data.get(k) != v for k, v in expected.items()):
        raise ValueError("Fuzz report identity or outcome mismatch")
    targets = data.get("targets", [])
    if not isinstance(targets, list) or any(
        not isinstance(t, dict) or type(t.get("seconds")) is not int or type(t.get("exit_code")) is not int
        for t in targets
    ) or [(t.get("name"), t.get("seconds"), t.get("exit_code")) for t in targets] != [
        (t["name"], t["seconds"], 0) for t in config["targets"]
    ]:
        raise ValueError("Fuzz report target inventory mismatch")


def exercised(engine: str, log: str) -> bool:
    """A successful command that discovers no fuzz target must not qualify an RC."""
    pattern = r"fuzz:.*?execs:\s*([0-9]+)" if engine == "go" else r"Done ([0-9]+) runs in"
    return any(int(value) > 0 for value in re.findall(pattern, log))


def split_receipts(receipts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fuzz validation evidence is not a toolchain input of final package builds."""
    validation = [r for r in receipts if r["name"] == "release-inputs-fuzz.json"]
    if len(validation) != 1 or [a.get("name") for a in validation[0]["inputs"].get("artifacts", [])] != [REPORT]:
        raise ValueError("Fuzz validation receipt must cover exactly the RC report")
    return [r for r in receipts if r not in validation], validation


def execute(command: list[str], cwd: Path, output: Path, timeout: int, isolated: bool) -> tuple[int, float]:
    # Build commands are reviewed source; do not interpolate them into a shell.
    if isolated:
        # Preserve the venv entry point: resolving its symlink selects the base
        # interpreter and silently discards pyvenv.cfg and installed packages.
        executable = os.path.abspath(cwd / command[0]) if "/" in command[0] else shutil.which(command[0])
        if executable is None:
            raise ValueError(f"Fuzz executable is unavailable: {command[0]}")
        environment = [f"{key}={os.environ[key]}" for key in (
            "PATH", "HOME", "GOROOT", "GOPATH", "GOCACHE", "GOMODCACHE", "GOTOOLCHAIN",
            "RUSTUP_HOME", "CARGO_HOME", "LD_LIBRARY_PATH", "PYTHONPATH",
        ) if key in os.environ]
        command = ["sudo", "-n", "unshare", "--net", f"--setgid={os.getgid()}",
                   f"--setuid={os.getuid()}", "--", "/usr/bin/env", "-i", *environment,
                   "GOMEMLIMIT=1536MiB", executable, *command[1:]]
    start = time.monotonic()
    with output.open("wb") as log:
        # The default-branch manifest supplies reviewed argv; campaigns add network isolation.
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)  # nosec B603
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            code = 124
    return code, round(time.monotonic() - start, 3)


def run_target(root: Path, output: Path, target: dict) -> dict:
    name, seconds = target["name"], target["seconds"]
    target_dir = output / name
    target_dir.mkdir()
    corpus = target_dir / "corpus"
    if "corpus" in target:
        shutil.copytree(confined(root, target["corpus"]), corpus)
    else:
        corpus.mkdir()
    command = list(target["command"])
    if target["engine"] == "go":
        command += [f"-fuzztime={seconds}s", "-fuzzminimizetime=10s", "-parallel=2", f"-timeout={seconds + 60}s"]
    else:
        command += [f"-max_total_time={seconds}", "-timeout=10", "-rss_limit_mb=2048",
                    f"-artifact_prefix={target_dir}/", str(corpus)]
    cwd = confined(root, target.get("cwd", "."))
    code, elapsed = execute(command, cwd, target_dir / "run.log", seconds + 300, True)
    if code == 0 and not exercised(target["engine"], (target_dir / "run.log").read_text(errors="replace")):
        code = 125
    if target["engine"] == "go":
        for index, path in enumerate(cwd.rglob("testdata/fuzz")):
            if path.is_dir():
                shutil.copytree(path, target_dir / f"go-regressions-{index}")
    return {"name": name, "seconds": seconds, "exit_code": code, "elapsed_seconds": elapsed}


def campaign(root: Path, output: Path) -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_contract(event, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"],
                   os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"])
    data = manifest(root)
    # Fixed read-only Git query using the runner's selected toolchain.
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()  # nosec B603, B607
    if actual != os.environ["GITHUB_SHA"]:
        raise ValueError("Fuzz checkout differs from RC source")
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": 1, "repository": os.environ["GITHUB_REPOSITORY"], "source_sha": actual,
        "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "channel": "rc", "private": False, "network": "isolated", "status": "failed",
        "manifest_sha256": hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest(),
        "targets": [],
    }
    try:
        for index, command in enumerate(data["setup"]):
            code, _ = execute(command, root, output / f"setup-{index}.log", 600, False)
            if code:
                raise ValueError(f"Fuzz build/setup {index} failed with {code}")
        for target in data["targets"]:
            report["targets"].append(run_target(root, output, target))
        if any(t["exit_code"] for t in report["targets"]):
            raise ValueError("One or more fuzz targets failed; inspect diagnostics")
        report["status"] = "passed"
    finally:
        (output / REPORT).write_text(json.dumps(report, indent=2) + "\n")
    print(f"RC fuzzing passed: {len(report['targets'])} targets")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "run"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "inspect":
        data = manifest(root)
        event_contract(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
                       os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"],
                       os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"language={data['language']}\ngo_version_file={data.get('go-version-file', '')}\n")
    else:
        if args.output is None:
            parser.error("run requires --output")
        campaign(root, args.output.resolve())


if __name__ == "__main__":
    main()
