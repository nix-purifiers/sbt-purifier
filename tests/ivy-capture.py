"""Source-bound host capture and actual sandboxed replay of native Ivy locks."""
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
from urllib.parse import urlsplit


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


deadline = float(os.environ.get("IVY_CAPTURE_DEADLINE_MONOTONIC", time.monotonic() + 90 * 60))


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


def command(args, *, cwd, env=None, log=None, timeout=600, success=True):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    print("RUN " + " ".join(map(str, args)), flush=True)
    stream = log.open("w") if log else subprocess.DEVNULL
    errors = log.with_suffix(".stderr").open("w") if log and log.suffix == ".json" else subprocess.STDOUT
    process = subprocess.Popen(list(map(str, args)), cwd=cwd, env=env,
                               stdout=stream, stderr=errors, start_new_session=True)
    try:
        code = process.wait(timeout=min(timeout, remaining))
    except BaseException:
        stop_owned_group(process)
        raise
    finally:
        if log:
            stream.close()
        if errors != subprocess.STDOUT:
            errors.close()
    if code:
        stop_owned_group(process)
    if (code == 0) != success:
        if log:
            print(log.read_text(), flush=True)
            if log.suffix == ".json":
                print(log.with_suffix(".stderr").read_text(), flush=True)
        raise AssertionError(f"Unexpected exit {code}: {args}")
    return code


parser = argparse.ArgumentParser()
parser.add_argument("--state", required=True, type=Path)
parser.add_argument("--producer-source", required=True, type=Path)
parser.add_argument("--native-fixture", required=True, type=Path)
parser.add_argument("--resume-after-relock", action="store_true", help="Continue a retained cachedResolution=false run after its successful relocked replay")
args = parser.parse_args()
state = args.state.resolve()
state.mkdir(parents=True, exist_ok=args.resume_after_relock)
source = args.producer_source.resolve()
fixture = args.native_fixture.resolve()
logs = state / "logs"
logs.mkdir(exist_ok=args.resume_after_relock)
env = dict(os.environ, HOME=str(state / "home"), TMPDIR=str(state / "tmp"),
           XDG_CACHE_HOME=str(state / "xdg/cache"), XDG_CONFIG_HOME=str(state / "xdg/config"),
           XDG_STATE_HOME=str(state / "xdg/state"), XDG_DATA_HOME=str(state / "xdg/data"))
for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
    Path(env[key]).mkdir(parents=True, exist_ok=True)
expression = f'''let f = builtins.getFlake {json.dumps(str(source))};
 system = builtins.currentSystem;
 p = import f.inputs.nixpkgs {{ inherit system; overlays = [ f.overlays.default ]; }};
 sdk = p.sbt-purifier.buildSbtProject {{ src = {source}/tests/fixtures/multi; }};
 in {{ inherit system; sbtVersion = sdk.lock.sbtVersion; javaMajor = sdk.lock.javaVersion;
 inject = toString f.packages.${{system}}.sbt.inject;
 repository = toString f.packages.${{system}}.sbt.pluginRepository;
 cli = toString f.packages.${{system}}.sbt-purifier;
 jdk = sdk.jdk.home; nixpkgs = toString f.inputs.nixpkgs;
 cache = toString sdk.dependencies; launcher = sdk.lock.launcher; }}'''
tool_json = state / "tools.json"
command(["nix", "eval", "--impure", "--json", "--expr", expression], cwd=state, env=env, log=tool_json)
tools = json.loads(tool_json.read_text())
assert tools["sbtVersion"] == "1.13.1" and tools["javaMajor"] == "21", "The native fixture must retain the explicit sbt1.13.1/Java21 test point"
command(["nix", "build", f"path:{source}#sbt-purifier", "--out-link", state / "cli"],
        cwd=state, env=env, log=logs / "tools-build.log")
repo = state / "origin"
if not args.resume_after_relock:
    shutil.copytree(fixture / "fixture-repository", repo)
for path in [repo, *repo.rglob("*")]:
    path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o200))
