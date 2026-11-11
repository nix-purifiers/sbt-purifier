#!/usr/bin/env python3
"""Bounded real-source SBT implementation lifecycle; preparation is not execution proof."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True, help="real immutable source B flake reference")
    parser.add_argument("--baseline", required=True, help="B's baseline-A JSON containing source")
    parser.add_argument("--fixture", required=True, help="real multi fixture with a legacy SBT lock")
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--source-model", required=True)
    parser.add_argument("--local-source", help="pre-publication Git mirror; exact A/B rev and NAR required")
    parser.add_argument("--workdir", required=True, help="new retained directory; filesystem root is rejected")
    parser.add_argument("--runtime-directory", help="optional new private socket directory shorter than 50 bytes")
    parser.add_argument("--output", help="additional receipt destination inside workdir")
    args = parser.parse_args()
    work = Path(args.workdir).resolve()
    assert work != Path(work.anchor), "workdir must be a new non-root directory"
    work.mkdir(parents=True, exist_ok=False)
    logs = work / "logs"
    logs.mkdir()
    environment = dict(os.environ)
    for key, relative in {"HOME": "home", "TMPDIR": "tmp", "XDG_CACHE_HOME": "xdg/cache",
                          "XDG_CONFIG_HOME": "xdg/config", "XDG_DATA_HOME": "xdg/data",
                          "XDG_STATE_HOME": "xdg/state"}.items():
        directory = work / relative
        directory.mkdir(parents=True, exist_ok=True)
        environment[key] = str(directory)
    if args.runtime_directory:
        runtime = Path(args.runtime_directory).resolve()
        assert runtime != Path(runtime.anchor) and len(str(runtime).encode()) < 50
        runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
    else:
        # CI uses a short private socket path even when its workspace is long.
        # Local callers with a narrower write policy supply --runtime-directory.
        runtime = Path(tempfile.mkdtemp(prefix="sbt-pin-", dir="/tmp")).resolve()
        assert len(str(runtime).encode()) < 50
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    environment["JAVA_OPTS"] = (environment.get("JAVA_OPTS", "") + " -XX:-UsePerfData"
                                + " -Duser.home=" + environment["HOME"]
                                + " -Djava.io.tmpdir=" + environment["TMPDIR"])
    for key in ("SBT_PURIFIER_LOCKFILE", "SBT_PURIFIER_IMPLEMENTATION_CONFIG", "SBT_NIX_MODE"):
        environment.pop(key, None)
    assert environment.get("SBT_NIX_BACKEND", "1") not in {"0", "false"}, "backend bypass is not a pin test"
    deadline = time.monotonic() + 90 * 60
    receipt = {"status": "running", "scriptSha256": sha(__file__), "commands": [],
               "workdir": str(work), "runtimeDirectory": str(runtime),
               "transport": "local-git" if args.local_source else "public-github",
               "bounds": {"wholeSeconds": 5400, "bootstrapSeconds": 1200,
                          "nativeSeconds": 600, "pureSeconds": 900},
               "scope": "Real source A/B packages, host wrapper/plugin observations and sandbox build are separate roles"}

    def save():
        text = json.dumps(receipt, indent=2) + "\n"
        (work / "receipt.json").write_text(text)
        if args.output:
            output = Path(args.output).resolve()
            assert output.is_relative_to(work)
            output.write_text(text)

    def run(label, arguments, cwd=work, env=None, success=True, timeout=600):
        assert not any(item["label"] == label for item in receipt["commands"])
        remaining = deadline - time.monotonic()
        assert remaining > 0, "whole lifecycle budget exhausted"
        print("RUN", label, flush=True)
        stdout, stderr = logs / (label + ".stdout"), logs / (label + ".stderr")
        failure = None
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(list(map(str, arguments)), cwd=cwd, env=env or environment,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                code = process.wait(timeout=min(timeout, remaining))
            except BaseException as error:
                failure = error
                # Kill the entire owned process group even if its leader exits first.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                code = process.returncode
        receipt["commands"].append({"label": label, "argv": list(map(str, arguments)),
            "cwd": str(cwd), "exitCode": code, "stdoutSha256": sha(stdout), "stderrSha256": sha(stderr)})
        save()
        if failure is not None:
            raise failure
        if success:
            assert code == 0, f"{label}: exit {code}; see retained logs"
        return code, stdout.read_text(), stderr.read_text()

    def one_path(label, arguments):
        paths = run(label, arguments, timeout=1200)[1].strip().splitlines()
        assert len(paths) == 1 and Path(paths[0]).is_absolute()
        return Path(paths[0])

    try:
        spec = importlib.util.spec_from_file_location("purifier_bootstrap", args.bootstrap)
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        receipt["bootstrapSha256"] = sha(args.bootstrap)
        receipt["sourceModelSha256"] = sha(args.source_model)
        source_a = json.loads(Path(args.baseline).read_text())["source"]
        bootstrap.validate_source(source_a)
        current_cli = one_path("build-B-cli", ["nix", "build", args.current + "#sbt-purifier", "--no-link", "--print-out-paths"])
        current_native = one_path("build-B-native", ["nix", "build", args.current + "#sbt", "--no-link", "--print-out-paths"])
        config = json.loads((current_cli / "share/purifier/implementation.json").read_text())
        source_b = bootstrap.validate_source(config["source"])
        native_config = json.loads((current_native / "share/purifier/implementation.json").read_text())
        assert bootstrap.same_source(native_config["source"], source_b)
        assert config["tool"] == "sbt-purifier" and config["checksum"] == "sbt"
        assert not bootstrap.same_source(source_a, source_b), "A/B must be genuinely distinct immutable sources"
        original_sources = {"A": source_a, "B": source_b}
        if args.local_source:
            effective_sources = []
            for label, source in (("A", source_a), ("B", source_b)):
                locator = "git+" + Path(args.local_source).resolve().as_uri() + "?rev=" + source["rev"]
                metadata = json.loads(run("mirror-" + label, ["nix", "flake", "metadata", "--json", "--no-write-lock-file", locator], timeout=120)[1])
                effective = {key: metadata["locked"][key] for key in ("type", "url", "rev", "narHash")}
                assert bootstrap.same_source(effective, source)
                effective_sources.append(effective)
            source_a, source_b = effective_sources
        receipt.update(publicSources=original_sources, effectiveSources={"A": source_a, "B": source_b},
                       system=config["system"], currentPackages=list(map(str, (current_cli, current_native))))
        project = work / "project"
        shutil.copytree(args.fixture, project, ignore=shutil.ignore_patterns("target", ".git"))
        for item in [project, *project.rglob("*")]:
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | (0o700 if item.is_dir() else 0o200))
        lock = project / "sbt-purifier.lock.json"

        def read_lock(file=lock):
            return bootstrap.read_lock(file.read_text(), "sbt")

        legacy = read_lock()
        assert legacy.get("implementation") is None
        assert re.match(r"^[12]\.", legacy["sbtVersion"]), "this script probes genuine SBT1 or SBT2 fixtures"
        properties = (project / "project/build.properties").read_text()
        declared = re.search(r"(?m)^\s*sbt\.version\s*=\s*(\S+)\s*$", properties)
        assert declared and declared[1] == legacy["sbtVersion"], "fixture SDK and captured SDK differ"
        receipt.update(sbtVersion=legacy["sbtVersion"], fixture=str(Path(args.fixture).resolve()),
                       nativeInvocation="ordinary wrapper default; no explicit foreground override",
                       daemonReuseClaim=False)
        pin_a = {"version": 1, "tool": "sbt-purifier", "source": source_a}
        # Use the stable bootstrap as a subprocess so package resolution is bounded and logged too.
        model_expression = bootstrap.nix_string(str(Path(args.source_model).resolve()))
        for entry, name in (("sbt-purifier", "A-cli"), ("sbt", "A-native")):
            expression = ("let r = (import " + model_expression + ").resolve "
                          + "(builtins.fromJSON " + bootstrap.nix_string(json.dumps(source_a)) + "); in r.flake.packages."
                          + bootstrap.nix_string(config["system"]) + "." + bootstrap.nix_string(entry))
            package = one_path("build-" + name, ["nix", "build", "--impure", "--expr", expression, "--no-link", "--print-out-paths"])
            bootstrap.verify_package(package, pin_a)
            if entry == "sbt-purifier":
                old_cli = package
            else:
                old_native = package
        receipt["baselinePackages"] = list(map(str, (old_cli, old_native)))
        legacy_bytes = lock.read_bytes()
        _, _, warning = run("legacy-warning-no-implicit-migration",
                            [current_cli / "bin/sbt-purifier", "implementation"], project)
        assert "does not pin" in warning and lock.read_bytes() == legacy_bytes
        run("migrate-legacy-to-A", [current_cli / "bin/sbt-purifier", "self-update", "--to", bootstrap.source_ref(source_a)], project)
        assert bootstrap.same_source(read_lock()["implementation"]["source"], source_a)
        assert read_lock()["artifacts"] == legacy["artifacts"], "migration changed captured dependency artifacts"
        allowed_changes = {"implementation", "contentHash", "inputsHash", "plugin"}
        assert {k: v for k, v in read_lock().items() if k not in allowed_changes} == {k: v for k, v in legacy.items() if k not in allowed_changes}

        def selected(source, label, external=None):
            command = [current_cli / "bin/sbt-purifier", "implementation"]
            if external:
                command += ["--lockfile", external]
            value = json.loads(run(label, command, project)[1])
            assert bootstrap.same_source(value["source"], source)
            return value

        def source_expression(source):
            return ("((import " + model_expression + ").resolve (builtins.fromJSON "
                    + bootstrap.nix_string(json.dumps(source)) + ")).flake")

        def origin(source, label, extra_actions=(), external=None):
            observation = '''eval { val enc = java.util.Base64.getEncoder; def emit(k: String, v: String) = println(k + "|" + enc.encodeToString(v.getBytes("UTF-8"))); emit("PIN_CONFIG", scala.io.Source.fromFile(sys.env("SBT_PURIFIER_IMPLEMENTATION_CONFIG")).mkString); emit("PIN_PLUGIN", sbtnixbackend.NixBackendPlugin.getClass.getProtectionDomain.getCodeSource.getLocation.toURI.toString); emit("PIN_INJECT", sys.props("sbt.nix.backend.inject")); emit("PIN_REPOSITORY", sys.props("sbt.nix.backend.repository")); emit("PIN_JAVA", sys.props("java.runtime.version")) }'''
            native_env = dict(environment)
            if external:
                native_env["SBT_PURIFIER_LOCKFILE"] = str(external)
            _, stdout, _ = run(label, [current_native / "bin/sbt", "-batch", *extra_actions,
                                       "show sbtVersion", observation], project, native_env)
            rows = {}
            for line in stdout.splitlines():
                match = re.search(r"\b(PIN_CONFIG|PIN_PLUGIN|PIN_INJECT|PIN_REPOSITORY|PIN_JAVA)\|([A-Za-z0-9+/=]+)$", line)
                if match:
                    assert match[1] not in rows
                    rows[match[1]] = base64.b64decode(match[2], validate=True).decode()
            assert len(rows) == 5
            assert bootstrap.same_source(json.loads(rows["PIN_CONFIG"])["source"], source)
            expected = json.loads(run(label + "-expected", ["nix", "eval", "--impure", "--json", "--expr",
                "let f=" + source_expression(source) + "; p=f.packages." + bootstrap.nix_string(config["system"])
                + ".sbt; in {repository=toString p.pluginRepository;inject=toString p.inject;}"])[1])
            assert rows["PIN_INJECT"] == expected["inject"] and rows["PIN_REPOSITORY"] == expected["repository"]
            actual_jar = Path(unquote(urlsplit(rows["PIN_PLUGIN"]).path))
            coordinates = read_lock(external or lock)["plugin"]
            assert actual_jar.is_file(), "plugin origin is not an actual packaged JAR"
            # Discover real published cross-version coordinates from POMs rather
            # than assuming SBT1's Scala suffix for an SBT2 runtime.
            repository = Path(expected["repository"]) / coordinates["organization"].replace(".", "/")
            matches = []
            for pom in sorted(repository.glob("*/" + coordinates["version"] + "/*.pom")):
                model = ET.parse(pom).getroot()
                group = model.findtext("{*}groupId")
                name = model.findtext("{*}artifactId") or ""
                version = model.findtext("{*}version")
                if (group != coordinates["organization"] or version != coordinates["version"]
                        or not (name == coordinates["name"] or name.startswith(coordinates["name"] + "_"))
                        or pom.name != name + "-" + version + ".pom"):
                    continue
                expected_jar = pom.with_suffix(".jar")
                if expected_jar.is_file() and sha(actual_jar) == sha(expected_jar):
                    matches.append({"group": group, "name": name, "version": version, "jar": str(expected_jar)})
            assert matches, "loaded plugin does not match any actual published plugin JAR in the selected repository"
            assert re.search(r"(?m)^\[info\]\s+" + re.escape(legacy["sbtVersion"]) + r"\s*$", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stdout))
            receipt.setdefault("origins", {})[label] = dict(rows, loadedJarSha256=sha(actual_jar), matchingPublishedModules=matches)
            save()

        def reject_client(label):
            original = lock.read_bytes()

            def rejected(suffix, expected_origin, flags=(), native_env=None):
                code, stdout, stderr = run(label + suffix,
                    [current_native / "bin/sbt", *flags, "-batch", "show sbtVersion"],
                    project, native_env, success=False, timeout=120)
                diagnostic = stdout + stderr
                assert code and "pinned projects require foreground mode" in diagnostic
                assert expected_origin in diagnostic and lock.read_bytes() == original

            rejected("-flag", "command arguments", ("--client",))
            rejected("-env", "SBT_NATIVE_CLIENT", native_env=dict(environment, SBT_NATIVE_CLIENT="true"))
            options_env = dict(environment, SBT_OPTS=environment.get("SBT_OPTS", "") + " --client")
            rejected("-SBT_OPTS", "SBT_OPTS", native_env=options_env)
            options_file = project / ".sbtopts"
            existed = options_file.exists()
            contents = options_file.read_bytes() if existed else b""
            original_mode = (options_file.stat().st_mode & 0o777) if existed else None
            try:
                separator = b"" if not contents or contents.endswith(b"\n") else b"\n"
                options_file.write_bytes(contents + separator + b"--jvm-client\n")
                rejected("-sbtopts", str(options_file))
            finally:
                if existed:
                    options_file.write_bytes(contents)
                    options_file.chmod(original_mode)
                else:
                    options_file.unlink(missing_ok=True)
            assert options_file.exists() == existed
            if existed:
                assert options_file.read_bytes() == contents and options_file.stat().st_mode & 0o777 == original_mode

        selected(source_a, "B-cli-selects-A")
        reject_client("pinned-A-client-rejected")
        origin(source_a, "B-native-runs-A-compile-test", ("compile", "test"))
        before_pin = read_lock()["implementation"]
        run("dependency-update-through-B", [current_cli / "bin/sbt-purifier", "update"], project)
        assert read_lock()["implementation"] == before_pin
        selected(source_a, "after-dependency-update-A")
        original_lock = lock.read_bytes()
        original_value = read_lock()

        def build_expression(source, external=None):
            return ("let f=" + source_expression(source) + "; pkgs=import f.inputs.nixpkgs { system="
                    + bootstrap.nix_string(config["system"]) + "; overlays=[f.overlays.default]; }; "
                    + "in (pkgs.sbt-purifier.buildSbtProject { src=/. + " + bootstrap.nix_string(str(project))
                    + "; lockFile=/. + " + bootstrap.nix_string(str(external or lock)) + "; }).tests")
        matched = run("matched-A-sandbox-tests", ["nix", "build", "--impure", "--no-link", "--print-out-paths",
                      "--option", "sandbox", "true", "--expr", build_expression(source_a)], timeout=900)[1].strip()
        assert Path(matched).is_dir()
        receipt["matchedBuild"] = matched
        receipt["matchedDerivation"] = run("matched-A-deriver", ["nix-store", "--query", "--deriver", matched])[1].strip()
        receipt["matchedOutputInfo"] = json.loads(run("matched-A-output-info", ["nix", "path-info", "--json", matched])[1])
        code, stdout, stderr = run("mismatched-B-builder", ["nix", "eval", "--impure", "--raw", "--expr",
                                  "(" + build_expression(source_b) + ").drvPath"], success=False)
        assert code and "builder implementation differs" in stdout + stderr
        target_b = bootstrap.source_ref(source_b) if args.local_source else "installed"
        run("explicit-self-update-B", [current_cli / "bin/sbt-purifier", "self-update", "--to", target_b], project)
        selected(source_b, "B-cli-selects-B")
        reject_client("pinned-B-client-rejected")
        value_b = read_lock()
        assert {k: v for k, v in value_b.items() if k not in allowed_changes} == {k: v for k, v in original_value.items() if k not in allowed_changes}
        assert value_b["artifacts"] == original_value["artifacts"]
        origin(source_b, "B-native-runs-B")
        run("explicit-return-A", [current_cli / "bin/sbt-purifier", "self-update", "--to", bootstrap.source_ref(source_a)], project)
        assert lock.read_bytes() == original_lock, "A return must restore exact native SBT lock bytes"
        selected(source_a, "restored-A-selected")
        external = work / "external.lock.json"
        lock.rename(external)
        selected(source_a, "external-cli-selects-A", external)
        origin(source_a, "external-native-runs-A", external=external)
        run("external-matched-builder", ["nix", "eval", "--impure", "--raw", "--expr",
                                        "(" + build_expression(source_a, external) + ").drvPath"])
        assert external.read_bytes() == original_lock and not lock.exists()
        external.rename(lock)
        receipt.update(status="passed", legacyWarningWithoutImplicitMigration=True,
                       legacyMigration=True, wrapperDispatchObserved=True,
                       orderingQualification="Observed B entrypoint selects A manifest, injector, repository and loaded JAR; temporal before-injection ordering also requires wrapper source review",
                       nativeCompileTest=True, observedPluginOrigin=True, dependencyUpdatePreservesPin=True,
                       pinnedClientFlagRejected=True, pinnedClientEnvironmentRejected=True,
                       pinnedClientOptionsEnvironmentRejected=True, pinnedClientProjectConfigRejected=True,
                       explicitSelfUpdatePreservesArtifacts=True, byteExactReturnA=True,
                       matchingSandboxBuild=True, mismatchedBuilderRejected=True, externalLock=True,
                       restoredLockSha256=sha(lock))
    except BaseException as error:
        receipt.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
