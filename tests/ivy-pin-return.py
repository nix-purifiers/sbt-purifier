"""Bounded local Ivy pin/Nix/pin experiment; retained failures are not passes."""
import argparse
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


parser = argparse.ArgumentParser()
parser.add_argument("--state", required=True, type=Path)
parser.add_argument("--producer-source", required=True, type=Path)
parser.add_argument("--native-fixture", required=True, type=Path)
parser.add_argument("--tools", required=True, type=Path)
parser.add_argument("--runtime-directory", required=True, type=Path,
                    help="Private short workspace path for Unix sockets")
args = parser.parse_args()
state = args.state.resolve()
state.mkdir(parents=True)
logs = state / "logs"
logs.mkdir()
source = args.producer_source.resolve()
tools = json.loads(args.tools.read_text())
assert tools["system"] == "x86_64-linux" and tools["sbtVersion"] == "1.13.1"
receipt = {"status": "running", "producerSource": str(source), "driverSha256": sha(Path(__file__)),
           "tools": tools, "phases": [], "scope": "local host capture and separately sandboxed replay"}
deadline = time.monotonic() + 45 * 60
env = dict(os.environ)
for key, relative in {"HOME": "home", "TMPDIR": "tmp", "XDG_CACHE_HOME": "xdg/cache",
                      "XDG_CONFIG_HOME": "xdg/config", "XDG_STATE_HOME": "xdg/state",
                      "XDG_DATA_HOME": "xdg/data"}.items():
    env[key] = str(state / relative)
    Path(env[key]).mkdir(parents=True, exist_ok=True)
runtime = args.runtime_directory.resolve()
runtime.mkdir(parents=True, mode=0o700)
assert len(str(runtime).encode()) < 50, "sbt boot socket suffix needs a short runtime path"
env["XDG_RUNTIME_DIR"] = str(runtime)
receipt["runtimeDirectory"] = str(runtime)


def save():
    (state / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def run(argv, label, cwd=state, success=True, timeout=600):
    print("RUN", label, flush=True)
    log = logs / (label + ".log")
    error = logs / (label + ".stderr")
    with log.open("w") as out, error.open("w") as err:
        process = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=env,
                                   stdout=out, stderr=err, start_new_session=True)
        try:
            code = process.wait(timeout=min(timeout, max(1, deadline - time.monotonic())))
        except BaseException:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
    if code:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
    receipt["phases"].append({"label": label, "argv": list(map(str, argv)), "cwd": str(cwd), "expectedSuccess": success,
                              "exitCode": code, "stdoutSha256": sha(log), "stderrSha256": sha(error)})
    save()
    if (code == 0) != success:
        raise AssertionError(f"{label}: unexpected exit {code}; see {log} and {error}")
    return log.read_text()


repo = state / "repository"
shutil.copytree(args.native_fixture / "fixture-repository", repo)
for path in [repo, *repo.rglob("*")]:
    path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o200))
foreign = repo / "alien/leaf/2"
foreign.mkdir(parents=True)
shutil.copyfile(repo / "proof/leaf/2/leaf-2.jar", foreign / "leaf-2.jar")
(foreign / "leaf-2.pom").write_text('<project><modelVersion>4.0.0</modelVersion><groupId>alien</groupId><artifactId>leaf</artifactId><version>2</version></project>')
assert sha(foreign / "leaf-2.jar") == sha(repo / "proof/leaf/2/leaf-2.jar")
sdk_root = Path(tools["cache"]) / "https/repo1.maven.org/maven2"
requests = []


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def translate_path(self, path):
        requests.append(path)
        if path.startswith("/maven2/"):
            return str(sdk_root / path.removeprefix("/maven2/"))
        return super().translate_path(path)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(repo)))
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"
env["SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR"] = origin + "/maven2/"
driver_root = Path(__file__).resolve().parent
provider = state / "provider"
provider.mkdir()
shutil.copyfile(driver_root / "ivy-pin-provider.nix", provider / "provider.nix")
(provider / "flake.nix").write_text('''{
 inputs.backend.url = "path:SOURCE";
 outputs = { self, backend }: let
   system = "x86_64-linux";
   pkgs = import backend.inputs.nixpkgs { inherit system; };
 in { packages.${system}.default = import ./provider.nix {
   inherit pkgs; purifier = backend.inputs.purifier.lib.purifier;
 }; };
}
'''.replace("SOURCE", str(source)))


