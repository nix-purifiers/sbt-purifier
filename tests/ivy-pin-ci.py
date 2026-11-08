"""Source-bound x86 Linux CI wrapper for the reviewed Ivy pin lifecycle fixture."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from composition_evidence import Evidence, sha256


parser = argparse.ArgumentParser()
parser.add_argument("--results", required=True, type=Path)
parser.add_argument("--runtime-directory", required=True, type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
evidence = Evidence(args.results, budget=75 * 60)
results = evidence.directory
env = dict(os.environ)
for key, name in {"HOME": "home", "TMPDIR": "tmp", "XDG_CACHE_HOME": "cache",
                  "XDG_CONFIG_HOME": "config", "XDG_STATE_HOME": "state",
                  "XDG_DATA_HOME": "data"}.items():
    env[key] = str(results / name)
    Path(env[key]).mkdir(parents=True)
roles = {}


def output(command, **kwargs):
    return evidence.output(command, cwd=root, env=env, **kwargs)


def sri(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def definition(document, drv):
    return document["derivations"][Path(drv).name] if "derivations" in document else document[drv]


def add_role(role, path, drv=None):
    path = str(Path(path).resolve(strict=True))
    assert path.startswith("/nix/store/")
    roles.setdefault(path, {"roles": [], "drvPaths": []})["roles"].append(role)
    if drv and drv not in roles[path]["drvPaths"]:
        roles[path]["drvPaths"].append(drv)


def retain_roles():
    archives = evidence.receipt.setdefault("selectedOutputArchives", [])
    for index, (selected, record) in enumerate(sorted(roles.items())):
        info = json.loads(output(["nix", "path-info", "--json", selected], timeout=60))
        if info.get("version") == 2:
            details = info["info"][Path(selected).name]
        else:
            details = info[selected]
        drv = details.get("deriver")
        if drv and not drv.startswith("/"):
            drv = str(Path(selected).parent / drv)
        if drv and drv.endswith(".drv"):
            if drv not in record["drvPaths"]:
                record["drvPaths"].append(drv)
            output(["nix", "derivation", "show", drv], timeout=60)
        nar = output(["nix-store", "--dump", selected], text=False, timeout=60)
        observed = output(["nix", "hash", "path", selected], timeout=60).strip()
        assert sri(nar) == observed == details["narHash"]
        archive = results / ("selected-" + str(index) + ".nar")
        archive.write_bytes(nar)
        archives.append({"path": selected, **record, "narHash": observed, "archive": archive.name})
        evidence.save()


try:
    with evidence.case():
        try:
            revision = output(["git", "rev-parse", "HEAD"], timeout=30).strip()
            assert not output(["git", "diff", "--binary", "HEAD"], timeout=30).strip(), "Committed source required"
            assert not output(["git", "ls-files", "--others", "--exclude-standard"], timeout=30).strip(), "All fixture files must be tracked"
            original_lock = (root / "flake.lock").read_bytes()
            reference = "git+file://" + str(root) + "?ref=" + revision + "&rev=" + revision
            archived = json.loads(output(["nix", "flake", "archive", "--json", "--no-update-lock-file", reference]))
            assert (root / "flake.lock").read_bytes() == original_lock
            paths = set()

            def physical(node):
                path = Path(node["path"])
                assert path.is_dir() and path.resolve() == path
                paths.add(path)
                for child in node.get("inputs", {}).values():
                    physical(child)

            physical(archived)
            (results / "gcroots").mkdir()
            for index, path in enumerate(sorted(paths)):
                output(["nix-store", "--add-root", results / "gcroots" / str(index), "--realise", path])
            source = Path(archived["path"])
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
                    actual = os.readlink(path).encode()
                else:
                    assert path.is_file() and not path.is_symlink()
                    assert bool(path.stat().st_mode & 0o111) == (mode == "100755")
                    actual = path.read_bytes()
                expected = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=root, env=env,
                                                   timeout=min(30, max(1, evidence.deadline - time.monotonic())))
                assert actual == expected, name
                inventory.append({"path": name, "mode": mode, "sha256": hashlib.sha256(actual).hexdigest()})
            (results / "source-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
            source_dump = output(["nix-store", "--dump", source], text=False, timeout=60)
            (results / "source.nar").write_bytes(source_dump)
            source_nar = output(["nix", "hash", "path", source], timeout=60).strip()
            assert sri(source_dump) == source_nar
            system = output(["nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"]).strip()
            host = os.uname()
            assert system == "x86_64-linux" and host.sysname == "Linux" and host.machine.lower() in {"x86_64", "amd64"}
            assert output(["nix", "config", "show", "sandbox"]).strip() == "true"
            evidence.receipt.update(revision=revision, reference=reference, source=str(source), sourceArchive=archived,
                sourceNar=source_nar, system=system, host={"system": host.sysname, "machine": host.machine, "release": host.release},
                sourceInventorySha256=sha256(results / "source-inventory.json"),
                scope="Native host captures/foreign-coordinate failures and actual sandboxed compile/test replays are separate roles",
                budget={"wrapperSeconds": 4500, "fixtureSeconds": 2700, "fixtureCommandSeconds": 2820})
            expression = f'''let f = builtins.getFlake {json.dumps(str(source))};
 p = import f.inputs.nixpkgs {{ system = builtins.currentSystem; overlays = [ f.overlays.default ]; }};
 sdk = p.sbt-purifier.buildSbtProject {{ src = {source}/tests/fixtures/multi; }};
 c = f.checks.${{builtins.currentSystem}}.ivy-native-state;
 in {{ system = builtins.currentSystem; sbtVersion = sdk.lock.sbtVersion; javaMajor = sdk.lock.javaVersion;
 inject = toString f.packages.${{builtins.currentSystem}}.sbt.inject;
 repository = toString f.packages.${{builtins.currentSystem}}.sbt.pluginRepository;
 cli = toString f.packages.${{builtins.currentSystem}}.sbt-purifier;
 jdk = sdk.jdk.home; nixpkgs = toString f.inputs.nixpkgs; cache = toString sdk.dependencies;
 launcher = sdk.lock.launcher; native = {{ drv = c.drvPath; out = c.outPath; }}; }}'''
            tools = json.loads(output(["nix", "eval", "--impure", "--json", "--expr", expression]))
            assert tools["sbtVersion"] == "1.13.1" and tools["javaMajor"] == "21" and tools["system"] == system
            (results / "tools.json").write_text(json.dumps(tools, indent=2) + "\n")
            output(["nix", "build", f"path:{source}#checks.{system}.ivy-native-state", f"path:{source}#sbt-purifier",
                    "--option", "sandbox", "true", "--out-link", results / "bootstrap-result"], timeout=20 * 60)
            assert evidence.deadline - time.monotonic() > 48 * 60, "Bootstrap exhausted the fixture and evidence reserve"
            output([sys.executable, "-B", source / "tests/ivy-pin-return.py", "--state", results / "experiment",
                    "--runtime-directory", args.runtime_directory.resolve(), "--producer-source", source,
                    "--native-fixture", tools["native"]["out"], "--tools", results / "tools.json"], timeout=47 * 60)
            experiment = results / "experiment"
            measured = json.loads((experiment / "receipt.json").read_text())
            assert measured["status"] == "passed" and len(measured["replays"]) == 6 and len(measured["transitions"]) == 6
            assert measured["driverSourceNar"] == source_nar == measured["producerSourceNar"]
            for command in measured["phases"]:
                assert (command["exitCode"] == 0) == command["expectedSuccess"]
                assert sha256(experiment / "logs" / (command["label"] + ".log")) == command["stdoutSha256"]
                assert sha256(experiment / "logs" / (command["label"] + ".stderr")) == command["stderrSha256"]
            negatives = measured["foreignCoordinateNegatives"]
            assert len(negatives) == 2 and {n["cachedResolution"] for n in negatives} == {False, True}
            assert all(n["expectedNativeFailure"] == "PIN_WRONG_COORDINATE" and n["loaded"][0] == "alien" and
                       n["loaded"][3] == "leaf-42" and n["loaded"][5] == measured["provider"]["jarSha256"] for n in negatives)
            physical(measured["provider"]["archive"])
            add_role("provider-source", measured["provider"]["archive"]["path"])
            add_role("native-fixture", tools["native"]["out"], tools["native"]["drv"])
            add_role("sdk-bootstrap-cache", tools["cache"])
            fixed_files = {}
            for replay in measured["replays"]:
                label = replay["label"]
                assert isinstance(replay["cachedResolution"], bool)
                binding = replay["bindings"]
                definitions = json.loads((experiment / "logs" / (label + "-derivations.log")).read_text())
                test_result = str((experiment / (label + "-result")).resolve(strict=True))
                for role, drv, selected in (("app", binding["app"], binding["output"]), ("tests", binding["tests"], test_result)):
                    subject = definition(definitions, drv)
                    assert subject["system"] == system and any(value.get("path") in (selected, Path(selected).name) for value in subject["outputs"].values())
                    add_role(label + "-" + role, selected, drv)
                add_role(label + "-cache", binding["cache"])
                lock_path = experiment / (label + "-lock.json")
                assert sha256(lock_path) == replay["lockSha256"]
                for artifact in json.loads(lock_path.read_text())["artifacts"]:
                    file = (Path(binding["cache"]) / artifact["path"]).resolve(strict=True)
                    fixed_files.setdefault(str(file), sri(file.read_bytes()))
                    assert fixed_files[str(file)] == artifact["hash"]
                jar = Path(replay["loaded"][4])
                assert sha256(jar) == replay["loaded"][5]
                if replay["loaded"][3] == "leaf-42":
                    selected = next(parent for parent in jar.parents if parent.parent == Path("/nix/store"))
                    add_role("provided-nix-projection", selected)
            retain_roles()
            evidence.receipt.update(complete=True, experimentReceiptSha256=sha256(experiment / "receipt.json"),
                successfulSandboxedReplays=6, nativeHostCaptures=6, nativeForeignCoordinateFailures=2,
                distinctFixedOutputFilesHashed=len(fixed_files),
                artifactScope="Selected source/provider/app/test/cache NARs; not the complete SDK/download closure")
        except BaseException as error:
            evidence.receipt["error"] = type(error).__name__ + ": " + str(error)
            # Preserve a bounded diagnostic snapshot without repeating a failed native operation.
            evidence.deadline = time.monotonic() + 60
            partial = []
            for path in sorted((results / "experiment/logs").glob("*-bindings.log")):
                try:
                    binding = json.loads(path.read_text())
                    if isinstance(binding, dict) and all(key in binding for key in ("app", "tests", "output", "cache")):
                        partial.append(binding)
                except (OSError, ValueError):
                    pass
            if partial:
                try:
                    output(["nix", "derivation", "show", *sorted({p[key] for p in partial for key in ("app", "tests")})], timeout=30)
                    for item in partial:
                        for key in ("output", "cache"):
                            if Path(item[key]).is_dir():
                                add_role("partial-" + key, item[key])
                    retain_roles()
                except BaseException as diagnostic:
                    evidence.receipt["diagnosticError"] = type(diagnostic).__name__ + ": " + str(diagnostic)
            raise
    evidence.receipt["status"] = "passed"
except BaseException:
    evidence.receipt["status"] = "failed"
    raise
finally:
    evidence.save()
