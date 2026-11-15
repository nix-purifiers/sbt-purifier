# SPDX-License-Identifier: MIT
#
# Unit tests of the public Nix library and package helpers on hand-written lockfiles. The
# fetcher is replaced by one that writes each URL into a file, so nothing
# downloads. The check fails with the list of failed tests.
{
  pkgs,
  # The flake's default package overlay, which adds buildSbtProject.
  overlay,
  # The library of the flake, with lib.sbt-purifier and lib.purifier.
  flakeLib,
}:
let
  inherit (pkgs) lib;
  inherit (flakeLib.sbt-purifier) readLock;

  # The lock that the tests build from, without its version.
  lockFields = {
    sbtVersion = "1.11.7";
    javaVersion = "17";
    launcher = "https/repo.example/sbt-launch-1.11.7.jar";
    javaOptions = [ "-Xmx1g" ];
    plugin = null;
    projects = [
      {
        id = "core";
        organization = "example";
        name = "core_3";
        variants = [
          {
            name = "scala3";
            scalaVersion = "3.3.6";
            artifacts = [
              {
                key = "core.jar";
                name = "core";
                type = "jar";
                extension = "jar";
                classifier = null;
                configurations = [ "compile" ];
                extraAttributes = { };
              }
              {
                key = "core-sources.jar";
                name = "core";
                type = "src";
                extension = "jar";
                classifier = "sources";
                configurations = [ "compile" ];
                extraAttributes = { };
              }
            ];
          }
        ];
      }
    ];
    flakeRepositories = [ ];
    inputsHash = "inputs";
    definitionHash = import ../lib-overlays/default/definition-hash.nix sourceTree;
    artifacts = [
      {
        path = "https/repo.example/a/b/b-1.0.jar";
        url = "https://repo.example/a/b/b-1.0.jar";
        mirrors = [ ];
        hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
      }
      {
        path = "https/host%3A8080/c/.directory";
        url = "https://host:8080/c/";
        mirrors = [ ];
        hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
      }
    ];
  };

  # The text of a lockfile as the plugin writes it (Lock.withContentHash):
  # the version line, then the content hash of the text without that line,
  # then the rest. Here the rest is compact JSON, which parses the same.
  lockText =
    overrides:
    let
      version = toString (overrides.version or 3);
      rest =
        let
          json = builtins.toJSON (lockFields // builtins.removeAttrs overrides [ "version" ]);
        in
        builtins.substring 1 (builtins.stringLength json) json;
      head = "{\n  \"version\": ${version},\n";
    in
    "${head}  \"contentHash\": \"${builtins.hashString "sha256" (head + rest)}\",\n${rest}";

  lockJson = overrides: builtins.toFile "sbt-purifier.lock.json" (lockText overrides);

  # The fetcher of the purifier library takes nixpkgs' fetchurl arguments.
  fakeFetchurl =
    {
      name,
      url ? builtins.head urls,
      urls ? [ ],
      ...
    }:
    pkgs.writeText name url;

  # An sbt build root with a build.sbt, for the stale check, and the same
  # tree as a derivation output, whose staleness readLock leaves to the
  # plugin.
  sourceTree = builtins.path {
    name = "build";
    path = ./nix-library-build;
  };
  src = pkgs.runCommand "empty-build" { } "mkdir $out";

  build = buildWith pkgs;

  buildWith =
    pkgs': args:
    (pkgs'.extend overlay).sbt-purifier.buildSbtProject (
      {
        inherit src;
        fetchers.fetchurl = fakeFetchurl;
      }
      // args
    );

  pluginLock =
    via:
    lockJson {
      plugin = {
        organization = "io.github.clhodapp";
        name = "sbt-nix-backend";
        version = "0.1.0";
        inherit via;
      };
    };

  markerJdk = pkgs.runCommand "marker-jdk" { } "mkdir $out";

  # A package set whose JDK 17 is the marker.
  withMarkerJdk = pkgs.extend (
    final: prev: {
      javaPackages = prev.javaPackages // {
        compiler = prev.javaPackages.compiler // {
          openjdk17 = prev.javaPackages.compiler.openjdk17 // {
            headless = markerJdk;
          };
        };
      };
    }
  );

  nameOf =
    fileName: (import ../pkgs/plugin/nix/fetch.nix { }).nameOf { path = "https/h/${fileName}"; };

  problemsOf = args: (readLock args).problems;

  hasProblem =
    infix: args:
    let
      problems = problemsOf args;
    in
    builtins.length problems == 1
    && lib.hasInfix infix (builtins.head problems)
    && lib.hasInfix "run `sbt-purifier lock`" (builtins.head problems);

  failsToEvaluate = value: !(builtins.tryEval (builtins.deepSeq value value)).success;

  tests = {
    testStoreNameKeepsValidNames = {
      expr = nameOf "scala-library-2.13.16.jar";
      expected = "scala-library-2.13.16.jar";
    };
    testStoreNameReplacesInvalidCharacters = {
      expr = nameOf "a b%c~d.jar";
      expected = "a_b_c_d.jar";
    };
    testStoreNameDropsLeadingDots = {
      expr = nameOf ".directory";
      expected = "directory";
    };
    testStoreNameIsNeverEmpty = {
      expr = nameOf "...";
      expected = "file";
    };
    testStoreNameIsAtMost200Characters = {
      expr = builtins.stringLength (nameOf (lib.concatStrings (lib.replicate 300 "x")));
      expected = 200;
    };

    # The lock in memory.
    testAUsableLockHasNoProblems = {
      expr = problemsOf {
        src = sourceTree;
        lockFile = lockJson { };
      };
      expected = [ ];
    };
    testTheLockPassesCheckLock = {
      expr =
        (flakeLib.purifier.checkLock (readLock {
          src = sourceTree;
          lockFile = lockJson { };
        })).purifier;
      expected = "sbt-purifier";
    };
    testTheEntriesAreTheDownloads = {
      expr =
        map (e: e.path)
          (readLock {
            src = sourceTree;
            lockFile = lockJson { };
          }).entries;
      expected = [
        "https/repo.example/a/b/b-1.0.jar"
        "https/host%3A8080/c/.directory"
      ];
    };
    testTheOutputsAreTheProjects = {
      expr =
        builtins.attrNames
          (readLock {
            src = sourceTree;
            lockFile = lockJson { };
          }).outputs;
      expected = [ "core" ];
    };
    testAMissingLockfileIsAProblem = {
      expr = hasProblem "does not exist" { src = sourceTree; };
      expected = true;
    };
    testAnEditedLockfileIsAProblem = {
      expr = hasProblem "does not match its content hash" {
        src = sourceTree;
        lockFile = builtins.toFile "sbt-purifier.lock.json" (
          builtins.replaceStrings [ "1.11.7" ] [ "1.11.8" ] (lockText { })
        );
      };
      expected = true;
    };
    testAMalformedLockfileIsAProblem = {
      expr = hasProblem "is not a lockfile that sbt-purifier wrote" {
        src = sourceTree;
        lockFile = builtins.toFile "sbt-purifier.lock.json" "not json";
      };
      expected = true;
    };
    testALockfileOfAnotherFormatIsAProblem = {
      expr = hasProblem "lock format version 2" {
        src = sourceTree;
        lockFile = lockJson { version = 2; };
      };
      expected = true;
    };
    testAStaleLockfileIsAProblem = {
      expr = hasProblem "is stale" {
        src = sourceTree;
        lockFile = lockJson { definitionHash = "0"; };
      };
      expected = true;
    };
    testTheBuilderFailsOnAProblem = {
      expr = failsToEvaluate (build { lockFile = lockJson { version = 2; }; }).packages;
      expected = true;
    };

    # The builder.
    testSourceDerivationRemainsABuildInput = {
      expr = (build { lockFile = lockJson { }; }).packages.core.src.drvPath;
      expected = src.drvPath;
    };
    testSourceOutputStringRemainsABuildInput = {
      expr =
        (build {
          src = "${src}";
          lockFile = lockJson { };
        }).packages.core.src;
      expected = "${src}";
    };
    testSourceFilterRejectsADeferredSource = {
      expr =
        failsToEvaluate
          (build {
            lockFile = lockJson { };
            srcFilter = _: _: true;
          }).packages.core.drvPath;
      expected = true;
    };
    testPackagesAreNamedByProjectId = {
      expr = builtins.attrNames (build { lockFile = lockJson { }; }).packages;
      expected = [ "core" ];
    };
    testPackageHasRepositoryOutput = {
      expr = (build { lockFile = lockJson { }; }).packages.core.outputs;
      expected = [
        "out"
        "repository"
        "publications"
      ];
    };
    testSbtRepositoriesAreTheRepositoryOutputs = {
      expr =
        let
          b = build { lockFile = lockJson { }; };
        in
        b.sbtRepositories.core.outPath == b.packages.core.repository.outPath;
      expected = true;
    };
    testTheFetchesAreTheEntriesOfTheLock = {
      expr = map (e: e.url) (build { lockFile = lockJson { }; }).fetches;
      expected = [
        "https://repo.example/a/b/b-1.0.jar"
        "https://host:8080/c/"
      ];
    };
    testTheResultHasTheContractAttributes = {
      expr = builtins.all (n: (build { lockFile = lockJson { }; }) ? ${n}) [
        "packages"
        "tests"
        "release"
        "fetches"
      ];
      expected = true;
    };
    # A lockfile outside the source, of a build kept external, takes the
    # place of the one in it; the source here has none.
    testALockfileOutsideTheSourceIsCopiedIn = {
      expr =
        let
          phase = (build { lockFile = lockJson { }; }).packages.core.buildPhase;
        in
        builtins.elem (toString (lockJson { })) (builtins.attrNames (builtins.getContext phase))
        && lib.hasInfix " sbt-purifier.lock.json" phase;
      expected = true;
    };
    testDefaultJdkFollowsTheLockedJavaMajorVersion = {
      expr = (build { lockFile = lockJson { }; }).jdk.outPath;
      expected = pkgs.jdk17_headless.outPath;
    };
    testTheJdkComesFromThePackageSet = {
      expr = (buildWith withMarkerJdk { lockFile = lockJson { }; }).jdk.outPath;
      expected = markerJdk.outPath;
    };
    testJdkArgumentWins = {
      expr =
        (build {
          lockFile = lockJson { };
          jdk = pkgs.jdk21_headless;
        }).jdk.outPath;
      expected = pkgs.jdk21_headless.outPath;
    };
    testFlakeRepositoryOverrideAvoidsGetFlake = {
      # The locked reference does not exist; evaluating the package must
      # not fetch it when an override is given.
      expr =
        lib.hasPrefix "/nix/store/"
          (build {
            lockFile = lockJson {
              flakeRepositories = [
                {
                  name = "other";
                  ref = "github:nobody/nothing#x";
                  locked = "github:nobody/nothing/0000000000000000000000000000000000000000";
                  attribute = "x";
                }
              ];
            };
            flakeRepositories.other = src;
          }).packages.core.drvPath;
      expected = true;
    };
    # A lock made with the wrapper gets the global plugin and the variable,
    # which a build that installs the plugin in its own
    # project/project/plugins.sbt resolves it through.
    testGlobalPluginGoesInAsAGlobalPlugin = {
      expr =
        let
          phase = (buildWithPlugin "global-plugin").packages.core.buildPhase;
        in
        lib.hasInfix "-Dsbt.global.plugins=" phase
        && lib.hasInfix "export SBT_NIX_BACKEND_REPOSITORY=/nix/store/" phase;
      expected = true;
    };
    testEnvironmentPluginIsNamedByTheVariable = {
      expr =
        let
          phase = (buildWithPlugin "environment").packages.core.buildPhase;
        in
        lib.hasInfix "export SBT_NIX_BACKEND_REPOSITORY=/nix/store/" phase
        && !(lib.hasInfix "-Dsbt.global.plugins=" phase);
      expected = true;
    };
    testPluginRepositoryVersionIsChecked = {
      expr = lib.hasInfix "does not hold io.github.clhodapp:sbt-nix-backend:0.1.0" (buildWithPlugin "environment")
      .packages.core.buildPhase;
      expected = true;
    };
    # The default plugin repository is the plugin built from this revision
    # for the same package set.
    testThePluginOfThisRevisionIsTheDefault = {
      expr = builtins.elem (pkgs.extend overlay).sbt-purifier.plugin.drvPath (
        builtins.attrNames (
          builtins.getContext (build { lockFile = pluginLock "environment"; }).packages.core.buildPhase
        )
      );
      expected = true;
    };
    # The lock names the artifacts of a package, so evaluating them builds
    # nothing: the empty source tree here builds no sbt project at all.
    testTheArtifactsOfAPackageAreKnownBeforeItIsBuilt = {
      expr =
        lib.mapAttrs (_: builtins.attrNames)
          (build { lockFile = lockJson { }; }).packages.core.artifacts;
      expected = {
        scala3 = [
          "core-sources.jar"
          "core.jar"
        ];
      };
    };
    testAnArtifactIsLinkedByVariantAndKey = {
      expr =
        let
          core = (build { lockFile = lockJson { }; }).packages.core;
        in
        core.artifacts.scala3."core-sources.jar" == "${core}/nix-support/artifacts/scala3/core-sources.jar";
      expected = true;
    };
    testTheVariantsKeepWhatTheLockRecords = {
      expr = map (v: {
        inherit (v) name scalaVersion;
        classifiers = map (a: a.classifier) v.artifacts;
        descriptor = lib.removePrefix "${(build { lockFile = lockJson { }; }).packages.core}/" v.descriptorFile;
      }) (build { lockFile = lockJson { }; }).packages.core.variants;
      expected = [
        {
          name = "scala3";
          scalaVersion = "3.3.6";
          classifiers = [
            null
            "sources"
          ];
          descriptor = "nix-support/artifacts/scala3/descriptor";
        }
      ];
    };
    # The derivation of a package is named after the project, without its
    # version, so a version bump renames nothing.
    testAPackageIsNamedAfterItsProject = {
      expr = (build { lockFile = lockJson { }; }).packages.core.name;
      expected = "core_3";
    };
    testUnknownPluginRouteFails = {
      expr = failsToEvaluate (buildWithPlugin "telepathy").packages.core.drvPath;
      expected = true;
    };
  };

  buildWithPlugin =
    via:
    build {
      lockFile = pluginLock via;
      pluginRepository = src;
    };

  failures = lib.runTests tests;

  # The coursier cache of the hand-written lock: each artifact at its
  # cache path, holding (through the fake fetcher) its URL.
  dependencies = (build { lockFile = lockJson { }; }).dependencies;
in
pkgs.runCommand "nix-library-tests"
  {
    passAsFile = [ "failures" ];
    failures = builtins.toJSON failures;
  }
  ''
    if [ "$(cat "$failuresPath")" != "[]" ]; then
      echo "failed tests:" >&2
      cat "$failuresPath" >&2
      exit 1
    fi
    test -L ${dependencies}/https/repo.example/a/b/b-1.0.jar
    test "$(cat ${dependencies}/https/repo.example/a/b/b-1.0.jar)" = https://repo.example/a/b/b-1.0.jar
    test "$(cat '${dependencies}/https/host%3A8080/c/.directory')" = https://host:8080/c/
    touch $out
  ''