def native(project, label, commands, success=True):
    home = state / (project.name + "-native")
    home.mkdir(exist_ok=True)
    boot = home / "repositories"
    boot.write_text("[repositories]\nlocked: file:" + str(sdk_root) + "\n")
    inject = home / "plugins"
    inject.mkdir(exist_ok=True)
    shutil.copyfile(tools["inject"], inject / "backend.sbt")
    argv = [Path(tools["jdk"]) / "bin/java", "-XX:ActiveProcessorCount=2", "-XX:-UsePerfData", "-Xmx1g",
            "-Djava.io.tmpdir=" + env["TMPDIR"],
            "-Duser.home=" + str(home), "-Dsbt.boot.directory=" + str(home / "boot"),
            "-Dsbt.global.base=" + str(home / "global"), "-Dsbt.ivy.home=" + str(home / "ivy"),
            "-Dsbt.repository.config=" + str(boot), "-Dsbt.global.plugins=" + str(inject),
            "-Dsbt.nix.backend.inject=" + str(inject / "backend.sbt"),
            "-Dsbt.nix.backend.repository=" + tools["repository"],
            "-Dsbt.nix.backend.nixpkgs=" + tools["nixpkgs"],
            "-Dsbt.server.autostart=false", "-Dsbt.log.noformat=true", "-jar",
            Path(tools["cache"]) / tools["launcher"], *commands]
    return run(argv, label, project, success)


def project_files(project, cached, nix=False, foreign=False):
    (project / "project").mkdir(parents=True, exist_ok=True)
    (project / "project/build.properties").write_text("sbt.version=1.13.1\n")
    (project / "project/build.sbt").write_text("useCoursier := false\n")
    (project / "build.sbt").write_text((driver_root / "ivy-pin-observe.sbt").read_text() + '\n' + '''ThisBuild / useCoursier := false
ThisBuild / scalaVersion := "2.12.21"
lazy val app = project.in(file(".")).settings(
 organization := "proof", name := "pin-return", version := "1",
 autoScalaLibrary := false, managedScalaInstance := false, crossPaths := false,
 resolvers += "controlled" at ORIGIN,
 externalResolvers := resolvers.value,
 updateOptions := updateOptions.value.withCachedResolution(CACHED),
 libraryDependencies += "GROUP" % "leaf" % "2",
 pinExpectedMarker := "MARKER"
)
'''.replace("ORIGIN", json.dumps(origin)).replace("CACHED", str(cached).lower())
        .replace("GROUP", "alien" if foreign else "proof").replace("MARKER", "leaf-42" if nix or foreign else "leaf-2")
        + ('ThisBuild / nixFlakeRepositories += NixFlakeRepository("leaf", ' + json.dumps(provider_ref) + ')\n' if nix else ''))
    java = project / "src/main/java/proof/Consumer.java"
    java.parent.mkdir(parents=True, exist_ok=True)
    java.write_text('package proof; public class Consumer { public static void main(String[] args) { System.out.println(Marker.value()); } }\n')


def loaded(text):
    rows = [line.split("\t")[1:] for line in text.splitlines() if line.startswith("PIN_LOADED\t")]
    assert rows and all(row == rows[0] for row in rows)
    return rows[0]


def replay(project, label, wanted):
    snapshot = state / "snapshots" / label
    snapshot.mkdir(parents=True)
    for name in ("build.sbt", "project", "src"):
        item = project / name
        if item.is_dir():
            shutil.copytree(item, snapshot / name, ignore=shutil.ignore_patterns("target", "project"))
        else:
            shutil.copyfile(item, snapshot / name)
    lock = project / "sbt-purifier.lock.json"
    saved = state / (label + "-lock.json")
    saved.write_bytes(lock.read_bytes())
    expr = f"import {driver_root}/ivy-capture-replay.nix {{ producerSource = {source}; projectSource = {snapshot}; lockFile = {saved}; }}"
    binding = json.loads(run(["nix", "eval", "--impure", "--json", "--expr", f"let b = {expr}; in {{ app = b.app.drvPath; output = b.app.outPath; tests = b.tests.drvPath; cache = b.dependencies.outPath; }}"], label + "-bindings"))
    run(["nix", "derivation", "show", binding["app"], binding["tests"]], label + "-derivations")
    run([Path(tools["cli"]) / "bin/sbt-purifier", "prefetch", "--lockfile", saved], label + "-prefetch", project)
    run(["nix", "build", "--impure", "--expr", f"({expr}).tests", "--option", "sandbox", "true", "--out-link", state / (label + "-result")], label + "-build")
    text = run(["nix", "log", binding["app"]], label + "-native-log")
    # nix log writes the builder log to stderr.
    text += (logs / (label + "-native-log.stderr")).read_text()
    observation = loaded(text)
    sdk = [line.split("\t")[1:] for line in text.splitlines() if line.startswith("PIN_SDK\t")]
    assert len(sdk) == 1 and sdk[0][0:4] == ["1.13.1", "21.0.12.1+1-nixos", "false", str(cached).lower()]
    assert observation[0:2] == ["proof", "leaf"] and observation[3] == wanted
    assert observation[2].startswith("42-nix-") if wanted == "leaf-42" else observation[2] == "2"
    expected_jar = provider_jar if wanted == "leaf-42" else repo / "proof/leaf/2/leaf-2.jar"
    assert observation[5] == sha(expected_jar)
    info = json.loads(run(["nix", "path-info", "--json", binding["output"], state / (label + "-result")], label + "-output-info"))
    assert lock.read_bytes() == saved.read_bytes()
    receipt.setdefault("replays", []).append({"label": label, "cachedResolution": cached, "bindings": binding,
        "loaded": observation, "observedSdk": sdk[0], "lockSha256": sha(saved), "outputInfo": info})
    save()
    return json.loads(saved.read_text())


