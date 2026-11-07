"""Native Ivy observations; no purifier plugin or manufactured resolver result."""
import functools
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET


def run(args, timeout=60, check=True, **kw):
    with subprocess.Popen(args, start_new_session=True, **kw) as process:
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            raise
    if check and code:
        raise subprocess.CalledProcessError(code, args)
    return subprocess.CompletedProcess(args, code)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


root = Path.cwd()
out = Path(sys.argv[1])
out.mkdir()
repo = root / "repository"
repo.mkdir()
expected = {}
expected_classes = {}
candidate_states = []


def publish(name, version, dependencies=()):
    directory = repo / "proof" / name / version
    directory.mkdir(parents=True)
    classes = root / f"classes-{name}-{version}"
    classes.mkdir()
    class_name = "Marker" if name == "leaf" else name.capitalize()
    java = root / (class_name + ".java")
    java.write_text('package proof; public class ' + class_name + ' { public static String value() { return "'
                    + name + '-' + version + '"; }}')
    run(["javac", "-d", str(classes), str(java)])
    jar = directory / f"{name}-{version}.jar"
    run(["jar", "--create", "--file", str(jar), "--date=2020-01-01T00:00:00Z", "-C", str(classes), "."])
    deps = "".join(f"<dependency><groupId>proof</groupId><artifactId>{n}</artifactId><version>{v}</version></dependency>" for n, v in dependencies)
    (directory / f"{name}-{version}.pom").write_text(f'<project><modelVersion>4.0.0</modelVersion><groupId>proof</groupId><artifactId>{name}</artifactId><version>{version}</version><dependencies>{deps}</dependencies></project>')
    expected[f"{name}:{version}"] = digest(jar)
    if name == "leaf":
        with zipfile.ZipFile(jar) as archive:
            expected_classes[version] = hashlib.sha256(archive.read("proof/Marker.class")).hexdigest()


for revision in ("1", "2", "3", "99"):
    publish("leaf", revision)
publish("left", "1", [("leaf", "1")])
publish("right", "1", [("leaf", "2")])


def metadata(versions):
    target = repo / "proof/leaf/maven-metadata.xml"
    target.write_text('<metadata><groupId>proof</groupId><artifactId>leaf</artifactId><versioning><latest>' + versions[-1] + '</latest><release>' + versions[-1] + '</release><versions>' + ''.join('<version>' + v + '</version>' for v in versions) + '</versions><lastUpdated>20200101000000</lastUpdated></versioning></metadata>')
    snapshot()


def snapshot():
    target = repo / "proof/leaf/maven-metadata.xml"
    visible = {}
    for path in sorted(repo.rglob("*")):
        if path.is_file():
            relative = path.relative_to(repo)
            if relative.parts[:2] == ("proof", "leaf") and len(relative.parts) > 3 and relative.parts[2] not in published:
                continue
            visible[str(relative)] = digest(path)
    result = {"published": sorted(published), "metadata": target.read_text(), "files": visible, "manifestSha256": hashlib.sha256(json.dumps(visible, sort_keys=True).encode()).hexdigest()}
    candidate_states.append(result)
    return result["manifestSha256"]


published = {"1", "2"}
requests = []


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_head(self):
        requests.append([self.command, self.path, sorted(published)])
        parts = self.path.split("/")
        if len(parts) > 3 and parts[1:3] == ["proof", "leaf"] and parts[3] in {"3", "99"} and parts[3] not in published:
            self.send_error(404)
            return None
        return super().send_head()

    def do_GET(self):
        if self.path == "/advance":
            published.add("3")
            metadata(["1", "2", "3"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"advanced")
        else:
            super().do_GET()


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(repo)))
threading.Thread(target=server.serve_forever, daemon=True).start()
origin = f"http://127.0.0.1:{server.server_port}"

