# SPDX-License-Identifier: MIT
#
# The flake's checks. Every one builds in the sandbox; the only network
# access is the fixed-output fetches of locked artifacts.
{
  pkgs,
  # The flake's default package overlay, which adds buildSbtProject.
  overlay,
  # The library of the flake, with lib.sbt-purifier and lib.purifier.
  flakeLib,
  plugin,
  plugin-repository,
}:
let
  inherit (pkgs) lib;

  # Every fixture builds the way a consumer's package does: through
  # buildSbtProject of a package set the overlay applies to.
  withOverlay = pkgs.extend overlay;
  inherit (withOverlay.sbt-purifier) buildSbtProject;
  independent = import ./independent {
    inherit pkgs buildSbtProject;
    pluginRepository = plugin-repository;
  };

  # For the conformance checks: whether readLock reports exactly one
  # problem, which says `infix` and names the command that fixes it.
  problemOf =
    infix: args:
    let
      problems = (flakeLib.sbt-purifier.readLock args).problems;
    in
    builtins.length problems == 1
    && lib.hasInfix infix (builtins.head problems)
    && lib.hasInfix "run `sbt-purifier lock`" (builtins.head problems);
  # Whether the builder fails evaluation on the lock.
  evaluationFails =
    args:
    !(builtins.tryEval (
      builtins.deepSeq (buildSbtProject args).packages (buildSbtProject args).packages
    )).success;
  conformance =
    name: ok:
    assert ok || throw "${name}: the lockfile problem is not reported as the contract says";
    pkgs.runCommand name { } "touch $out";
  multiLockText = builtins.readFile ./fixtures/multi/sbt-purifier.lock.json;
  # Trailing whitespace preserves valid JSON but changes the hashed bytes,
  # including after the scheduled updater relocks a newer sbt version.
  editedLock = builtins.toFile "sbt-purifier.lock.json" (multiLockText + "\n");
  # The multi lockfile without the downloads whose paths `drop` matches,
  # written as the plugin writes a lockfile: the version line, the content
  # hash of the text without its line, and the rest.
  lockWithout =
    drop:
    let
      lock = builtins.fromJSON multiLockText;
      fields = builtins.removeAttrs lock [
        "version"
        "contentHash"
      ];
      json = builtins.toJSON (
        fields // { artifacts = builtins.filter (a: !(drop a.path)) lock.artifacts; }
      );
      rest = builtins.substring 1 (builtins.stringLength json) json;
      head = "{\n  \"version\": ${toString lock.version},\n";
    in
    builtins.toFile "sbt-purifier.lock.json" "${head}  \"contentHash\": \"${
      builtins.hashString "sha256" (head + rest)
    }\",\n${rest}";

  # The fixtures were locked with the plugin injected (see
  # tests/relock-fixtures.sh), so their builds inject a plugin. multi takes
  # the builder's default, the plugin built from this revision.
  multi = buildSbtProject {
    src = ./fixtures/multi;
    # app's munit suite runs in the build, and it reads the license the
    # third-party plugin fetched outside coursier.
    doCheck = true;
  };

  # Java 8's default source/Javadoc encoding follows the process locale.
  # The ordinary lock capture uses UTF-8; a C-locale sandbox must not turn
  # non-ASCII documentation into an unmappable-character failure.
  unicodeJavadoc = buildSbtProject {
    src = pkgs.runCommand "sbt-unicode-javadoc-source" { } ''
      cp -r ${./fixtures/multi} $out
      chmod -R u+w $out
      cat > $out/javalib/src/main/java/example/javalib/Shout.java <<'EOF'
      package example.javalib;
      import org.apache.commons.text.WordUtils;
      /** Describes a café. */
      public final class Shout {
          private Shout() {}
          public static String shout(String s) { return WordUtils.capitalizeFully(s); }
      }
      EOF
    '';
    lockFile = ./fixtures/multi/sbt-purifier.lock.json;
    jdk = pkgs.jdk8_headless;
  };

  # sbt 0.13's implicit project id must survive copying into a sandbox.
  legacy = buildSbtProject {
    src = ./fixtures/sbt013;
    pluginRepository = plugin-repository;
    doCheck = true;
  };

  # An sbt 2 build, which injects the plugin's sbt 2 variant.
  sbt2 = buildSbtProject {
    src = ./fixtures/sbt2;
    pluginRepository = plugin-repository;
    doCheck = true;
  };

  # A build that installs the plugin in project/project/plugins.sbt and was
  # locked with plain sbt; its Nix build names the plugin repository in
  # SBT_NIX_BACKEND_REPOSITORY.
  metameta = buildSbtProject {
    src = ./fixtures/metameta;
    pluginRepository = plugin-repository;
    doCheck = true;
  };

  # A build that resolves with Ivy (useCoursier := false), in its
  # meta-build too; in the sandbox Ivy reads the locked downloads through
  # a file: mirror of their URLs.
  ivy = buildSbtProject {
    src = ./fixtures/ivy;
    pluginRepository = plugin-repository;
    doCheck = true;
  };

  # Builds whose projects come from projectMatrix, on sbt 1 (the
  # sbt-projectmatrix plugin) and on sbt 2 (built in): a project per Scala
  # version, each with a package of its own.
  matrix = buildSbtProject {
    src = ./fixtures/matrix;
    pluginRepository = plugin-repository;
  };
  matrix2 = buildSbtProject {
    src = ./fixtures/matrix2;
    pluginRepository = plugin-repository;
  };

  # The consumer's lock pins a flake reference to the producer's
  # repository; the check passes the producer's output instead, as a flake
  # with the producer as an input would.
  consumer = buildSbtProject {
    src = ./fixtures/consumer;
    pluginRepository = plugin-repository;
    flakeRepositories = {
      multi = multi.sbtRepositories.app;
      ivy = ivy.sbtRepositories.ivyjava;
    };
  };

  # Resolves `dependency` (an sbt ModuleID expression) from `repository`
  # alone and runs `check` with the runtime classpath in $cp.
  #
  # The resolver is sbt (coursier inside it), booted offline from the multi
  # fixture's locked cache. The throwaway build it runs reads `repository`
  # as a consumer does, with a Maven resolver and an Ivy resolver in the
  # standard Ivy layout, and has no managed Scala, so every file of the
  # module's runtime classpath must come from the repository: a file
  # repository is read in place, never through the cache, and the build is
  # offline.
  resolveFromRepository =
    name: repository: dependency: check:
    let
      consumerBuild = pkgs.writeTextDir "build.sbt" ''
        autoScalaLibrary := false
        managedScalaInstance := false
        crossPaths := false
        externalResolvers := Seq(
          MavenRepository("repository", "file://${repository}"),
          Resolver.file("repository-ivy", file("${repository}"))(Resolver.ivyStylePatterns)
        )
        libraryDependencies += ${dependency}
      '';
    in
    pkgs.runCommand name { nativeBuildInputs = [ multi.jdk ]; } ''
      export HOME="$TMPDIR/home" COURSIER_CACHE="$TMPDIR/cache" COURSIER_MODE=offline
      mkdir -p "$HOME" consumer/project
      cp -rs --no-preserve=mode ${multi.dependencies}/. "$COURSIER_CACHE"
      cp ${consumerBuild}/build.sbt consumer/
      echo sbt.version=${multi.lock.sbtVersion} > consumer/project/build.properties
      cd consumer
      java -Duser.home="$HOME" -Djava.io.tmpdir="$TMPDIR" -Dsbt.global.base="$HOME/.sbt" \
        -Dsbt.boot.directory="$HOME/.sbt/boot" -Dsbt.ivy.home="$HOME/.ivy2" \
        -Dsbt.server.autostart=false -Dsbt.server.forcestart=true -Dsbt.log.noformat=true \
        -jar "$COURSIER_CACHE/${multi.lock.launcher}" "export Runtime/managedClasspath" </dev/null > sbt-output
      cp=$(tail -n 1 sbt-output)
      case "$cp" in ${repository}/*) ;; *) cat sbt-output >&2; echo "no classpath from the repository" >&2; exit 1 ;; esac
      tr ':' '\n' <<<"$cp" | grep -v '^${repository}/' && { echo "classpath entries outside the repository" >&2; exit 1; }
      ${check}
      # The output references what the check read, so a CI cache that
      # roots the checks keeps those builds.
      mkdir -p $out
      ln -s ${repository} $out/repository
      ln -s ${multi.dependencies} $out/sbt-downloads
    '';

  # Resolves `module` (organization:name:version) from `repository` alone
  # and runs `mainClass`; the check passes when the output holds every
  # line of `expected`.
  runFromRepository =
    name: repository: module: mainClass: expected:
    let
      coordinates = lib.splitString ":" module;
    in
    resolveFromRepository name repository
      ''"${builtins.elemAt coordinates 0}" % "${builtins.elemAt coordinates 1}" % "${builtins.elemAt coordinates 2}"''
      ''
        java -cp "$cp" ${mainClass} > output
        cat output
        ${lib.concatMapStrings (line: ''
          grep -qxF ${lib.escapeShellArg line} output || { echo "missing output line: ${line}" >&2; exit 1; }
        '') expected}
      '';

  # A build defined outside its sbt project: buildSbtProject of the
  # top-level directory of the project, which holds the build and its
  # lockfile and no Nix file. The lockfile names every artifact of every
  # package, by variant and key, before anything is built. The check
  # requires that the packages named in `variants` have exactly the
  # variants given there. It builds each package and requires that its
  # link directory holds exactly the links the lockfile names, and that
  # their targets are exactly the files the build published besides
  # checksums. It links every artifact as <project>/<variant>/<key>.
  externalizedBuild =
    name: build: variants:
    let
      actualVariants = builtins.mapAttrs (
        id: _: builtins.attrNames build.packages.${id}.artifacts
      ) variants;
    in
    assert
      actualVariants == builtins.mapAttrs (_: lib.sort lib.lessThan) variants
      || throw "${name}: the variants are ${builtins.toJSON actualVariants}";
    pkgs.runCommand name { } ''
      mkdir $out
      ${lib.concatStrings (
        lib.mapAttrsToList (
          id: package:
          let
            named = lib.concatMap (
              v: [ "${v.name}/descriptor" ] ++ map (a: "${v.name}/${a.key}") v.artifacts
            ) package.variants;
          in
          ''
            check() {
              if [ "$1" != "$2" ]; then
                echo "${id}: $3" >&2
                diff <(echo "$1") <(echo "$2") >&2 || true
                exit 1
              fi
            }
            links=${package}/nix-support/artifacts
            check "$(printf '%s\n' ${lib.escapeShellArgs named} | sort)" \
              "$(cd $links && find . -mindepth 2 -printf '%P\n' | sort)" \
              "the link directory holds other links than the lockfile names"
            check "$(cd ${package} && find . -path ./nix-support -prune -o -type f -not -name '*.sha1' -not -name '*.md5' -printf '%P\n' | sort)" \
              "$(cd $links && find . -mindepth 2 -exec realpath --relative-to=${package} {} \; | sort -u)" \
              "the links reach other files than the build published"
            ${lib.concatStrings (
              lib.mapAttrsToList (
                variant: files:
                ''
                  mkdir -p $out/${id}/${variant}
                ''
                + lib.concatStrings (
                  lib.mapAttrsToList (key: file: ''
                    ln -s ${file} $out/${id}/${variant}/${lib.escapeShellArg key}
                  '') files
                )
              ) package.artifacts
            )}
          ''
        ) build.packages
      )}
    '';

  # A repository holds every one of `files`.
  repositoryLayouts =
    name: repository: files:
    pkgs.runCommand name { } ''
      cd ${repository}
      ls ${lib.escapeShellArgs files}
      mkdir -p $out
      ln -s ${repository} $out/repository
    '';

  # The `out` output of a package holds the project's own artifacts, under
  # `dir`, and nothing else; `files` are paths below `dir` it must hold.
  # The default is a POM and a jar of the Maven layout.
  onlyOwnArtifacts =
    name: package: dir:
    {
      files ? [
        "*/*.pom"
        "*/*.jar"
      ],
    }:
    pkgs.runCommand name { } ''
      cd ${package}
      # nix-support holds the links of the package to its own artifacts.
      others=$(find . -mindepth 2 -maxdepth 3 -type d -path './*/*' -not -path './nix-support/*' | grep -v '^./${dir}' || true)
      test -d ${dir} || { echo "${package} does not hold ${dir}" >&2; exit 1; }
      test -z "$others" || { echo "${package} holds more than ${dir}: $others" >&2; exit 1; }
      ls ${lib.concatMapStringsSep " " (f: "${dir}/${f}") files}
      mkdir -p $out
      ln -s ${package} $out/package
    '';

