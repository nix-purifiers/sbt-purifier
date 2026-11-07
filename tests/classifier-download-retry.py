#!/usr/bin/env python3
"""Exercise classifier retries through native lock capture using an owned Maven repository."""
import argparse
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading
import time
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wrapper', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--sbt-version', choices=['1.11.7', '1.13.1'], required=True)
    args = parser.parse_args()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    project = results / 'project'
    (project / 'project').mkdir(parents=True)
    (project / 'src/main/java').mkdir(parents=True)
    (project / 'project/build.properties').write_text('sbt.version=' + args.sbt_version + '\n')
    (project / 'src/main/java/Main.java').write_text('public class Main { public static void main(String[] args) { System.out.println("owned"); } }\n')
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, 'w') as archive:
        item = zipfile.ZipInfo('META-INF/MANIFEST.MF', (1980, 1, 1, 0, 0, 0))
        archive.writestr(item, 'Manifest-Version: 1.0\n\n')
    artifacts = {}
    for module in ['dependency', 'other']:
        base = '/owned/classifier/' + module + '/1.0.0/' + module + '-1.0.0'
        artifacts[base + '.pom'] = ('<project><modelVersion>4.0.0</modelVersion><groupId>owned.classifier</groupId>'
            '<artifactId>' + module + '</artifactId><version>1.0.0</version></project>').encode()
        for classifier in ['', '-sources', '-javadoc']:
            artifacts[base + classifier + '.jar'] = jar.getvalue()
    for path, body in list(artifacts.items()):
        for suffix, algorithm in [('.sha1', 'sha1'), ('.md5', 'md5')]:
            artifacts[path + suffix] = hashlib.new(algorithm, body).hexdigest().encode()
    target = '/owned/classifier/dependency/1.0.0/dependency-1.0.0-sources.jar'
    other = '/owned/classifier/other/1.0.0/other-1.0.0-sources.jar'
    state = {'scenario': 'baseline', 'requests': Counter(), 'targetTimes': []}
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, send_body):
            with state_lock:
                state['requests'][(self.command, self.path)] += 1
                count = state['requests'][(self.command, self.path)]
                scenario = state['scenario']
                if self.command == 'GET' and self.path == target:
                    state['targetTimes'].append(time.monotonic())
            body = artifacts.get(self.path)
            status = 200 if body is not None else 404
            body = body or b'not found'
            length = len(body)
            if self.command == 'GET' and self.path == target:
                if scenario == 'transient503' and count == 1:
                    status = 503
                elif scenario in ['exhausted503', 'mixed503401']:
                    status = 503
                elif scenario == 'checksum':
                    body += b'corrupt payload'
                    length = len(body)
                elif scenario == 'truncated':
                    body = body[:3]
                elif scenario == 'tls':
                    self.send_response(302)
                    self.send_header('Location', 'https://127.0.0.1:' + str(tls_server.server_port) + target)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            if self.command == 'GET' and self.path == other and scenario == 'mixed503401':
                status = 401
            self.send_response(status)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(length))
            self.send_header('Connection', 'close')
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            self.close_connection = True

    openssl = shutil.which('openssl')
    if not openssl:
        raise RuntimeError('openssl is required for the owned untrusted TLS control')
    with (results / 'certificate-generation.log').open('w') as log:
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=localhost', '-keyout', str(results / 'test-key.pem'),
            '-out', str(results / 'test-cert.pem')], stdout=log, stderr=subprocess.STDOUT, check=True)
    tls_server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(results / 'test-cert.pem', results / 'test-key.pem')
    tls_server.socket = context.wrap_socket(tls_server.socket, server_side=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in [server, tls_server]]
    for thread in threads:
        thread.start()
    counter = results / 'project-task-counts.txt'
    (project / 'build.sbt').write_text("""ThisBuild / organization := "owned.classifier"
ThisBuild / version := "1.0.0"
name := "owned-classifier-consumer"
scalaVersion := "2.12.21"
autoScalaLibrary := false
crossPaths := false
resolvers += MavenRepository("owned-classifier-fixture", "http://127.0.0.1:PORT/")
libraryDependencies ++= Seq("owned.classifier" % "dependency" % "1.0.0", "owned.classifier" % "other" % "1.0.0")
Compile / compile := {
  val result = (Compile / compile).value
  IO.append(file(sys.env("OWNED_CLASSIFIER_TASK_COUNTER")), "compile\\n")
  result
}
Compile / packageBin := {
  val result = (Compile / packageBin).value
  IO.append(file(sys.env("OWNED_CLASSIFIER_TASK_COUNTER")), "packageBin\\n")
  result
}
Test / test := {
  (Test / test).value
  IO.append(file(sys.env("OWNED_CLASSIFIER_TASK_COUNTER")), "test\\n")
}
publish := {
  (publish).value
  IO.append(file(sys.env("OWNED_CLASSIFIER_TASK_COUNTER")), "publish\\n")
}
updateClassifiers := {
  val report = updateClassifiers.value
  if (sys.env.get("OWNED_CLASSIFIER_FATAL").contains("true")) sys.error("owned fatal classifier override")
  report
}
""".replace('PORT', str(server.server_port)))
    env = dict(os.environ)
    for key in ['JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', 'JAVA_OPTS', 'SBT_OPTS', 'COURSIER_REPOSITORIES',
                'COURSIER_MODE', 'SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR', 'SBT_PURIFIER_LOCKFILE',
                'SBT_NIX_BACKEND_REPOSITORY', 'SBT_NIX_MODE', 'SBT_NIX_OUTPUT', 'OWNED_CLASSIFIER_FATAL']:
        env.pop(key, None)
    for key, name in [('HOME', 'home'), ('XDG_CACHE_HOME', 'cache'), ('COURSIER_CACHE', 'coursier'), ('XDG_RUNTIME_DIR', 'runtime')]:
        path = results / name
        path.mkdir(mode=0o700)
        env[key] = str(path)
    temporary = results / 'tmp'
    temporary.mkdir()
    env['TMPDIR'] = str(temporary)
    # Parent bootstrap may reuse SDK downloads. LockGeneration unsets this flag
    # and creates the actual capture's empty HOME/cache and SBT_NIX_MODE=lock.
    env['SBT_NIX_BACKEND'] = '0'
    env['OWNED_CLASSIFIER_TASK_COUNTER'] = str(counter)
    env['SBT_OPTS'] = ('-Xmx2g -Xss4m -XX:ActiveProcessorCount=2 -Dsbt.server.autostart=false '
        '-Dsbt.server.forcestart=true -Dsbt.supershell=false -Dsbt.log.noformat=true '
        '-Duser.home=' + env['HOME'] + ' -Djava.io.tmpdir=' + str(temporary) +
        ' -Dsbt.global.base=' + str(results / 'global') + ' -Dsbt.ivy.home=' + str(results / 'ivy') +
        ' -Dsbt.boot.directory=' + str(results / 'boot'))
    source = Path(__file__).resolve().parents[1]
    source_revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain']), 'capture requires a frozen clean source'
    receipt = {'sbtVersion': args.sbt_version, 'wrapper': str(args.wrapper.resolve()),
        'sourceRevision': source_revision,
        'wrapperScriptSha256': hashlib.sha256((args.wrapper / 'bin/sbt').read_bytes()).hexdigest(),
        'harnessSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'bootstrap': 'Backend disabled only in parent; actual capture resets mode and starts empty HOME/cache. No Maven Central mirror configured.',
        'repository': 'Owned Maven test artifacts; default native SDK repositories unchanged.',
        'cases': [], 'complete': False}
    receipt_path = results / 'receipt.json'

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')

    baseline = None
    baseline_counts = None
    lockfile = project / 'sbt-purifier.lock.json'
    try:
        for scenario in ['baseline', 'transient503', 'exhausted503', 'mixed503401', 'checksum', 'truncated', 'tls', 'fatal']:
            with state_lock:
                state['scenario'] = scenario
                state['requests'] = Counter()
                state['targetTimes'] = []
            counter.write_text('')
            case_env = dict(env)
            if scenario == 'fatal':
                case_env['OWNED_CLASSIFIER_FATAL'] = 'true'
            before = lockfile.read_bytes() if lockfile.exists() else None
            label = scenario + '.log'
            case = {'scenario': scenario, 'log': label, 'expectedSuccess': scenario in ['baseline', 'transient503']}
            receipt['cases'].append(case)
            save()
            with (results / label).open('w') as log:
                process = subprocess.run([str(args.wrapper / 'bin/sbt'), '--server', '-batch', 'nixRelock'],
                    cwd=project, env=case_env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
            text = (results / label).read_text()
            counts = Counter(counter.read_text().splitlines())
            with state_lock:
                requests = [{'method': method, 'path': path, 'count': count}
                    for (method, path), count in sorted(state['requests'].items())]
                times = list(state['targetTimes'])
            case.update(returncode=process.returncode, projectTaskCounts=dict(counts), requests=requests,
                classifierRetryWarnings=text.count('transient classifier download failed; retry'),
                freshCapture='in a fresh sbt with an empty cache' in text,
                targetGetOffsets=[round(value - times[0], 6) for value in times])
            save()
            assert (process.returncode == 0) == case['expectedSuccess'], scenario + ': unexpected native result; see ' + label
            assert case['freshCapture'], 'case did not reach a fresh native capture'
            if scenario == 'baseline':
                baseline = lockfile.read_bytes()
                baseline_counts = counts
                assert counts['compile'] > 0, 'baseline did not execute the project compiler'
                parsed = json.loads(baseline)
                assert parsed['sbtVersion'] == args.sbt_version
                receipt['actualSbtWelcomeLines'] = [line for line in text.splitlines() if 'welcome to sbt ' in line]
                urls = {artifact['url'] for artifact in parsed['artifacts']}
                expected = {'http://127.0.0.1:' + str(server.server_port) + path
                    for path in artifacts if path.endswith(('-sources.jar', '-javadoc.jar'))}
                assert expected <= urls, 'baseline omitted requested source or javadoc classifiers'
                receipt['ownedClassifiersCaptured'] = sorted(expected)
            elif scenario == 'transient503':
                assert lockfile.read_bytes() == baseline, 'successful transient capture changed baseline lock bytes'
                assert counts == baseline_counts, 'classifier retry reran a project task'
                assert case['classifierRetryWarnings'] >= 1
                assert next(row['count'] for row in requests if row['method'] == 'GET' and row['path'] == target) == 2
                assert times[1] - times[0] >= 0.9, 'native retry did not observe its first backoff'
            else:
                assert before == baseline and lockfile.read_bytes() == baseline, 'failed capture changed old lock bytes'
                assert not counts, 'failed editor downloads executed project compile/test/package/publish'
                assert 'wrote sbt-purifier.lock.json' not in text
                assert 'LockGeneration$LockFailed' in text, 'failure did not come from native capture'
                if scenario == 'exhausted503':
                    assert next(row['count'] for row in requests if row['method'] == 'GET' and row['path'] == target) == 3
                    assert case['classifierRetryWarnings'] == 2
                    assert times[1] - times[0] >= 0.9 and times[2] - times[1] >= 1.9
                else:
                    assert case['classifierRetryWarnings'] == 0, 'hard failure retried a classifier task'
                    markers = {
                        'mixed503401': [target, other, 'ArtifactError$Unauthorized', '503 for URL:'],
                        'checksum': [target, 'ArtifactError$WrongChecksum'],
                        'truncated': [target, 'ArtifactError$WrongChecksum'],
                        'tls': [target, 'javax.net.ssl.SSLHandshakeException'],
                        'fatal': ['owned fatal classifier override'],
                    }[scenario]
                    assert all(marker in text for marker in markers), 'negative failed for an unrelated reason'
                    case['expectedNativeFailureMarkers'] = markers
            case['oldLockPreserved'] = baseline is not None and lockfile.read_bytes() == baseline
            case['lockSha256'] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
            case['passed'] = True
            save()
            print(scenario + ': passed', flush=True)
        receipt['complete'] = True
        save()
    finally:
        for active in [server, tls_server]:
            active.shutdown()
            active.server_close()
        for thread in threads:
            thread.join(timeout=5)
        save()


if __name__ == '__main__':
    main()