task = r'''
import java.nio.file.{Files, StandardOpenOption}
import java.security.MessageDigest
def sha(f: File) = MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(f.toPath)).map(b => f"${b & 255}%02x").mkString
val measure = taskKey[Unit]("Observe native report and actual loaded class")
val advance = taskKey[Unit]("Publish next controlled candidate")
advance := { val s = scala.io.Source.fromURL("ORIGIN/advance"); try s.mkString finally s.close() }
measure := {
  val report = update.value
  val lines = scala.collection.mutable.ArrayBuffer[String]()
  val backend = ivySbt.value.getClass
  lines += Seq("backend", backend.getName, backend.getProtectionDomain.getCodeSource.getLocation.toString, sha(new File(backend.getProtectionDomain.getCodeSource.getLocation.toURI))).mkString("\t")
  val ivy = classOf[org.apache.ivy.Ivy]
  lines += Seq("ivy", ivy.getName, ivy.getProtectionDomain.getCodeSource.getLocation.toString, sha(new File(ivy.getProtectionDomain.getCodeSource.getLocation.toURI)), org.apache.ivy.Ivy.getIvyVersion).mkString("\t")
  lines += Seq("sdk", sbtVersion.value, System.getProperty("java.runtime.version"), useCoursier.value.toString, updateOptions.value.cachedResolution.toString).mkString("\t")
  report.configurations.foreach { c => (c.modules ++ c.details.flatMap(_.modules)).distinct.foreach { m =>
    lines += Seq("module", c.configuration.toString, m.module.organization, m.module.name, m.module.revision, m.evicted.toString, m.callers.map(x => x.caller.toString).mkString("|")).mkString("\t")
    m.artifacts.foreach { case (a, f) => lines += Seq("artifact", m.module.name, m.module.revision, f.getCanonicalPath, sha(f)).mkString("\t") }
  }}
  ((file(sys.props("sbt.ivy.home")) / "cache") * "*.xml").get.sorted.foreach { f =>
    val bytes = Files.readAllBytes(f.toPath)
    if (new String(bytes, "UTF-8").contains("<ivy-report")) lines += Seq("ivy-report", f.getName, sha(f), java.util.Base64.getEncoder.encodeToString(bytes)).mkString("\t")
  }
  val cp = (Runtime / managedClasspath).value.map(_.data)
  val loader = new java.net.URLClassLoader(cp.map(_.toURI.toURL).toArray, null)
  try {
    val c = loader.loadClass("proof.Marker")
    val location = c.getProtectionDomain.getCodeSource.getLocation
    val bytes = c.getResourceAsStream("/proof/Marker.class")
    val buffer = new java.io.ByteArrayOutputStream()
    try { val b = new Array[Byte](4096); var n = bytes.read(b); while(n != -1) { buffer.write(b,0,n); n = bytes.read(b) } } finally bytes.close()
    val classSha = MessageDigest.getInstance("SHA-256").digest(buffer.toByteArray).map(b => f"${b & 255}%02x").mkString
    lines += Seq("loaded", c.getMethod("value").invoke(null).toString, location.toString, sha(new File(location.toURI)), classSha).mkString("\t")
  } finally loader.close()
  Files.write(file("evidence.tsv").toPath, (lines.mkString("\n") + "\nEND\n").getBytes("UTF-8"), StandardOpenOption.CREATE, StandardOpenOption.APPEND)
}
'''.replace("ORIGIN", origin)