sdk_root = Path(tools["cache"]) / "https/repo1.maven.org/maven2"
published = {"1", "2"}
requests = json.loads((state / "requests.json").read_text()) if args.resume_after_relock else []
manifests = json.loads((state / "candidate-manifests.json").read_text()) if args.resume_after_relock else []
phases = json.loads((state / "phases.json").read_text()) if args.resume_after_relock else []
if args.resume_after_relock:
    assert any(p["phase"] == "False-relocked-replay" and p["exitCode"] == 0 for p in phases)
    for phase in phases:
        if "logSha256" in phase:
            assert sha(logs / (phase["phase"] + ".log")) == phase["logSha256"]
        if "nativeLogSha256" in phase:
            assert sha(logs / (phase["phase"] + "-native-replay.log")) == phase["nativeLogSha256"]
    old_lock = json.loads((state / "consumer-False/sbt-purifier.lock.json").read_text())
    port = urlsplit(next(a["url"] for a in old_lock["artifacts"] if "/proof/leaf/" in a["url"])).port
else:
    port = 0


def manifest(label):
    files = {str(p.relative_to(repo)): sha(p) for p in sorted(repo.rglob("*"))
             if p.is_file() and not (p.relative_to(repo).parts[:2] == ("proof", "leaf")
                 and len(p.relative_to(repo).parts) > 3 and p.relative_to(repo).parts[2] not in published)}
    value = {"label": label, "files": files, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    manifests.append(value)
    return value["sha256"]


def publish(versions):
    published.clear()
    published.update(versions)
    metadata = repo / "proof/leaf/maven-metadata.xml"
    metadata.write_text('<metadata><groupId>proof</groupId><artifactId>leaf</artifactId><versioning><latest>' + versions[-1] + '</latest><release>' + versions[-1] + '</release><versions>' + ''.join('<version>' + v + '</version>' for v in versions) + '</versions><lastUpdated>20200101000000</lastUpdated></versioning></metadata>')
    return manifest("publication-" + "-".join(versions))


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def translate_path(self, path):
        if path.startswith("/maven2/"):
            return str(sdk_root / path.removeprefix("/maven2/"))
        return super().translate_path(path)

    def send_head(self):
        requests.append([self.command, self.path, sorted(published)])
        parts = self.path.split("/")
        if len(parts) > 3 and parts[1:3] == ["proof", "leaf"] and parts[3] in {"3", "99"} and parts[3] not in published:
            self.send_error(404)
            return None
        return super().send_head()


server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(repo)))
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"
env["SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR"] = origin + "/maven2/"
native_receipt = json.loads((fixture / "receipt.json").read_text())
expected = native_receipt["expectedJarSha256"]


def write_project(project, cached):
    (project / "project").mkdir(parents=True)
    (project / "project/build.properties").write_text("sbt.version=1.13.1\n")
    (project / "project/build.sbt").write_text("useCoursier := false\n")
    (project / "build.sbt").write_text('''ThisBuild / useCoursier := false
ThisBuild / scalaVersion := "2.12.21"
lazy val app = project.in(file(".")).settings(
  organization := "proof", name := "ivy-capture", version := "1",
  autoScalaLibrary := false, managedScalaInstance := false, crossPaths := false,
  externalResolvers := Seq("controlled" at ORIGIN),
  updateOptions := updateOptions.value.withCachedResolution(CACHED),
  libraryDependencies ++= Seq("proof" % "left" % "1", "proof" % "right" % "1", "proof" % "leaf" % "latest.integration"),
  Test / test := { (Compile / runner).value.run("proof.Consumer", (Compile / fullClasspath).value.files, Seq(), streams.value.log).get }
)
'''.replace("ORIGIN", json.dumps(origin)).replace("CACHED", str(cached).lower()))
    java = project / "src/main/java/proof/Consumer.java"
    java.parent.mkdir(parents=True)
    java.write_text('''package proof;
import java.nio.file.*;
import java.security.*;
public class Consumer {
 public static void main(String[] args) throws Exception {
  String value = Marker.value();
  String wanted = value.equals("leaf-2") ? "SHA2" : value.equals("leaf-3") ? "SHA3" : "INVALID";
  var origin = Marker.class.getProtectionDomain().getCodeSource().getLocation();
  byte[] bytes = Files.readAllBytes(Path.of(origin.toURI()));
  String actual = java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
  if (!actual.equals(wanted)) throw new AssertionError(value + " " + actual + " " + origin);
  System.out.println("IVY_CAPTURE_LOADED " + value + " " + actual + " " + origin);
 }
}
'''.replace("SHA2", expected["leaf:2"]).replace("SHA3", expected["leaf:3"]))