try:
    receipt["driverSourceNar"] = run(["nix", "hash", "path", driver_root.parent], "driver-source-nar").strip()
    receipt["producerSourceNar"] = run(["nix", "hash", "path", source], "producer-source-nar").strip()
    run([Path(tools["jdk"]) / "bin/java", "-XX:-UsePerfData", "-Duser.home=" + env["HOME"],
         "-Djava.io.tmpdir=" + env["TMPDIR"], "-XshowSettings:properties", "-version"], "java-runtime")
    run(["nix", "flake", "lock", "path:" + str(provider)], "provider-lock")
    archive = json.loads(run(["nix", "flake", "archive", "--json", "--no-update-lock-file", "path:" + str(provider)], "provider-archive"))
    def physical(node):
        path = Path(node["path"])
        assert path.is_dir() and str(path.resolve()) == str(path)
        for child in node.get("inputs", {}).values():
            physical(child)
    physical(archive)
    provider_ref = "path:" + archive["path"] + "#default"
    provider_out = Path(run(["nix", "build", provider_ref, "--print-out-paths", "--out-link", state / "provider-result"], "provider-build").strip())
    provider_jar = provider_out / "proof/leaf/42/leaf-42.jar"
    shutil.copyfile(provider_jar, foreign / "leaf-2.jar")
    assert sha(foreign / "leaf-2.jar") == sha(provider_jar)
    receipt["provider"] = {"archive": archive, "narHash": run(["nix", "hash", "path", archive["path"]], "provider-nar").strip(),
                           "jarSha256": sha(provider_jar), "reference": provider_ref}
    for cached in (False, True):
        project = state / f"consumer-{cached}"
        baseline = None
        baseline_bytes = None
        for suffix, nix in (("regular", False), ("nix", True), ("return", False)):
            label = f"{cached}-{suffix}"
            project_files(project, cached, nix=nix)
            native(project, label + "-capture", ["nixRelock"])
            lock = replay(project, label, "leaf-42" if nix else "leaf-2")
            lock_bytes = (state / (label + "-lock.json")).read_bytes()
            artifacts = [a for a in lock["artifacts"] if "/proof/leaf/" in a["url"]]
            if nix:
                assert not artifacts, "Provided leaf leaked into downloaded artifacts"
                repositories = lock["flakeRepositories"]
                assert len(repositories) == 1 and repositories[0]["ref"] == provider_ref
                locked = urlsplit(repositories[0]["locked"])
                assert locked.path == archive["path"]
                assert parse_qs(locked.query)["narHash"] == [receipt["provider"]["narHash"]]
                assert [a for a in lock["artifacts"] if "/proof/leaf/" not in a["url"]] == [a for a in baseline["artifacts"] if "/proof/leaf/" not in a["url"]], "Nix transition changed SDK/bootstrap artifacts"
            elif baseline is None:
                baseline = lock
                baseline_bytes = lock_bytes
            else:
                assert lock_bytes == baseline_bytes, "Pin return did not restore the original lock bytes"
            receipt.setdefault("transitions", []).append({"label": label, "cachedResolution": cached,
                "predicates": "passed", "lockSha256": sha(state / (label + "-lock.json"))})
            save()
        foreign_project = state / f"foreign-{cached}"
        project_files(foreign_project, cached, foreign=True)
        text = native(foreign_project, f"{cached}-foreign-coordinate", ["compile", "pinObserve"], success=False)
        assert "PIN_WRONG_COORDINATE" in text and loaded(text)[0] == "alien"
        assert loaded(text)[3] == "leaf-42" and loaded(text)[5] == sha(provider_jar)
        receipt.setdefault("foreignCoordinateNegatives", []).append({"cachedResolution": cached,
            "predicates": "passed", "loaded": loaded(text), "expectedNativeFailure": "PIN_WRONG_COORDINATE"})
        save()
    receipt["status"] = "passed"
except BaseException as error:
    receipt["status"] = "failed"
    receipt["error"] = repr(error)
    raise
finally:
    server.shutdown()
    server.server_close()
    (state / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")
    save()
