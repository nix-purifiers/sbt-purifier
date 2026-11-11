#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a pinned independent project through native and purified sbt.

Keep commands, logs, lockfiles, source identities and partial receipts even
on failure. This is runtime evidence, rather than a version declaration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

SAMPLE_URL = 'https://github.com/sbt/test-interface.git'
SAMPLE_REV = '22b24334e8ea0274fc9a8fa7df8dd5ecd4f45cd5'



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', required=True, type=Path)
    parser.add_argument('--sbt-version', default='1.13.1')
    parser.add_argument('--fixture', choices=['independent', 'sbt2'], default='independent')
    args = parser.parse_args()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    # Only ephemeral Darwin CI uses /tmp. Local runs keep their temporary
    # state beneath the requested evidence directory.
    short_runtime = platform.system() == 'Darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
    with tempfile.TemporaryDirectory(prefix='sbt-run-', dir='/tmp' if short_runtime else results) as directory:
        run_sample(args, Path(directory).resolve(), short_runtime)


def runtime_directory(root, name, maximum_bytes=None):
    directory = root / name
    directory.mkdir(mode=0o700)
    assert directory.stat().st_mode & 0o077 == 0, 'runtime directory must be private'
    if maximum_bytes is not None:
        assert len(os.fsencode(directory)) <= maximum_bytes, 'runtime directory exceeds path budget'
    return directory


