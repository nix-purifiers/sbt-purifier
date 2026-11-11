#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Native JVM input selection, sandbox build, and projected publication."""
from pathlib import Path
import argparse, json, os, re, shutil, tempfile
from urllib.parse import unquote, urlparse
from composition_evidence import Evidence, sha256, observed_origins, terminal_text
import xml.etree.ElementTree as ET

def compose(args, evidence):
    root = Path(__file__).resolve().parent.parent
    system = evidence.output(['nix', 'eval', '--impure', '--raw', '--expr',
                                      'builtins.currentSystem'], text=True).strip()
    revision = evidence.output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    assert not evidence.output(['git', '-C', str(root), 'diff', '--binary', 'HEAD'], text=True).strip(), \
        'composition evidence requires committed producer source'
    producer_reference = 'git+' + root.as_uri() + '?rev=' + revision
    lock_bytes = (root / 'flake.lock').read_bytes()
    archived = json.loads(evidence.output(
        ['nix', 'flake', 'archive', '--json', '--no-update-lock-file', producer_reference], text=True))
    snapshot = archived['path']
    def check_archived_inputs(node):
        path = Path(node['path'])
        assert path.is_dir() and str(path.resolve()) == str(path), path
        for value in node.get('inputs', {}).values():
            check_archived_inputs(value)
    check_archived_inputs(archived)
    assert (root / 'flake.lock').read_bytes() == lock_bytes, 'archive changed the producer lockfile'
    evidence.receipt.update(sourceRevision=revision, system=system, requestedSbtVersion=args.sbt_version,
        sourceArchive=archived, driverSha256=sha256(__file__))
    evidence.save()
    source_nar_hash = evidence.output(['nix', 'hash', 'path', snapshot], text=True).strip()
    evidence.receipt['sourceNarHash'] = source_nar_hash
    input_hashes = {}
    def record_input(node):
        path = node['path']
        if path not in input_hashes:
            input_hashes[path] = evidence.output(['nix', 'hash', 'path', path], text=True).strip()
        for value in node.get('inputs', {}).values():
            record_input(value)
    record_input(archived)
    evidence.receipt['sourceInputNarHashes'] = input_hashes
    evidence.receipt['flakeLockSha256'] = sha256(root / 'flake.lock')
    evidence.receipt['sandbox'] = evidence.output(['nix', 'config', 'show', 'sandbox'], text=True).strip()
    assert evidence.receipt['sandbox'] == 'true', 'composition builds require Nix sandbox'
    evidence.save()
    print(json.dumps({'producerSource': {'revision': revision, 'reference': producer_reference,
        'path': snapshot, 'narHash': source_nar_hash}, 'system': system, 'sbtVersion': args.sbt_version}), flush=True)
    with tempfile.TemporaryDirectory(prefix='sbt-native-composition-') as temporary, evidence.case():
        temporary = Path(temporary)
        package = evidence.output(['nix', 'build', producer_reference + '#sbt',
            '--print-out-paths', '--out-link', str(temporary / 'wrapper')], text=True).strip().splitlines()[-1]
        evidence.selected('wrapper', package)
        sbt = Path(package) / 'bin/sbt'
        evidence.receipt['wrapperSha256'] = sha256(sbt)
        provider = evidence.output(['nix', 'eval', '--raw', producer_reference + '#checks.' + system + '.composition-producer.outPath'], text=True).strip()
        project = temporary / 'native-project'
        (project / 'project').mkdir(parents=True)
        (project / 'project/build.properties').write_text('sbt.version=' + args.sbt_version + '\n')
        provider_reference = producer_reference + '#checks.' + system + '.composition-producer'
        (project/'build.sbt').write_text('''ThisBuild / scalaVersion := "2.12.20"
    ThisBuild / nixFlakeRepositories += NixFlakeRepository("lang3", '''+json.dumps(provider_reference)+''')
    organization := "example"
    name := "native-input-consumer"
    version := "1.0.0"
    crossPaths := false
    autoScalaLibrary := false
    libraryDependencies ++= Seq(
      "org.apache.commons" % "commons-text" % "1.13.0",
      "org.apache.commons" % "commons-lang3" % "[99,100)"
    )
    Compile / mainClass := Some("example.NativeMain")
    ''')
        source=project/'src/main/java/example/NativeMain.java'
        source.parent.mkdir(parents=True)
        source.write_text('''package example;
    public class NativeMain {
      public static void main(String[] args) throws Exception {
        String result = org.apache.commons.text.WordUtils.capitalizeFully("hello nix");
        if (!result.equals("Hello Nix")) throw new AssertionError(result);
        System.out.println(result);
        System.out.println("COMPOSITION_JAVA_HOME|" + System.getProperty("java.home"));
        System.out.println("COMPOSITION_JAVA_VERSION|" + System.getProperty("java.runtime.version"));
        for (Class<?> cls : new Class<?>[] {NativeMain.class, org.apache.commons.text.WordUtils.class, org.apache.commons.lang3.StringUtils.class}) {
          java.net.URL origin = cls.getProtectionDomain().getCodeSource().getLocation();
          java.nio.file.Path payload = java.nio.file.Paths.get(origin.toURI());
          String checksum = "directory";
          if (java.nio.file.Files.isRegularFile(payload)) {
            StringBuilder hash = new StringBuilder();
            for (byte value : java.security.MessageDigest.getInstance("SHA-256").digest(java.nio.file.Files.readAllBytes(payload))) {
              hash.append(String.format("%02x", value & 255));
            }
            checksum = hash.toString();
          }
          System.out.println("COMPOSITION_ORIGIN|" + cls.getName() + "|" + origin + "|" + checksum);
        }
      }
    }
    ''')
        definition = project / 'build.sbt'
        if args.sbt_version.startswith('0.13.'):
            definition.write_text(definition.read_text()
                .replace('ThisBuild / scalaVersion', 'scalaVersion in ThisBuild')
                .replace('ThisBuild / nixFlakeRepositories', 'nixFlakeRepositories in ThisBuild')
                .replace('Compile / mainClass', 'mainClass in Compile')
                .replace('2.12.20', '2.10.7'))
        elif args.sbt_version.startswith('2.'):
            definition.write_text(definition.read_text().replace('2.12.20', '3.8.4'))
        env = dict(os.environ, SBT_NIX_BACKEND_MAVEN_CENTRAL_MIRROR='https://repo.maven.apache.org/maven2/')
        def run(*commands):
            output = evidence.output([str(sbt), '-batch', ';' + ';'.join(commands)], cwd=project, env=env, text=True)
            evidence.receipt.setdefault('nativeCommandsPassed', []).append(list(commands))
            evidence.save()
            return output
        settings = run('show sbtVersion', 'show scalaVersion', 'show javaHome')
        plain_settings = terminal_text(settings)
        assert re.search(r'(?m)^\[info\]\s*' + re.escape(args.sbt_version) + r'\s*$', plain_settings), 'actual sbt version differs'
        evidence.receipt['nativeSettings'] = settings
        run('nixLock')
        locked = json.loads((project / 'sbt-purifier.lock.json').read_text())
        assert locked['sbtVersion'] == args.sbt_version
        evidence.receipt['nativeLock'] = locked
        shutil.copy2(project / 'sbt-purifier.lock.json', evidence.directory / 'sbt-purifier.lock.json')
        shutil.copy2(project / 'build.sbt', evidence.directory / 'build.sbt')
        shutil.copytree(project / 'src', evidence.directory / 'project-src')
        shutil.copytree(project / 'project', evidence.directory / 'project-definition', ignore=shutil.ignore_patterns('target', 'project'))
        evidence.save()
        assert len(locked['flakeRepositories']) == 1
        assert locked['buildDirectory'] == project.name
        assert not any('/commons-lang3/0.0.1-nix-' in entry['path'] for entry in locked['artifacts'])
        native_output = run('run', 'makePom', 'nixRuntimeRepository')
        evidence.receipt['nativeRunOutput'] = native_output
        plain_native = terminal_text(native_output)
        java_home = re.search(r'COMPOSITION_JAVA_HOME\|([^\r\n]+)', plain_native).group(1).strip()
        java_version = re.search(r'COMPOSITION_JAVA_VERSION\|([^\r\n]+)', plain_native).group(1).strip()
        java_major = java_version.split('.')[1] if java_version.startswith('1.') else java_version.split('.')[0]
        assert java_major == locked['javaVersion'], 'actual project JVM differs from lock'
        assert java_home.startswith('/nix/store/'), 'native JVM must have an immutable SDK identity'
        evidence.receipt['nativeJvm'] = {'home': java_home, 'version': java_version, 'role': 'native-project-runtime'}
        evidence.selected('native-project-jdk', '/'.join(java_home.split('/')[:4]))
        native_dependencies = observed_origins(plain_native,
            {'org.apache.commons.text.WordUtils', 'org.apache.commons.lang3.StringUtils'})
        provider_jar = Path(provider) / 'org/apache/commons/commons-lang3/0.0.1/commons-lang3-0.0.1.jar'
        assert native_dependencies['org.apache.commons.lang3.StringUtils']['sha256'] == sha256(provider_jar), 'native consumer did not load selected Nix provider bytes'
        evidence.receipt['nativeLoadedDependencies'] = native_dependencies
        evidence.save()
        evidence.selected('nix-input-provider', provider)
        published = temporary / 'published'
        scope = 'publishTo'
        setting = 'set ' + scope + ' := Some("native-test" at ' + json.dumps(published.as_uri()) + ')'
        shutil.rmtree(project / 'target/classes', ignore_errors=True)
        run(setting, 'nixPublish')
        assert not (project / 'target/classes').exists(), 'publication compiled the working tree'
        roots = project / 'target/sbt-nix-backend/gcroots'
        selected = sorted(roots.glob('package-*'))
        assert any(p.name.endswith('-publication') for p in selected), 'missing actual publication output root'
        assert any(p.name.endswith('-repository') for p in selected), 'missing actual runtime repository output root'
        for output in selected:
            record = evidence.selected(output.name, output)
            if output.name.endswith('-repository'):
                definition = record['derivation']['derivations'][Path(record['drvPath']).name]
                for name in ('out', 'publications'):
                    path = definition['outputs'][name]['path']
                    evidence.selected('consumer-package-' + name, path if path.startswith('/') else '/nix/store/' + path)
        publication_roots = [p.resolve() / 'maven' for p in selected if p.name.endswith('-publication')]
        assert len(publication_roots) == 1, 'fixture must select exactly one project publication'
        publication_joins = {}
        for path in sorted(published.rglob('*')):
            if path.is_file() and path.suffix in ('.jar', '.pom'):
                relative = path.relative_to(published)
                selected_path = publication_roots[0] / relative
                assert selected_path.is_file() and sha256(path) == sha256(selected_path), 'native publisher changed selected Nix publication bytes'
                publication_joins[str(relative)] = {'publishedSha256': sha256(path),
                    'selectedPath': str(selected_path), 'selectedSha256': sha256(selected_path)}
        assert publication_joins, 'no actual selected publication artifacts joined'
        evidence.receipt['selectedPublicationByteJoins'] = publication_joins
        library = project / 'target/sbt-nix-backend/nix-lib'
        shutil.copytree(library, evidence.directory / 'generated-nix-library')
        shutil.copytree(published, evidence.directory / 'published')
        evidence.files('published-files', published)
        evidence.receipt['publishedPomPath'] = str(published)
        evidence.save()
        pom = next((published / 'example/native-input-consumer').glob('*/*.pom'))
        assert '-nix-' in pom.parent.name
        dependencies = ET.parse(pom).findall('.//{*}dependency')
        for name in ['commons-text', 'commons-lang3']:
            dependency = next(node for node in dependencies if node.findtext('{*}artifactId') == name)
            version = dependency.findtext('{*}version')
            assert version.startswith('[') and version.endswith(']') and '-nix-' in version
            revision = version[1:-1]
            path = published / 'org/apache/commons' / name / revision
            assert (path / (name + '-' + revision + '.jar')).is_file()
        assert (published / 'org/apache/commons/commons-text').is_dir()
        expected_origins = {'example.NativeMain': pom.with_suffix('.jar')}
        for cls, name in [('org.apache.commons.text.WordUtils', 'commons-text'), ('org.apache.commons.lang3.StringUtils', 'commons-lang3')]:
            dep = next(node for node in dependencies if node.findtext('{*}artifactId') == name)
            ver = dep.findtext('{*}version')[1:-1]
            expected_origins[cls] = published / 'org/apache/commons' / name / ver / (name + '-' + ver + '.jar')
            assert native_dependencies[cls]['sha256'] == sha256(expected_origins[cls]), 'publication changed native dependency payload'
        # Resolve the deployed descriptors with an ordinary Maven client. This
        # also requires uploaded parent/BOM POMs, not just the project JARs.
        expression = """let
          f = builtins.getFlake (builtins.getEnv "COMPOSITION_FLAKE");
          p = f.inputs.nixpkgs.legacyPackages.${builtins.currentSystem};
        in p.runCommand "sbt-composition-downstream-tools" {} ''
          mkdir -p "$out/bin"
          ln -s ${p.maven}/bin/mvn "$out/bin/mvn"
          ln -s ${p.jdk21_headless.home} "$out/jdk"
        '' """
        tools = Path(evidence.output(['nix', 'build', '--impure', '--print-out-paths',
            '--out-link', str(temporary / 'maven-tools'), '--expr', expression], text=True,
            env=dict(os.environ, COMPOSITION_FLAKE=producer_reference)).strip())
        evidence.selected('ordinary-maven-tools', tools)
        downstream = temporary / 'ordinary-downstream'
        downstream.mkdir()
        (downstream / 'pom.xml').write_text(f"""<project><modelVersion>4.0.0</modelVersion>
          <groupId>example</groupId><artifactId>downstream</artifactId><version>1</version>
          <repositories><repository><id>projected</id><url>{published.as_uri()}</url></repository></repositories>
          <dependencies><dependency><groupId>example</groupId><artifactId>native-input-consumer</artifactId>
          <version>{pom.parent.name}</version></dependency></dependencies></project>""")
        plain = dict(os.environ, JAVA_HOME=str(tools / 'jdk'))
        evidence.run([str(tools / 'bin/mvn'), '-B', '-Dmaven.repo.local='+str(temporary / 'ordinary-m2'),
            'dependency:build-classpath', '-Dmdep.outputFile=classpath'], cwd=downstream, env=plain, check=True)
        output = evidence.output([str(tools / 'jdk/bin/java'), '-cp',
            (downstream / 'classpath').read_text().strip(), 'example.NativeMain'],
            cwd=downstream, env=plain, text=True)
        assert 'Hello Nix' in output, output
        observations = observed_origins(output, set(expected_origins))
        for cls, observation in observations.items():
            jar = Path(unquote(urlparse(observation['origin']).path))
            assert sha256(jar) == observation['sha256'] == sha256(expected_origins[cls]), 'ordinary consumer loaded different publication bytes'
            observation['publishedSha256'] = sha256(expected_origins[cls])
        evidence.receipt['ordinaryConsumer'] = {'stdout': output, 'loadedOrigins': observations}
        evidence.receipt['operationResults'] = {name: 'passed' for name in [
            'native-lock', 'project-build-scripts', 'native-input-composition',
            'sandbox-publication', 'native-publish', 'ordinary-maven-consumer',
            'ordinary-consumer-loaded-origin-equality']}
        evidence.receipt['complete'] = True
        evidence.save()
    print('SBT NATIVE COMPOSITION PASSED: ' + args.sbt_version)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sbt-version', default='1.11.7')
    parser.add_argument('--results', required=True, type=Path)
    args = parser.parse_args()
    evidence = Evidence(args.results)
    try:
        compose(args, evidence)
    except BaseException as error:
        evidence.receipt['complete'] = False
        evidence.receipt['failure'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        evidence.close()
        evidence.save()


if __name__ == '__main__':
    main()