def native(project, commands, label, injected=True, expected_revision=None):
    home = state / (project.name + "-home")
    home.mkdir(exist_ok=True)
    boot_repositories = home / "repositories"
    boot_repositories.write_text("[repositories]\nlocked: file:" + str(sdk_root) + "\n")
    global_plugins = home / "global-plugins"
    global_plugins.mkdir(exist_ok=True)
    shutil.copyfile(tools["inject"], global_plugins / "sbt-nix-backend.sbt")
    native_env = dict(env, HOME=str(home), COURSIER_CACHE=str(home / "coursier"),
                      XDG_CACHE_HOME=str(home / "xdg/cache"), XDG_CONFIG_HOME=str(home / "xdg/config"),
                      XDG_STATE_HOME=str(home / "xdg/state"), XDG_DATA_HOME=str(home / "xdg/data"))
    java = [str(Path(tools["jdk"]) / "bin/java"), "-XX:ActiveProcessorCount=2", "-Xmx1g",
            "-Duser.home=" + str(home), "-Dsbt.boot.directory=" + str(home / "boot"),
            "-Dsbt.global.base=" + str(home / "global"), "-Dsbt.ivy.home=" + str(home / "ivy"),
            "-Dsbt.repository.config=" + str(boot_repositories), "-Dsbt.global.plugins=" + str(global_plugins),
            "-Dsbt.nix.backend.inject=" + str(global_plugins / "sbt-nix-backend.sbt"),
            "-Dsbt.nix.backend.repository=" + tools["repository"], "-Dsbt.nix.backend.nixpkgs=" + tools["nixpkgs"],
            "-Dsbt.server.autostart=false", "-Dsbt.log.noformat=true", "-jar", str(Path(tools["cache"]) / tools["launcher"])]
    if not injected:
        java = [value for value in java if not value.startswith(("-Dsbt.global.plugins=", "-Dsbt.nix.backend."))]
    log = logs / (label + ".log")
    before = manifest(label + "-before")
    code = command(java + commands, cwd=project, env=native_env, log=log)
    if expected_revision is not None:
        assert f"IVY_CAPTURE_LOADED leaf-{expected_revision} {expected['leaf:' + expected_revision]}" in log.read_text()
    lock = project / "sbt-purifier.lock.json"
    saved = state / "locks" / (label + ".json")
    saved.parent.mkdir(exist_ok=True)
    if lock.exists():
        saved.write_bytes(lock.read_bytes())
    value = {"phase": label, "cachedResolution": cached, "scope": "host native purifier task" if injected else "host plugin-free native fresh oracle", "commands": commands,
             "exitCode": code, "candidate": before, "logSha256": sha(log), "lockSha256": sha(lock) if lock.exists() else None}
    phases.append(value)
    (state / "partial-phases.json").write_text(json.dumps(phases, indent=2) + "\n")
    return lock.read_bytes() if lock.exists() else None