results = []
try:
    for cached in (False, True):
        published.clear()
        published.update(["1", "2"])
        metadata(["1", "2"])
        build = root / f"consumer-{cached}"
        (build / "project").mkdir(parents=True)
        (build / "project/build.properties").write_text("sbt.version=1.13.1\n")
        (build / "project/build.sbt").write_text("useCoursier := false\n")
        # Full report is an explicit separate native operation: it may resolve
        # again, so its class loader must consume that report's artifacts.
        full_task = task.replace('val measure =', 'val measureFull =').replace('measure :=', 'measureFull :=').replace('val advance = taskKey[Unit]("Publish next controlled candidate")', '').replace('advance := { val s = scala.io.Source.fromURL("' + origin + '/advance"); try s.mkString finally s.close() }', '').replace('val report = update.value', 'val report = updateFull.value').replace('val cp = (Runtime / managedClasspath).value.map(_.data)', 'val cp = report.configurations.filter(_.configuration.toString == "runtime").flatMap(_.modules).flatMap(_.artifacts.map(_._2)).distinct')
        # Share imports/helpers once; the separate task starts at its key.
        full_task = full_task[full_task.index('val measureFull ='):]
        (build / "build.sbt").write_text('useCoursier := false\nupdateOptions := updateOptions.value.withCachedResolution(' + str(cached).lower() + ')\nautoScalaLibrary := false\nmanagedScalaInstance := false\ncrossPaths := false\nexternalResolvers := Seq("controlled" at "' + origin + '")\nlibraryDependencies ++= Seq("proof" % "left" % "1", "proof" % "right" % "1", "proof" % "leaf" % "latest.integration")\n' + task + full_task)
        def invoke(state, commands, label, expected_success=True):
            print(f"Ivy phase cachedResolution={cached}: {label}", flush=True)
            before_manifest = snapshot()
            if label in {"fresh", "full-report", "competitor", "missing", "restored"}:
                shutil.rmtree(build / "target", ignore_errors=True)
                shutil.rmtree(build / "project/target", ignore_errors=True)
            state.mkdir(exist_ok=True)
            cache = state / "downloads"
            if not cache.exists():
                shutil.copytree(os.environ["SDK_CACHE"], cache, symlinks=True)
            # Existing complete immutable SDK closure supplies bootstrap only.
            repository_config = state / "repositories"
            repository_config.write_text('[repositories]\nlocked: file:' + str(cache / 'https/repo1.maven.org/maven2') + '\n')
            args = ["java", "-XX:ActiveProcessorCount=2", "-Xmx1g", "-Duser.home=" + str(state), "-Dsbt.global.base=" + str(state / "global"), "-Dsbt.boot.directory=" + str(state / "boot"), "-Dsbt.ivy.home=" + str(state / "ivy"), "-Dsbt.repository.config=" + str(repository_config), "-Dsbt.server.autostart=false", "-Dsbt.log.noformat=true", "-jar", str(cache / os.environ["SDK_LAUNCHER"]), *commands]
            log = out / f"{cached}-{label}.log"
            evidence = build / "evidence.tsv"
            previous = evidence.stat().st_size if evidence.exists() else 0
            with log.open("w") as stream:
                env = dict(os.environ, HOME=str(state), COURSIER_CACHE=str(cache), XDG_CACHE_HOME=str(state / "xdg/cache"), XDG_CONFIG_HOME=str(state / "xdg/config"), XDG_DATA_HOME=str(state / "xdg/data"), XDG_STATE_HOME=str(state / "xdg/state"))
                process = run(args, check=False, cwd=build, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
            if (process.returncode == 0) != expected_success:
                print(log.read_text(), file=sys.stderr)
                raise AssertionError(f"{label}: unexpected native exit {process.returncode}")
            rows = evidence.read_bytes()[previous:].decode() if evidence.exists() else ""
            (out / f"{cached}-{label}.tsv").write_text(rows)
            observations = []
            for block in rows.split("END\n"):
                if not block.strip():
                    continue
                fields = [line.split("\t") for line in block.splitlines()]
                loaded = next(x for x in fields if x[0] == "loaded")
                revision = loaded[1].removeprefix("leaf-")
                assert loaded[3] == expected[f"leaf:{revision}"]
                assert loaded[4] == expected_classes[revision]
                assert f"/proof/leaf/jars/leaf-{revision}.jar" in loaded[2]
                assert any(x[0] == "ivy" and x[1] == "org.apache.ivy.Ivy" for x in fields)
                assert any(x[0] == "sdk" and x[1] == "1.13.1" and x[3:] == ["false", str(cached).lower()] for x in fields)
                modules = [x for x in fields if x[0] == "module"]
                assert any(x[2:5] == ["proof", "leaf", revision] and x[5] == "false" for x in modules), json.dumps(fields)
                native_reports = []
                for row in fields:
                    if row[0] == "ivy-report":
                        raw = base64.b64decode(row[3])
                        assert hashlib.sha256(raw).hexdigest() == row[2]
                        xml = ET.fromstring(raw)
                        nodes = []
                        for module in xml.findall("./dependencies/module"):
                            for rev in module.findall("revision"):
                                nodes.append({"organisation": module.get("organisation"), "name": module.get("name"), "revision": rev.attrib, "callers": [c.attrib for c in rev.findall("caller")]})
                        native_reports.append({"name": row[1], "sha256": row[2], "nodes": nodes})
                observations.append({"revision": revision, "loadedJarSha256": loaded[3], "loadedClassSha256": loaded[4], "moduleRows": modules, "nativeReports": native_reports, "backend": [x for x in fields if x[0] in {"backend", "ivy", "sdk"}]})
            if expected_success:
                assert observations, "Native task must emit observations"
            else:
                assert not observations, "Missing artifact must not reach loaded marker"
                negative_log = log.read_text()
                assert "proof#leaf;3!leaf.jar" in negative_log and "download failed" in negative_log.lower(), negative_log
            result = {"cachedResolution": cached, "phase": label, "state": state.name, "candidateBefore": before_manifest, "candidateAfter": snapshot(), "exitCode": process.returncode, "logSha256": digest(log), "observations": observations}
            results.append(result)
            return result
        warm = invoke(root / f"state-{cached}", ["measure", "advance", "measure"], "same-jvm")
        assert warm["observations"][0]["revision"] == "2"
        invoke(root / f"state-{cached}", ["measure"], "new-jvm-same-cache")
        shutil.rmtree(build / "target", ignore_errors=True)
        shutil.rmtree(build / "project/target", ignore_errors=True)
        fresh = invoke(root / f"fresh-{cached}", ["measure"], "fresh")
        assert fresh["observations"][0]["revision"] == "3"
        full = invoke(root / f"full-{cached}", ["measureFull"], "full-report")
        assert full["observations"][0]["revision"] == "3"
        assert {row[4] for row in full["observations"][0]["moduleRows"] if row[5] == "true" and row[6]} >= {"1", "2"}, json.dumps(full)
        published.add("99")
        competitor = invoke(root / f"competitor-{cached}", ['set libraryDependencies := Seq("proof" % "leaf" % "99")', "measure"], "competitor")
        assert competitor["observations"][0]["revision"] == "99"
        missing = repo / "proof/leaf/3/leaf-3.jar"
        saved = missing.read_bytes()
        missing.unlink()
        invoke(root / f"missing-{cached}", ["measure"], "missing", expected_success=False)
        missing.write_bytes(saved)
        restored = invoke(root / f"restored-{cached}", ["measure"], "restored")
        assert restored["observations"][0]["revision"] == "3"

finally:
    server.shutdown()
    server.server_close()
    shutil.copytree(repo, out / "fixture-repository")
    (out / "candidate-states.json").write_text(json.dumps(candidate_states, indent=2) + "\n")
    (out / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")
    (out / "receipt.json").write_text(json.dumps({"scope": "native Ivy controlled observations; ordinary update cache phases and independent fresh updateFull lineage probe are distinct operations", "sdkCache": os.environ["SDK_CACHE"], "launcher": os.environ["SDK_LAUNCHER"], "launcherSha256": digest(Path(os.environ["SDK_CACHE"]) / os.environ["SDK_LAUNCHER"]), "driverSha256": digest(Path(__file__)), "expectedJarSha256": expected, "expectedClassSha256": expected_classes, "observations": results}, indent=2) + "\n")
    if sys.exc_info()[0] is not None:
        print(json.dumps({"status": "failed", "completedPhases": results, "candidateStates": candidate_states}), flush=True)
        logs = sorted(out.glob("*.log"), key=lambda p: p.stat().st_mtime_ns)
        if logs:
            print(logs[-1].read_text(), flush=True)
