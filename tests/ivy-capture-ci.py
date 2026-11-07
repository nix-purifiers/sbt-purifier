"""Bounded native Linux CI entry point; preserve source, failures and role proofs."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--results", required=True, type=Path)
args = parser.parse_args()
results = args.results.resolve()
results.mkdir(parents=True, exist_ok=False)
root = Path(__file__).resolve().parent.parent
started = time.monotonic()
deadline = started + 90 * 60
env = dict(os.environ, HOME=str(results / "home"), TMPDIR=str(results / "tmp"),
           XDG_CACHE_HOME=str(results / "cache"), XDG_CONFIG_HOME=str(results / "config"),
           XDG_STATE_HOME=str(results / "state"), XDG_DATA_HOME=str(results / "data"))
for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
    Path(env[key]).mkdir(parents=True)
commands = []
receipt = {"status": "running", "commands": commands, "scope": "native host capture/oracles and sandboxed replay retain distinct roles"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stop_owned_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run(command, label, timeout=3600, raw=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, 0)
    print(label, flush=True)
    stdout = results / (label + (".nar" if raw else ".stdout"))
    stderr = results / (label + ".stderr")
    row = {"phase": label, "command": list(map(str, command)), "status": "running"}
    commands.append(row)
    try:
        with stdout.open("wb" if raw else "w") as out, stderr.open("w") as err:
            process = subprocess.Popen(list(map(str, command)), cwd=root, env=env,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                code = process.wait(timeout=min(timeout, remaining))
            except BaseException:
                stop_owned_group(process)
                raise
        row.update(status="passed" if code == 0 else "failed", exitCode=code)
        if code:
            stop_owned_group(process)
            print(stderr.read_text(), file=sys.stderr, flush=True)
            raise subprocess.CalledProcessError(code, command)
        return stdout if raw else stdout.read_text()
    except BaseException as error:
        row.update(status="timed-out" if isinstance(error, subprocess.TimeoutExpired) else "failed",
                   error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        if stdout.exists():
            row["stdoutSha256"] = sha(stdout)
        if stderr.exists():
            row["stderrSha256"] = sha(stderr)
        (results / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


try:
    revision = run(["git", "rev-parse", "HEAD"], "revision", 30).strip()
    assert not run(["git", "diff", "--binary", "HEAD"], "tracked-diff", 30).strip(), "CI evidence requires committed source"
    assert not run(["git", "ls-files", "--others", "--exclude-standard"], "untracked-files", 30).strip(), "CI evidence requires all test files tracked"
    original_lock = (root / "flake.lock").read_bytes()
    reference = "git+file://" + str(root) + "?rev=" + revision
    archived = json.loads(run(["nix", "flake", "archive", "--json", "--no-update-lock-file", reference], "source-archive"))
    assert (root / "flake.lock").read_bytes() == original_lock
    source = Path(archived["path"])
    paths = set()
    def physical_inputs(node):
        path = Path(node["path"])
        assert path.is_dir() and path.resolve() == path
        paths.add(path)
        for child in node.get("inputs", {}).values():
            physical_inputs(child)
    physical_inputs(archived)
    (results / "gcroots").mkdir()
    for index, path in enumerate(sorted(paths)):
        run(["nix-store", "--add-root", results / "gcroots" / str(index), "--realise", path], "source-root-" + str(index))
    inventory = []
    tree = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", revision], cwd=root, env=env, timeout=30)
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        name = name.decode()
        assert kind == "blob"
        path = source / name
        if mode == "120000":
            value = os.readlink(path).encode()
        else:
            assert path.is_file() and not path.is_symlink()
            assert bool(path.stat().st_mode & 0o111) == (mode == "100755")
            value = path.read_bytes()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("source inventory", 0)
        expected = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=root, env=env, timeout=min(30, remaining))
        assert value == expected, name
        inventory.append({"path": name, "mode": mode, "sha256": hashlib.sha256(value).hexdigest()})
    (results / "source-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    system = run(["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"], "system", 60).strip()
    assert system in {"x86_64-linux", "aarch64-linux"}
    host = os.uname()
    machines = {"x86_64-linux": {"x86_64", "amd64"}, "aarch64-linux": {"aarch64", "arm64"}}
    assert host.sysname == "Linux" and host.machine.lower() in machines[system], "A native runner is required"
    source_nar = run(["nix", "hash", "path", source], "source-nar", 60).strip()
    source_dump = run(["nix-store", "--dump", source], "source", 60, raw=True)
    assert "sha256-" + base64.b64encode(hashlib.sha256(source_dump.read_bytes()).digest()).decode() == source_nar
    receipt.update(revision=revision, reference=reference, source=str(source), sourceNar=source_nar,
                   system=system, host={"system": host.sysname, "machine": host.machine, "release": host.release},
                   sourceInventorySha256=sha(results / "source-inventory.json"))
    expression = f'''let f = builtins.getFlake {json.dumps(str(source))}; c = f.checks.{system}; in {{
 native = {{ drv = c.ivy-native-state.drvPath; out = c.ivy-native-state.outPath; }};
 mutable = {{ drv = c.realize-mutable-metadata.drvPath; out = c.realize-mutable-metadata.outPath; }}; }}'''
    checks = json.loads(run(["nix", "eval", "--impure", "--json", "--expr", expression], "check-bindings"))
    run(["nix", "build", f"path:{source}#checks.{system}.ivy-native-state",
         f"path:{source}#checks.{system}.realize-mutable-metadata", "--option", "sandbox", "true",
         "--out-link", results / "checks-result"], "exported-checks")
    for name, check in checks.items():
        shutil.copytree(check["out"], results / ("check-" + name), symlinks=True)
    receipt["exportedChecks"] = checks
    env["IVY_CAPTURE_DEADLINE_MONOTONIC"] = str(deadline - 120)
    run([sys.executable, "-B", source / "tests/ivy-capture.py", "--state", results / "capture",
         "--producer-source", source, "--native-fixture", checks["native"]["out"]], "capture", timeout=90 * 60)
    phases = json.loads((results / "capture/phases.json").read_text())
    assert len(phases) == 24
    assert all(sum(p["cachedResolution"] == mode for p in phases) == 12 for mode in (False, True))
    by_name = {p["phase"]: p for p in phases}
    expected = json.loads(Path(checks["native"]["out"]).joinpath("receipt.json").read_text())["expectedJarSha256"]
    roles = [check["out"] for check in checks.values()]
    derivations = {check["drv"] for check in checks.values()}
    fixed_files = {}
    for phase in phases:
        name = phase["phase"]
        if "bindings" not in phase:
            assert phase["exitCode"] == 0
            assert sha(results / "capture/logs" / (name + ".log")) == phase["logSha256"]
            continue
        binding = phase["bindings"]
        roles.append(binding["dependencies"])
        derivations.update([binding["app"]["drv"], binding["tests"]])
        log = results / "capture/logs" / (name + "-native-replay.log")
        assert sha(log) == phase["nativeLogSha256"]
        lock = results / "capture/locks" / (name + "-replay.json")
        assert sha(lock) == phase["lockSha256"]
        for entry in json.loads(lock.read_text())["artifacts"]:
            file = (Path(binding["dependencies"]) / entry["path"]).resolve(strict=True)
            if file not in fixed_files:
                fixed_files[file] = "sha256-" + base64.b64encode(hashlib.sha256(file.read_bytes()).digest()).decode()
            assert fixed_files[file] == entry["hash"]
        if phase["negativeMissingRecord"]:
            assert phase["exitCode"] != 0 and not re.search(r"^IVY_CAPTURE_LOADED ", log.read_text(), re.M)
            assert "download failed: proof#leaf;3!leaf.jar" in log.read_text()
        else:
            assert phase["exitCode"] == 0
            revision = phase["expectedRevision"]
            assert re.search(r"^IVY_CAPTURE_LOADED leaf-" + revision + " " + expected["leaf:" + revision] + r" file:", log.read_text(), re.M)
            roles += [binding["app"]["out"], binding["dependencies"], str((results / "capture" / (name + "-result")).resolve(strict=True))]
    for cached in ("False", "True"):
        old = by_name[cached + "-initial-lock"]["lockSha256"]
        new = by_name[cached + "-forced-relock"]["lockSha256"]
        assert old != new
        for suffix in ("repeat-lock", "after-publication-noop"):
            assert by_name[cached + "-" + suffix]["lockSha256"] == old
        for suffix in ("after-relock-noop", "forced-update", "restored-selected"):
            assert by_name[cached + "-" + suffix]["lockSha256"] == new
    info = json.loads(run(["nix", "path-info", "--json", *sorted(set(roles))], "selected-output-info"))
    selected = [(info["storeDir"] + "/" + name, entry) for name, entry in info["info"].items()] if info.get("version") == 2 else list(info.items())
    archives = []
    for index, (path, entry) in enumerate(selected):
        assert run(["nix", "hash", "path", path], "selected-nar-" + str(index), 60).strip() == entry["narHash"]
        dump = run(["nix-store", "--dump", path], "selected-" + str(index), 60, raw=True)
        assert "sha256-" + base64.b64encode(hashlib.sha256(dump.read_bytes()).digest()).decode() == entry["narHash"]
        archives.append({"path": path, "archive": dump.name, "narHash": entry["narHash"]})
    run(["nix", "derivation", "show", *sorted(derivations)], "selected-derivations")
    run(["nix", "--version"], "nix-version", 30)
    tools = json.loads((results / "capture/tools.json").read_text())
    run([Path(tools["jdk"]) / "bin/java", "-Duser.home=" + env["HOME"], "-Djava.io.tmpdir=" + env["TMPDIR"], "-XshowSettings:properties", "-version"], "java-runtime", 30)
    java_properties = dict(line.strip().split(" = ", 1) for line in (results / "java-runtime.stderr").read_text().splitlines() if " = " in line)
    assert java_properties["os.arch"].lower() in machines[system]
    assert java_properties["java.version"].split(".", 1)[0] == "21"
    receipt.update(status="passed", phaseCount=24, distinctFixedOutputFilesHashed=len(fixed_files),
                   selectedOutputCount=len(selected), selectedOutputArchives=archives, tools=tools,
                   observedJava={key: java_properties[key] for key in ("java.version", "java.runtime.version", "java.vendor", "os.arch")})
except BaseException as error:
    receipt.update(status="failed", error=type(error).__name__ + ": " + str(error))
    raise
finally:
    if receipt["status"] != "passed":
        # A separate bounded diagnostic grace preserves evidence without
        # rerunning a failed operation or masking its original exception.
        deadline = time.monotonic() + 60
        partial = []
        for path in sorted((results / "capture").glob("*-bindings.json"), key=lambda p: p.stat().st_mtime_ns):
            try:
                binding = json.loads(path.read_text())
                if isinstance(binding, dict) and isinstance(binding.get("app"), dict) and all(key in binding for key in ("tests", "dependencies")) and all(key in binding["app"] for key in ("drv", "out")):
                    partial.append(binding)
            except (ValueError, OSError):
                pass
        partial_commands = []
        if partial:
            partial_commands.append((["nix", "log", partial[-1]["app"]["drv"]], "partial-latest-native-log"))
            drvs = sorted({p["app"]["drv"] for p in partial} | {p["tests"] for p in partial})
            partial_commands.append((["nix", "derivation", "show", *drvs], "partial-selected-derivations"))
            outputs = sorted({p[k] for p in partial for k in ("dependencies",) if Path(p[k]).exists()}
                             | {p["app"]["out"] for p in partial if Path(p["app"]["out"]).exists()})
            if outputs:
                partial_commands.append((["nix", "path-info", "--json", *outputs], "partial-selected-output-info"))
        for command, label in partial_commands:
            try:
                run(command, label, timeout=15)
            except BaseException as error:
                receipt.setdefault("diagnosticErrors", []).append(type(error).__name__ + ": " + str(error))
    receipt["elapsedSeconds"] = round(time.monotonic() - started, 3)
    (results / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