def replay(project, lock_bytes, label, revision, missing=False):
    snapshot = state / "snapshots" / label
    snapshot.mkdir(parents=True)
    for name in ("build.sbt", "project", "src"):
        item = project / name
        if item.is_dir():
            shutil.copytree(item, snapshot / name, ignore=shutil.ignore_patterns("target", "project"))
        else:
            shutil.copyfile(item, snapshot / name)
    lock = state / "locks" / (label + "-replay.json")
    if missing:
        data = json.loads(lock_bytes)
        removed = [entry for entry in data["artifacts"] if entry["url"].endswith(f"/proof/leaf/{revision}/leaf-{revision}.jar")]
        assert len(removed) == 1
        data["artifacts"] = [entry for entry in data["artifacts"] if entry not in removed]
        version = data.pop("version")
        data.pop("contentHash")
        rest = json.dumps(data, sort_keys=True, separators=(",", ":"))[1:]
        head = '{\n  "version": ' + str(version) + ',\n'
        checksum = hashlib.sha256((head + rest).encode()).hexdigest()
        lock.write_text(head + '  "contentHash": "' + checksum + '",\n' + rest)
    else:
        lock.write_bytes(lock_bytes)
    expr = f'''let b = import {Path(__file__).with_name('ivy-capture-replay.nix')} {{
 producerSource = {source}; projectSource = {snapshot}; lockFile = {lock}; }}; in b'''
    bindings = state / (label + "-bindings.json")
    command(["nix", "eval", "--impure", "--json", "--expr", expr + ' // { tests = b.tests.drvPath; dependencies = b.dependencies.outPath; app = { drv = b.app.drvPath; out = b.app.outPath; }; }'], cwd=state, env=env, log=bindings)
    bound = json.loads(bindings.read_text())
    if not missing:
        command([str(Path(tools["cli"]) / "bin/sbt-purifier"), "prefetch", "--lockfile", lock], cwd=project, env=env, log=logs / (label + "-prefetch.log"))
    build_log = logs / (label + "-build.log")
    code = command(["nix", "build", "--impure", "--expr", expr + '.tests', "--option", "sandbox", "true", "--out-link", state / (label + "-result")], cwd=state, env=env, log=build_log, success=not missing)
    native_log = logs / (label + "-native-replay.log")
    command(["nix", "log", bound["app"]["drv"]], cwd=state, env=env, log=native_log)
    text = native_log.read_text()
    if missing:
        assert "IVY_CAPTURE_LOADED" not in text and f"download failed: proof#leaf;{revision}!leaf.jar" in text
    else:
        assert f"IVY_CAPTURE_LOADED leaf-{revision} {expected['leaf:' + revision]}" in text
    info = state / (label + "-path-info.json")
    if not missing:
        command(["nix", "path-info", "--json", bound["app"]["out"], str(state / (label + "-result"))], cwd=state, env=env, log=info)
    phases.append({"phase": label, "cachedResolution": cached, "scope": "actual sandboxed compile/test/runtime replay", "exitCode": code, "expectedRevision": revision, "negativeMissingRecord": missing, "bindings": bound, "lockSha256": sha(lock), "nativeLogSha256": sha(native_log)})
    assert (project / "sbt-purifier.lock.json").read_bytes() == lock_bytes


try:
    for cached in (False, True):
        if args.resume_after_relock and not cached:
            publish(["1", "2", "3"])
            project = state / "consumer-False"
            relocked = (project / "sbt-purifier.lock.json").read_bytes()
        else:
            publish(["1", "2"])
            oracle = state / f"oracle-before-{cached}"
            write_project(oracle, cached)
            native(oracle, ["compile", "runMain proof.Consumer"], f"{cached}-fresh-before-oracle", injected=False, expected_revision="2")
            project = state / f"consumer-{cached}"
            write_project(project, cached)
            first = native(project, ["nixLock"], f"{cached}-initial-lock")
            replay(project, first, f"{cached}-initial-replay", "2")
            repeat = native(project, ["nixLock"], f"{cached}-repeat-lock")
            assert first == repeat
            publish(["1", "2", "3"])
            oracle = state / f"oracle-after-{cached}"
            write_project(oracle, cached)
            native(oracle, ["compile", "runMain proof.Consumer"], f"{cached}-fresh-after-oracle", injected=False, expected_revision="3")
            noop = native(project, ["nixLock"], f"{cached}-after-publication-noop")
            assert first == noop
            relocked = native(project, ["nixRelock"], f"{cached}-forced-relock")
            assert relocked != first
            replay(project, relocked, f"{cached}-relocked-replay", "3")
        negative_label = f"{cached}-missing-selected" + ("-retry" if args.resume_after_relock and not cached else "")
        replay(project, relocked, negative_label, "3", missing=True)
        replay(project, relocked, f"{cached}-restored-selected", "3")
        repeated = native(project, ["nixLock"], f"{cached}-after-relock-noop")
        assert repeated == relocked
        updated = native(project, ["nixLockUpdate"], f"{cached}-forced-update")
        assert updated == relocked
finally:
    server.shutdown()
    server.server_close()
    (state / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")
    (state / "candidate-manifests.json").write_text(json.dumps(manifests, indent=2) + "\n")
    (state / "phases.json").write_text(json.dumps(phases, indent=2) + "\n")