def run_sample(args, runtime_root, short_runtime):
    root = Path(__file__).resolve().parents[1]
    results = args.results.resolve()
    work = results / 'work'
    work.mkdir()
    receipt = {'sample': {'url': SAMPLE_URL, 'revision': SAMPLE_REV},
               'sbtVersion': args.sbt_version, 'fixture': args.fixture,
               'platform': platform.platform(), 'machine': platform.machine(),
               'harnessSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'runtimeRoot': str(runtime_root),
               'runtimeDirectoryMaximumBytes': 48 if short_runtime else None,
               'commands': [], 'complete': False}
    receipt_file = results / 'receipt.json'

    def save():
        receipt_file.write_text(json.dumps(receipt, indent=2) + '\n')

    def run(label, command, cwd=root, env=None, succeeds=True, machine=False):
        if command[:2] == ['nix', 'build']:
            command = [*command, '--option', 'max-jobs', '2', '--option', 'cores', '2']
        entry = {'label': label, 'argv': [str(x) for x in command],
                 'cwd': str(cwd), 'log': label + '.log', 'stderr': label + '.stderr.log'}
        if env is not None:
            entry['environment'] = {key: env[key] for key in (
                'HOME', 'XDG_CACHE_HOME', 'COURSIER_CACHE', 'XDG_RUNTIME_DIR',
                'JAVA_HOME', 'SBT_OPTS', 'JAVA_TOOL_OPTIONS',
                'SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR') if key in env}
        receipt['commands'].append(entry)
        save()
        with (results / entry['log']).open('w') as log, (results / entry['stderr']).open('w') as errors:
            process = subprocess.run(entry['argv'], cwd=cwd, env=env,
                                     stdout=log, stderr=errors, timeout=3600)
        entry['returncode'] = process.returncode
        save()
        output = (results / entry['log']).read_text()
        error_output = (results / entry['stderr']).read_text()
        print(label + ': ' + str(process.returncode), flush=True)
        if (process.returncode == 0) != succeeds:
            raise AssertionError(label + '\n' + output[-10000:] + error_output[-10000:])
        return (output if machine else output + error_output).strip()

    receipt['sourceRevision'] = run('source-revision', ['git', 'rev-parse', 'HEAD'])
    receipt['sourceDiff'] = run('source-diff', ['git', 'diff', '--binary', 'HEAD'])
    assert not receipt['sourceDiff'], 'runtime evidence requires a committed producer source'
    receipt['sourceReference'] = 'git+' + root.as_uri() + '?rev=' + receipt['sourceRevision']
    archived = json.loads(run('source-archive', ['nix', 'flake', 'archive', '--json',
        '--no-update-lock-file', receipt['sourceReference']], machine=True))
    snapshot = archived['path']
    assert Path(snapshot).is_dir() and str(Path(snapshot).resolve()) == snapshot, snapshot
    def check_archived_inputs(node):
        assert Path(node['path']).is_dir(), node['path']
        for value in node.get('inputs', {}).values():
            check_archived_inputs(value)
    check_archived_inputs(archived)
    receipt['sourceSnapshot'] = snapshot
    receipt['sourceNarHash'] = run('source-nar-hash', ['nix', 'hash', 'path', snapshot], machine=True)
    system = run('system', ['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem'], machine=True)
    receipt['system'] = system
    assert system in ('x86_64-linux', 'aarch64-linux', 'aarch64-darwin'), system
    receipt['sandbox'] = run('sandbox-setting', ['nix', 'config', 'show', 'sandbox'], machine=True)
    save()
    assert receipt['sandbox'] == 'true', 'Nix sandbox must be enabled'
    tools_expr = '''let
      f = builtins.getFlake SOURCE_REFERENCE;
      p = f.inputs.nixpkgs.legacyPackages.SYSTEM;
    in p.runCommand "sbt-native-control-tools" {} ''
      mkdir -p $out/bin
      ln -s ${p.sbt}/bin/sbt $out/bin/sbt
      ln -s ${p.jdk.home} $out/jdk
    '' '''.replace('SOURCE_REFERENCE', json.dumps(receipt['sourceReference'])).replace('SYSTEM', system)
    tools = Path(run('native-tools', ['nix', 'build', '--impure', '--expr', tools_expr,
                '--out-link', work / 'native-tools', '--print-out-paths'], machine=True).splitlines()[-1])
    run('java-version', [tools / 'jdk/bin/java', '-version'])
    wrapper = Path(run('wrapper', ['nix', 'build', receipt['sourceReference'] + '#sbt',
                '--out-link', work / 'wrapper', '--print-out-paths'], machine=True).splitlines()[-1])
    purifier = Path(run('purifier', ['nix', 'build', receipt['sourceReference'] + '#sbt-purifier',
                 '--out-link', work / 'purifier', '--print-out-paths'], machine=True).splitlines()[-1])
    project = work / 'project'
    if args.fixture == 'independent':
        run('sample-clone', ['git', 'clone', '--no-checkout', SAMPLE_URL, project])
        run('sample-checkout', ['git', 'checkout', '--detach', SAMPLE_REV], cwd=project)
        assert run('sample-revision', ['git', 'rev-parse', 'HEAD'], cwd=project) == SAMPLE_REV
    else:
        shutil.copytree(root / 'tests/fixtures/sbt2', project)
        (project / 'sbt-purifier.lock.json').unlink()
        receipt['sample'] = {'fixture': 'tests/fixtures/sbt2', 'sourceSnapshot': snapshot}
    properties = project / 'project/build.properties'
    receipt['originalBuildProperties'] = properties.read_text()
    properties.write_text('sbt.version=' + args.sbt_version + '\n')
    receipt['adaptation'] = (
        'none' if properties.read_text() == receipt['originalBuildProperties'] else
        'Only project/build.properties: sbt.version=' + args.sbt_version)
    if args.fixture == 'independent':
        run('sample-adaptation', ['git', 'diff', '--', 'project/build.properties'], cwd=project)

    def environment(name, offline=False):
        state = work / name
        state.mkdir()
        env = dict(os.environ)
        for key in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', 'SBT_OPTS', 'JAVA_OPTS',
                    'COURSIER_REPOSITORIES', 'COURSIER_MODE', 'SBT_NIX_BACKEND',
                    'SBT_NIX_BACKEND_REPOSITORY', 'SBT_PURIFIER_LOCKFILE'):
            env.pop(key, None)
        for key, suffix in [('HOME', 'home'), ('XDG_CACHE_HOME', 'cache'),
                            ('COURSIER_CACHE', 'coursier')]:
            env[key] = str(state / suffix)
            Path(env[key]).mkdir()
        # Reserve space for sbt's socket suffix within the macOS path limit.
        env['XDG_RUNTIME_DIR'] = str(runtime_directory(runtime_root, name, 48 if short_runtime else None))
        env['JAVA_HOME'] = str(tools / 'jdk')
        (state / 'tmp').mkdir()
        crash_inputs = results / 'jvm-crash-inputs'
        crash_inputs.mkdir(exist_ok=True)
        env['SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR'] = 'https://repo.maven.apache.org/maven2/'
        env['SBT_OPTS'] = ('-Xmx2g -Xss4m -XX:ActiveProcessorCount=2 '
            '-XX:ErrorFile=' + str(crash_inputs / 'hs_err_pid%p.log') + ' '
            '-Duser.home=' + env['HOME'] + ' '
            '-Djava.io.tmpdir=' + str(state / 'tmp') + ' '
            '-Dsbt.server.autostart=false -Dsbt.server.forcestart=true '
            '-Dsbt.supershell=false -Dsbt.log.noformat=true '
            '-Dsbt.global.base=' + str(state / 'global') + ' -Dsbt.ivy.home=' + str(state / 'ivy')
            + ' -Dsbt.boot.directory=' + str(state / 'boot'))
        if offline:
            env['JAVA_TOOL_OPTIONS'] = ('-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort=9 '
                                       '-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=9 '
                                       '-Dhttp.nonProxyHosts= -Dhttps.nonProxyHosts=')
        return env

    native = environment('native')
    native_log = run('native-test', [tools / 'bin/sbt', '--server', '-batch', 'clean', 'test'], cwd=project, env=native)
    assert re.search(r'All tests passed\.|Passed: Total [1-9]', native_log), 'native tests must actually execute'
    # Remove every sbt build product before changing providers or caches.
    def clean():
        for directory in sorted(project.rglob('target'), reverse=True):
            if directory.is_dir():
                shutil.rmtree(directory)
        shutil.rmtree(project / '.sbt-purifier', ignore_errors=True)

    clean()
    locking = environment('locking')
    run('lock', [purifier / 'bin/sbt-purifier', 'lock', '--server'], cwd=project, env=locking)
    lock = project / 'sbt-purifier.lock.json'
    original = lock.read_bytes()
    parsed = json.loads(original)
    assert parsed['sbtVersion'] == args.sbt_version
    assert parsed['artifacts'], 'sample must have actual locked downloads'
    receipt['javaMajor'] = parsed['javaVersion']
    receipt['lockSha256'] = hashlib.sha256(original).hexdigest()
    shutil.copyfile(lock, results / lock.name)
    run('lock-noop', [purifier / 'bin/sbt-purifier', 'lock', '--server'], cwd=project, env=locking)
    assert lock.read_bytes() == original, 'unchanged lock must be byte identical'
    run('relock', [wrapper / 'bin/sbt', '--server', '-batch', 'nixRelock'], cwd=project, env=locking)
    assert lock.read_bytes() == original, 'fresh relock must be byte identical'
    run('prefetch', [purifier / 'bin/sbt-purifier', 'prefetch'], cwd=project, env=locking)
    assert list(project.glob('sbt-purifier.lock.json.gcroot*')), 'prefetch must retain GC roots'
    clean()
    offline = environment('offline', offline=True)
    # A native empty-cache run must fail under these same network controls.
    negative = run('offline-negative-control', [tools / 'bin/sbt', '--server', '-batch', 'test'], cwd=project,
                   env=offline, succeeds=False)
    assert re.search(r'CantDownloadModule|could not retrieve sbt', negative), negative
    assert re.search(r'Connection refused|ConnectException', negative), negative
    clean()
    offline_log = run('offline-test', [wrapper / 'bin/sbt', '--server', '-batch', 'clean', 'test'],
                      cwd=project, env=offline)
    assert re.search(r'All tests passed\.|Passed: Total [1-9]', offline_log), 'offline tests must actually execute'
    downloaded = [str(p) for p in Path(offline['COURSIER_CACHE']).rglob('*')
                  if p.is_file() and not p.is_symlink() and not p.name.startswith('.')]
    assert not downloaded, downloaded
    assert lock.read_bytes() == original
    clean()
    # A fresh flake evaluates purely; the project derivations run sandboxed.
    build = work / 'build'
    build.mkdir()
    shutil.copytree(project, build / 'project', ignore=shutil.ignore_patterns('.git', '*.gcroot*'))
    socket_bindings = ''
    pure_tests = 'b.tests'
    if system == 'aarch64-darwin' and args.fixture == 'independent' and parsed['javaVersion'] == '21':
        probe = root / 'tests/GradleSocketProbe.java'
        shutil.copyfile(probe, build / probe.name)
        receipt['pureSocketProbe'] = {
            'sha256': hashlib.sha256(probe.read_bytes()).hexdigest(),
            'scope': 'Darwin independent fixture, selected JDK 21, before original checked package build',
            'requiredPairs': ['default-to-default-v4', 'default-to-INET-v4', 'INET-to-INET-v4'],
            'requiredExternalDenials': ['default', 'INET'],
            'unsupportedIPv6IsNotCountedAsDenial': True,
        }
        save()
        socket_bindings = """
        socketChecked = p.lib.mapAttrs (_: package: package.overrideAttrs (old: {
          preBuild = (old.preBuild or "") + ''
            cp ${./GradleSocketProbe.java} "$TMPDIR/GradleSocketProbe.java"
            socket_probe_status=0
            echo SBT_JDK21_SOCKET_PROBE_BEGIN >&2
            ${b.jdk}/bin/java ${p.lib.escapeShellArgs b.lock.javaOptions} \
              "$TMPDIR/GradleSocketProbe.java" "$TMPDIR/GradleSocketProbe.java" \
              > "$TMPDIR/sbt-java-socket-probe.log" 2>&1 || socket_probe_status=$?
            cat "$TMPDIR/sbt-java-socket-probe.log" >&2
            echo SBT_JDK21_SOCKET_PROBE_END >&2
            test "$socket_probe_status" -eq 0 || test "$socket_probe_status" -eq 2
            grep -q '^java.net.preferIPv4Stack: true$' "$TMPDIR/sbt-java-socket-probe.log"
            for pair in default-to-default-v4 default-to-INET-v4 INET-to-INET-v4; do
              grep -q "^$pair pair PASS crossProcess=true$" "$TMPDIR/sbt-java-socket-probe.log"
            done
            test "$(grep -E -c '^external (default|INET) DENIED kind=permission ' "$TMPDIR/sbt-java-socket-probe.log")" -eq 2
            if [ "$socket_probe_status" -eq 2 ]; then
              grep -q '^external INET6 UNPROVEN class=java.lang.UnsupportedOperationException ' "$TMPDIR/sbt-java-socket-probe.log"
            fi
            ! grep -q 'UNEXPECTED_SUCCESS' "$TMPDIR/sbt-java-socket-probe.log"
          '';
        })) b.packages;
        """
        pure_tests = """p.runCommand "sbt-tests" {} ''
          mkdir $out
          ${p.lib.concatStrings (p.lib.mapAttrsToList (id: package: "ln -s ${package} $out/${id}\n") socketChecked)}
        ''"""
    (build / 'flake.nix').write_text('''{
      inputs.purifier.url = SOURCE_REFERENCE;
      inputs.nixpkgs.follows = "purifier/nixpkgs";
      outputs = { self, purifier, nixpkgs }: let
        system = "SYSTEM";
        p = import nixpkgs { inherit system; overlays = [ purifier.overlays.default ]; };
        b = p.sbt-purifier.buildSbtProject { src = ./project; doCheck = true; };
        SOCKET_BINDINGS
      in { packages.${system} = PACKAGE_SET // { default = PURE_TESTS; }; };
    }'''.replace('SOURCE_REFERENCE', json.dumps(receipt['sourceReference'])).replace('SYSTEM', system)
       .replace('SOCKET_BINDINGS', socket_bindings).replace('PURE_TESTS', pure_tests)
       .replace('PACKAGE_SET', 'socketChecked' if socket_bindings else '{}'))
    shutil.copyfile(build / 'flake.nix', results / 'sample-build.nix')
    build_ref = 'path:' + str(build)
    run('pure-lock', ['nix', 'flake', 'lock', build_ref])
    shutil.copyfile(build / 'flake.lock', results / 'sample-build.lock.json')
    receipt['derivation'] = run('pure-derivation', ['nix', 'eval', '--raw', build_ref
        + '#packages.' + system + '.default.drvPath'], machine=True)
    run('pure-build', ['nix', 'build', build_ref, '--no-allow-import-from-derivation',
                     '--out-link', work / 'pure-result', '--print-build-logs'])
    if socket_bindings:
        names = json.loads(run('pure-checked-package-names', ['nix', 'eval', '--json',
            build_ref + '#packages.' + system, '--apply', 'builtins.attrNames'], machine=True))
        checked = [name for name in names if name != 'default']
        assert checked, 'the rebuild must target real checked packages'
        run('pure-rebuild', ['nix', 'build', *[build_ref + '#' + name for name in checked],
            '--no-allow-import-from-derivation', '--no-update-lock-file', '--rebuild',
            '--no-link', '--print-build-logs'])
    receipt['outputs'] = json.loads(run('pure-output-identities', ['nix', 'path-info',
        '--json', str(work / 'pure-result')], machine=True))
    run('pure-derivation-json', ['nix', 'derivation', 'show', receipt['derivation']])
    if args.fixture == 'independent' and args.sbt_version == '1.13.1':
        check = receipt['sourceReference'] + '#checks.' + system + '.independent-sample-offline-tasks'
        receipt['offlineTaskCheck'] = check
        receipt['offlineTaskDerivation'] = run('offline-task-derivation',
            ['nix', 'eval', '--raw', check + '.drvPath'], machine=True)
        run('offline-task-build', ['nix', 'build', check, '--print-build-logs',
            '--out-link', work / 'offline-task-result'])
        receipt['offlineTaskOutputs'] = json.loads(run('offline-task-output-identities',
            ['nix', 'path-info', '--json', str(work / 'offline-task-result')], machine=True))
        run('offline-task-derivation-json', ['nix', 'derivation', 'show', receipt['offlineTaskDerivation']])
    shutil.copyfile(build / 'flake.nix', results / 'sample-build.nix')
    shutil.copyfile(build / 'flake.lock', results / 'sample-build.lock.json')
    receipt['complete'] = True
    save()
    print('SBT RUNTIME SAMPLE PASSED: ' + args.fixture + ' ' + args.sbt_version, flush=True)


if __name__ == '__main__':
    main()