in
{
  implementation-lifecycle-driver =
    let
      bootstrap = pkgs.callPackage flakeLib.purifier.implementation {
        source = { };
        repository = "nix-purifiers/sbt-purifier";
        tool = "sbt-purifier";
        nativeCommand = "sbt";
        checksum = "sbt";
        lockVersions = [ 3 ];
      };
    in
    pkgs.writeShellApplication {
      name = "sbt-implementation-lifecycle";
      runtimeInputs = [
        pkgs.nix
        pkgs.git
        pkgs.python3
      ];
      text = ''
        exec python ${./implementation-lifecycle.py} \
          --baseline ${./implementation-baseline.json} \
          --bootstrap ${bootstrap.tool} --source-model ${bootstrap.sourceModel} "$@"
      '';
    };
  implementation-override-guards =
    let
      scope = withOverlay.sbt-purifier;
      metadataOnly = scope.sbt.overrideAttrs (old: {
        meta = (old.meta or { }) // {
          priority = 7;
        };
      });
      customCoreutils = pkgs.coreutils.overrideAttrs (old: {
        pname = old.pname + "-unrecorded";
      });
    in
    assert !(builtins.tryEval ((scope.sbt.override { pluginVersion = "unrecorded"; }).drvPath)).success;
    assert
      !(builtins.tryEval (
        (scope.sbt-purifier.override {
          pkgs = pkgs // {
            coreutils = customCoreutils;
          };
        }).drvPath
      )).success;
    assert metadataOnly.drvPath == scope.sbt.drvPath && metadataOnly.meta.priority == 7;
    pkgs.runCommand "sbt-implementation-override-guards" { } "touch $out";
  implementation-scope-guards =
    let
      source = {
        rev = "1111111111111111111111111111111111111111";
        narHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
      };
      scope = lib.makeScope pkgs.newScope (
        self:
        import ../pkgs {
          callPackage = self.callPackage;
          lib = flakeLib;
          implementationLib = flakeLib;
          implementationNixpkgs = pkgs.path;
          implementationSource = source;
        }
      );
      custom =
        name:
        scope.overrideScope (
          final: prev: {
            pkgs = pkgs // {
              ${name} = pkgs.${name}.overrideAttrs (old: {
                pname = old.pname + "-unrecorded";
              });
            };
          }
        );
      pythonScope = custom "python3";
      coreutilsScope = custom "coreutils";
      customConstructor = scope.overrideScope (
        final: prev: {
          writeShellScriptBin = name: text: pkgs.writeShellScriptBin name (text + "\necho changed\n");
        }
      );
      rejected = package: !(builtins.tryEval package.drvPath).success;
    in
    assert scope.sbt.implementation.source.rev == source.rev;
    assert scope.sbt-purifier.implementation.source.rev == source.rev;
    assert rejected pythonScope.sbt;
    assert rejected pythonScope.sbt-purifier;
    assert rejected coreutilsScope.sbt-purifier;
    assert rejected customConstructor.sbt-purifier;
    pkgs.runCommand "sbt-implementation-scope-guards" { } "touch $out";
  implementation-lock-path = pkgs.runCommand "sbt-purifier-implementation-lock-path" { } ''
    mkdir -p stub
    cat > stub/realpath <<'SH'
    #!${pkgs.runtimeShell}
    exit 77
    SH
    chmod +x stub/realpath
    status=0
    PATH="$PWD/stub:$PATH" ${withOverlay.sbt-purifier.sbt-purifier}/bin/sbt-purifier \
      invalid-action --lockfile missing/../external/lock.json >stdout 2>stderr || status=$?
    test "$status" = 2
    grep -F 'usage: sbt-purifier' stderr
    touch "$out"
  '';
  implementation-mode =
    pkgs.runCommand "sbt-purifier-implementation-mode" { nativeBuildInputs = [ pkgs.python3 ]; }
      ''
        mkdir -p tests pkgs/sbt
        cp ${./test_implementation_mode.py} tests/test_implementation_mode.py
        cp ${../pkgs/sbt/implementation-mode.py} pkgs/sbt/implementation-mode.py
        python -B tests/test_implementation_mode.py
        touch "$out"
      '';
  jvm-maven-projection = multi.packages.javalib.projections.maven;
  jvm-ivy-projection = ivy.packages.ivyjava.projections.maven;

  # The plugin built by itself from its lock, with its unit tests run.
  inherit plugin;

  independent-sample-sbt1 = independent.tests;
  independent-sample-offline-tasks = independent.offline;
  sbt2-tests = sbt2.tests;

  nix-library = import ./nix-library.nix { inherit pkgs overlay flakeLib; };

  # Feature 1: the coursier cache of the multi fixture.
  multi-dependencies = multi.dependencies;

  jdk8-unicode-javadoc =
    pkgs.runCommand "sbt-jdk8-unicode-javadoc"
      {
        nativeBuildInputs = [ pkgs.unzip ];
      }
      ''
        jar=$(find ${unicodeJavadoc.packages.javalib} -name '*-javadoc.jar' -print -quit)
        test -n "$jar"
        unzip -p "$jar" example/javalib/Shout.html > $TMPDIR/Shout.html
        grep -F 'café' $TMPDIR/Shout.html
        touch $out
      '';

  # Feature 2: each package holds its own artifacts.
  multi-app-artifacts = onlyOwnArtifacts "multi-app-artifacts" multi.packages.app "example/app_3" { };
  multi-core-artifacts =
    onlyOwnArtifacts "multi-core-artifacts" multi.packages.core "example/core_3"
      { };
  multi-javalib-artifacts =
    onlyOwnArtifacts "multi-javalib-artifacts" multi.packages.javalib "example/javalib"
      { };

  # Feature 4: a repository is enough to resolve and run its module.
  multi-app-repository =
    runFromRepository "multi-app-repository" multi.sbtRepositories.app "example:app_3:1.0.0"
      "example.Main"
      [
        ''{"hello":"Nix World"}''
        "Apache License"
        "version 1.0.0"
      ];

  # Feature 5: a build that resolves another build's repository through a
  # flake repository, and whose own repository holds what it took from it.
  consumer-repository =
    runFromRepository "consumer-repository" consumer.sbtRepositories.consumer "example:consumer_3:1.0.0"
      "consumer.Main"
      [
        ''consumer: {"hello":"Nix World"}''
        "consumer: published Ivy-style"
      ];
  # The consumer's repository holds the module it took from the ivy
  # fixture's flake repository in the standard Ivy layout.
  consumer-repository-layouts =
    repositoryLayouts "consumer-repository-layouts" consumer.sbtRepositories.consumer
      [
        "example/ivyjava/4.0.0/ivys/ivy.xml"
        "example/ivyjava/4.0.0/jars/ivyjava.jar"
      ];

  # The same on sbt 2: the build locks and builds with the plugin's sbt 2
  # variant injected, its test suite runs, and the app's repository alone
  # resolves and runs the app.
  sbt2-app-artifacts = onlyOwnArtifacts "sbt2-app-artifacts" sbt2.packages.app "example/app_3" { };
  sbt2-app-repository =
    runFromRepository "sbt2-app-repository" sbt2.sbtRepositories.app "example:app_3:2.0.0"
      "example2.Main"
      [
        ''{"sbt":2}''
        "version 2.0.0"
        "interface xsbti.Logger"
      ];
  # An sbt 2 plugin published Ivy-style is named <name>_sbt2_<scala>, as
  # sbt 2 names it in either style, and its repository resolves it.
  sbt2-ivyplugin-artifacts =
    onlyOwnArtifacts "sbt2-ivyplugin-artifacts" sbt2.packages.ivyplugin2 "example/ivyplugin2_sbt2_3"
      {
        files = [
          "2.0.0/ivys/ivy.xml"
          "2.0.0/jars/ivyplugin2_sbt2_3.jar"
        ];
      };
  sbt2-ivyplugin-repository =
    resolveFromRepository "sbt2-ivyplugin-repository" sbt2.sbtRepositories.ivyplugin2
      ''"example" % "ivyplugin2_sbt2_3" % "2.0.0"''
      ''
        tr ':' '\n' <<<"$cp" | grep -qx '${sbt2.sbtRepositories.ivyplugin2}/example/ivyplugin2_sbt2_3/2.0.0/jars/ivyplugin2_sbt2_3.jar' ||
          { echo "the plugin jar is not on the classpath: $cp" >&2; exit 1; }
      '';

  # sbt 2 publishes the lib Ivy-style, and the app's repository holds it
  # and sbt's interface module in the standard Ivy layout.
  sbt2-lib-artifacts = onlyOwnArtifacts "sbt2-lib-artifacts" sbt2.packages.lib "example/lib_3" {
    files = [
      "2.0.0/ivys/ivy.xml"
      "2.0.0/jars/lib_3.jar"
    ];
  };
  sbt2-app-repository-layouts =
    repositoryLayouts "sbt2-app-repository-layouts" sbt2.sbtRepositories.app
      [
        "example/app_3/2.0.0/app_3-2.0.0.pom"
        "example/lib_3/2.0.0/ivys/ivy.xml"
        "example/lib_3/2.0.0/jars/lib_3.jar"
        "org.scala-sbt/interface/0.13.18/ivys/ivy.xml"
        "org.scala-sbt/interface/0.13.18/jars/interface.jar"
      ];

  # A build that installs the plugin once, in its meta-most build, under
  # plain sbt: the plugin reaches the meta-build and the build (both use its
  # keys), the Nix build supplies its repository, and the app's repository
  # alone resolves and runs the app.
  metameta-app-repository =
    runFromRepository "metameta-app-repository" metameta.sbtRepositories.app "example:app_3:3.0.0"
      "metameta.Main"
      [
        ''{"installed":"project/project"}''
        "version 3.0.0"
      ];

  # A build that resolves with Ivy builds in the sandbox, and its tests run.
  # The app's repository alone resolves and runs the app: it holds the app
  # in Maven layout, and in the standard Ivy layout the Ivy-style lib and
  # sbt's interface module, which came from an Ivy-layout repository.
  ivy-app-repository =
    runFromRepository "ivy-app-repository" ivy.sbtRepositories.app "example:app_2.13:4.0.0"
      "ivyexample.Main"
      [
        ''{"resolvedWith":"ivy"}''
        "version 4.0.0"
        "interface xsbti.Logger"
      ];
  ivy-app-repository-layouts =
    repositoryLayouts "ivy-app-repository-layouts" ivy.sbtRepositories.app
      [
        "example/app_2.13/4.0.0/app_2.13-4.0.0.pom"
        "example/lib_2.13/4.0.0/ivys/ivy.xml"
        "example/lib_2.13/4.0.0/jars/lib_2.13.jar"
        "org.scala-sbt/interface/0.13.18/ivys/ivy.xml"
        "org.scala-sbt/interface/0.13.18/jars/interface.jar"
      ];

  # A project that resolves through an Ivy settings file builds in the
  # sandbox, and its repository alone runs it. The repository holds sbt's
  # interface module, which the settings read with patterns other than the
  # standard Ivy layout, at its standard Ivy path.
  ivy-settingsfile-repository =
    runFromRepository "ivy-settingsfile-repository" ivy.sbtRepositories.settingsfile
      "example:settingsfile_2.13:4.0.0"
      "ivyexample.Settings"
      [
        ''{"resolvedWith":"ivysettings.xml"}''
        "interface xsbti.Logger"
      ];
  ivy-settingsfile-repository-layouts =
    repositoryLayouts "ivy-settingsfile-repository-layouts" ivy.sbtRepositories.settingsfile
      [
        "example/settingsfile_2.13/4.0.0/settingsfile_2.13-4.0.0.pom"
        "org.scala-sbt/interface/0.13.18/ivys/ivy.xml"
        "org.scala-sbt/interface/0.13.18/jars/interface.jar"
      ];

  # Each Scala version of a projectMatrix project is a package of its own,
  # holding that version's module only, and its repository alone runs it.
  matrix-app-artifacts =
    onlyOwnArtifacts "matrix-app-artifacts" matrix.packages.app "example/app_2.13"
      { };
  matrix-app3-artifacts =
    onlyOwnArtifacts "matrix-app3-artifacts" matrix.packages.app3 "example/app_3"
      { };
  matrix-app-repository =
    runFromRepository "matrix-app-repository" matrix.sbtRepositories.app "example:app_2.13:5.0.0"
      "matrixexample.Main"
      [ "matrix app for Scala 2.13" ];
  matrix-app3-repository =
    runFromRepository "matrix-app3-repository" matrix.sbtRepositories.app3 "example:app_3:5.0.0"
      "matrixexample.Main"
      [ "matrix app for Scala 3" ];
  matrix2-app-artifacts =
    onlyOwnArtifacts "matrix2-app-artifacts" matrix2.packages.app "example/app_3"
      { };
  matrix2-app2_13-artifacts =
    onlyOwnArtifacts "matrix2-app2_13-artifacts" matrix2.packages.app2_13 "example/app_2.13"
      { };
  matrix2-app-repository =
    runFromRepository "matrix2-app-repository" matrix2.sbtRepositories.app "example:app_3:6.0.0"
      "matrixexample.Main"
      [ "matrix app for Scala 3" ];
  matrix2-app2_13-repository =
    runFromRepository "matrix2-app2_13-repository" matrix2.sbtRepositories.app2_13
      "example:app_2.13:6.0.0"
      "matrixexample.Main"
      [ "matrix app for Scala 2.13" ];

  # A project published Ivy-style for Scala 2.13 and Scala 3 has a module
  # for each in its output and in its repository, and each resolves and
  # runs from the repository alone.
  ivy-crosslib-artifacts = repositoryLayouts "ivy-crosslib-artifacts" ivy.packages.crosslib [
    "example/crosslib_2.13/4.0.0/ivys/ivy.xml"
    "example/crosslib_2.13/4.0.0/jars/crosslib_2.13.jar"
    "example/crosslib_3/4.0.0/ivys/ivy.xml"
    "example/crosslib_3/4.0.0/jars/crosslib_3.jar"
  ];
  ivy-crosslib-2_13-repository =
    runFromRepository "ivy-crosslib-2_13-repository" ivy.sbtRepositories.crosslib
      "example:crosslib_2.13:4.0.0"
      "ivyexample.CrossLib"
      [ "crosslib published Ivy-style" ];
  ivy-crosslib-3-repository =
    runFromRepository "ivy-crosslib-3-repository" ivy.sbtRepositories.crosslib
      "example:crosslib_3:4.0.0"
      "ivyexample.CrossLib"
      [ "crosslib published Ivy-style" ];

  # A Scala 2.12 project that resolves with Ivy builds in the sandbox: Ivy
  # resolves the compiler bridge sources from the locked downloads too.
  ivy-legacy-artifacts =
    onlyOwnArtifacts "ivy-legacy-artifacts" ivy.packages.legacy "example/legacy_2.12"
      { };

  # A project published Ivy-style has its output in the standard Ivy
  # layout.
  ivy-lib-artifacts = onlyOwnArtifacts "ivy-lib-artifacts" ivy.packages.lib "example/lib_2.13" {
    files = [
      "4.0.0/ivys/ivy.xml"
      "4.0.0/jars/lib_2.13.jar"
    ];
  };

  # An sbt plugin published Ivy-style keeps its name and carries its Scala
  # and sbt versions as extra attributes, which are part of its path; its
  # repository resolves it by them.
  ivy-plugin-artifacts =
    onlyOwnArtifacts "ivy-plugin-artifacts" ivy.packages.ivyplugin "example/ivyplugin"
      {
        files = [
          "scala_2.12/sbt_1.0/4.0.0/ivys/ivy.xml"
          "scala_2.12/sbt_1.0/4.0.0/jars/ivyplugin.jar"
        ];
      };
  ivy-plugin-repository =
    resolveFromRepository "ivy-plugin-repository" ivy.sbtRepositories.ivyplugin
      ''Defaults.sbtPluginExtra("example" % "ivyplugin" % "4.0.0", "1.0", "2.12")''
      ''
        tr ':' '\n' <<<"$cp" | grep -qx '${ivy.sbtRepositories.ivyplugin}/example/ivyplugin/scala_2.12/sbt_1.0/4.0.0/jars/ivyplugin.jar' ||
          { echo "the plugin jar is not on the classpath: $cp" >&2; exit 1; }
      '';

  # Builds defined outside their sbt projects know every artifact before
  # they build: Maven-style on sbt 1 (multi), Ivy-style with Ivy-style
  # plugins and a library for two Scala versions (ivy), sbt 2 with both
  # styles (sbt2), and a projectMatrix build (matrix2).
  externalized-multi = externalizedBuild "externalized-multi" multi {
    core = [ "scala3" ];
    javalib = [ "default" ];
  };
  externalized-ivy = externalizedBuild "externalized-ivy" ivy {
    crosslib = [
      "scala2_13"
      "scala3"
    ];
    ivyjava = [ "default" ];
    ivyplugin = [ "scala2_12_sbt1_0" ];
  };
  externalized-sbt2 = externalizedBuild "externalized-sbt2" sbt2 {
    lib = [ "scala3" ];
    ivyplugin2 = [ "scala3_sbt2" ];
  };
  externalized-matrix2 = externalizedBuild "externalized-matrix2" matrix2 {
    core = [ "scala3" ];
    core2_13 = [ "scala2_13" ];
  };

  # The checks of the purifier contract (CONTRACT.md of the purifier
  # library) that the sandbox can run; conformance-relock,
  # conformance-update and conformance-offline need the network or the Nix
  # daemon, and run as CI jobs of those names.
  conformance-stale = conformance "conformance-stale" (
    problemOf "is stale" {
      src = ./fixtures/multi;
      lockFile = ./fixtures/sbt2/sbt-purifier.lock.json;
    }
    && evaluationFails {
      src = ./fixtures/multi;
      lockFile = ./fixtures/sbt2/sbt-purifier.lock.json;
    }
  );
  conformance-tampered = conformance "conformance-tampered" (
    problemOf "does not match its content hash" {
      src = ./fixtures/multi;
      lockFile = editedLock;
    }
    && evaluationFails {
      src = ./fixtures/multi;
      lockFile = editedLock;
    }
  );
  conformance-malformed = conformance "conformance-malformed" (
    builtins.all
      (
        text:
        let
          lockFile = builtins.toFile "sbt-purifier.lock.json" text;
        in
        problemOf "is not a lockfile that sbt-purifier wrote" {
          src = ./fixtures/multi;
          inherit lockFile;
        }
        && evaluationFails {
          src = ./fixtures/multi;
          inherit lockFile;
        }
      )
      [
        "{ not json"
        (builtins.substring 0 40 multiLockText)
      ]
  );
  # A lockfile without the downloads of upickle, which app needs, and with
  # its content hash recomputed: the Nix build of app fails and names the
  # command that fixes it.
  conformance-missing =
    pkgs.runCommand "conformance-missing"
      {
        failure =
          pkgs.testers.testBuildFailure
            (buildSbtProject {
              src = ./fixtures/multi;
              lockFile = lockWithout (path: lib.hasInfix "/upickle_3/" path);
            }).packages.app;
      }
      ''
        grep -q 'run `sbt-purifier lock`' "$failure/testBuildFailure.log" ||
          { echo "the failed build does not name the command that fixes it" >&2; cat "$failure/testBuildFailure.log" >&2; exit 1; }
        touch $out
      '';
  # A second build of app, as a derivation of its own, gives the same bytes.
  conformance-rebuild =
    let
      again = multi.packages.app.overrideAttrs { SBT_PURIFIER_REBUILD = "1"; };
    in
    pkgs.runCommand "conformance-rebuild" { } ''
      diff -r ${multi.packages.app} ${again}
      diff -r ${multi.packages.app.repository} ${again.repository}
      touch $out
    '';
  # A build kept external: its source holds no lockfile, and the lockfile
  # comes from outside it.
  conformance-external =
    let
      external = buildSbtProject {
        src = builtins.path {
          name = "multi-without-lockfile";
          path = ./fixtures/multi;
          filter = path: _: baseNameOf path != "sbt-purifier.lock.json";
        };
        lockFile = ./fixtures/multi/sbt-purifier.lock.json;
      };
    in
    pkgs.runCommand "conformance-external" { } ''
      test -f ${external.packages.app}/nix-support/artifacts/scala3/app.jar
      ln -s ${external.packages.app} $out
    '';

  legacy-build = legacy.packages.sbt013;
  legacy-ivy-build = legacy.packages.ivy;
  legacy-ivy-repository =
    runFromRepository "legacy-ivy-repository" legacy.sbtRepositories.ivy "example.legacy:ivy_2.10:1.0.0"
      "example.IvyMain"
      [ "Legacy Ivy" ];
  legacy-repository =
    runFromRepository "legacy-repository" legacy.sbtRepositories.sbt013
      "example.legacy:legacy-example_2.10:1.0.0"
      "example.Main"
      [ "Hello, Nix!" ];

  # The plugin repository holds the plugin for sbt 0.13, 1 and 2, each
  # with its API documentation.
  plugin-variants = pkgs.runCommand "plugin-variants" { nativeBuildInputs = [ multi.jdk ]; } ''
    for variant in sbt-nix-backend_2.10_0.13 sbt-nix-backend_2.12_1.0 sbt-nix-backend_sbt2_3; do
      ls ${plugin-repository}/io/github/clhodapp/$variant/*/*.jar
      for docs in ${plugin-repository}/io/github/clhodapp/$variant/*/*-javadoc.jar; do
        pages=$(jar tf "$docs" | grep -c 'NixBackendPlugin.*\.html$' || true)
        if [ "$pages" -eq 0 ]; then
          echo "the $variant documentation jar has no page for NixBackendPlugin" >&2
          exit 1
        fi
      done
    done
    touch $out
  '';
  source-version = import ./source-version.nix { inherit pkgs; };
  composition-producer = import ./composition-producer.nix {
    inherit pkgs;
    purifier = flakeLib.purifier;
  };

}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  ivy-native-state = import ./ivy-state.nix {
    inherit pkgs;
    sdk = multi;
  };
  realize-mutable-metadata = import ./realize-mutable-metadata.nix { inherit pkgs; };
}
