# buildSbtProject: the sandboxed builds of an sbt build, from the lockfile
# that `sbt-purifier lock` writes. It is the builder of the purifier
# contract (CONTRACT.md of the purifier library).
#
# The file is a package for callPackage: the first argument set is what it
# takes from the package set, and the result is the builder. It lives in
# the `sbt-purifier` scope that the overlay (`overlays.default`) adds to
# the package set, beside the plugin, which passes it the purifier library
# and readLock, so a package written for callPackage takes the scope:
#
#   { sbt-purifier }: (sbt-purifier.buildSbtProject { src = ./.; }).packages.app
#
# `sbt-purifier.buildSbtProject.override` replaces what the builder takes,
# and `sbt-purifier.overrideScope` what the scope shares. The builder
# returns
#
#   packages.<id>     the published artifacts of sbt project <id>, in Maven
#                     or Ivy layout; its `repository` output holds them
#                     together with their runtime dependencies
#   tests             the test run of every package, in the sandbox
#   release           the published artifacts of every package, as one
#                     repository, which `sbt-purifier publish` uploads
#   fetches           the download entries of the lock (purifier library)
#   dependencies      the coursier cache of every locked download
#   sbtRepositories   packages.<id>.repository by id, under the name a
#                     flake exports it as (`sbtRepositories.<system>.<id>`)
#                     for other builds to reference
{
  lib,
  stdenvNoCC,
  runCommand,
  writeText,
  # The packages fetchEntry of the purifier library takes by name, for the
  # downloads of the lock.
  fetchurl,
  unzip,
  zstd,
  python3,
  # The JDKs a build can run on; the default JDK of a build is the headless
  # one of the major version its lock was made with.
  javaPackages,
  # The purifier library (`lib.purifier`), which the scope passes.
  purifier,
  # lib.sbt-purifier.readLock, which the scope passes.
  readLock,
  # The default plugin repository: the scope's plugin with its runtime
  # dependencies. It is null outside the scope; a lock that needs the
  # plugin then needs `pluginRepository`.
  plugin-repository ? null,
  implementation ? null,
}:
{
  # The root of the sbt build.
  src,
  # The lockfile. A lockfile outside `src` takes the place of any
  # lockfile in it.
  lockFile ? src + "/sbt-purifier.lock.json",
  # The fetchers of the purifier library (`fetchEntry`, `tree`), such as a
  # `fetchurl` with credentials or a company mirror.
  fetchers ? { },
  # The JDK that runs sbt.
  jdk ? null,
  # The repository the build takes the sbt-purifier plugin from, when the
  # lock says the Nix build supplies it (`plugin` in the lock): as a global
  # plugin, or through SBT_NIX_BACKEND_REPOSITORY for a build that installs
  # the plugin in its project/project/plugins.sbt. The default is the
  # scope's `plugin-repository`: the plugin built from the sbt-purifier
  # revision whose scope holds this builder.
  pluginRepository ? null,
  # Repository paths that replace flake repositories of the lock, by
  # resolver name. A flake that has the producing flake as an input can
  # pass its output here rather than letting the lock's reference be
  # fetched with builtins.getFlake.
  flakeRepositories ? { },
  # Nix-built projects from any JVM purifier. Their native coordinates and
  # selected publication versions are inferred from repository metadata.
  projectDependencies ? [ ],
  # Runs the build's tests (`test`) in each package build.
  doCheck ? false,
  # Filters the build root before it is copied into the store, as the
  # `filter` of builtins.path. The default leaves out build outputs and
  # editor state. A derivation source is already a prepared tree and is
  # passed through; filter it in the producing derivation instead.
  srcFilter ? null,
}:
let
  # The lock in memory; checkLock fails evaluation with its problems, each
  # naming `sbt-purifier lock`.
  read = purifier.checkLock (readLock {
    inherit src lockFile;
  });
  inherit (read) lock;
  system = stdenvNoCC.hostPlatform.system;

  # Build outputs and editor state never take part in a build.
  defaultSrcFilter =
    path: type:
    let
      name = baseNameOf path;
    in
    !(
      (
        type == "directory"
        && (
          name == "target"
          || name == ".sbt-purifier"
          || name == ".bsp"
          || name == ".metals"
          || name == ".bloop"
        )
      )
      || name == ".git"
      || name == ".idea"
      || lib.hasPrefix "result" name
    );

  filteredSrc =
    if import ./source-readable.nix src then
      builtins.path {
        path = src;
        name = "sbt-source";
        filter = if srcFilter != null then srcFilter else defaultSrcFilter;
      }
    else if srcFilter != null then
      throw "sbt-purifier: srcFilter needs a source readable at evaluation; filter a derivation source in its producing derivation instead."
    else
      src;

  javaMajor = builtins.head (lib.splitString "." lock.javaVersion);
  defaultJdk =
    javaPackages.compiler."openjdk${javaMajor}".headless
      or (throw "sbt-purifier: javaPackages has no openjdk${javaMajor} for the Java ${lock.javaVersion} the lock was made with; pass `jdk`.");
  jdk' = if jdk != null then jdk else defaultJdk;

  # The coursier cache: every download of the lock for this system, at its
  # coursier path.
  dependencies = purifier.tree (
    {
      inherit
        fetchurl
        runCommand
        unzip
        zstd
        python3
        writeText
        ;
      name = "sbt-coursier-cache";
      entries = purifier.entriesFor system read;
    }
    // fetchers
  );

  flakeViews = builtins.listToAttrs (
    map (input: {
      inherit (input) name;
      value = import ./project-repository.nix {
        inherit system;
        inherit (input) locked attribute;
        override = flakeRepositories.${input.name} or null;
      };
    }) lock.flakeRepositories
  );
  flakeRepositoryPaths = builtins.mapAttrs (_: view: view.repository) flakeViews;
  flakeRepositoryCatalogs = builtins.mapAttrs (_: view: view.catalog) (
    lib.filterAttrs (_: view: view.catalog != null) flakeViews
  );
  allProjectDependencies =
    projectDependencies
    ++ builtins.filter (dependency: dependency != null) (
      map (view: view.dependency) (builtins.attrValues flakeViews)
    );

  provided = purifier.jvmProjections {
    repository = null;
    dependencies = allProjectDependencies;
    inherit runCommand python3;
    name = "sbt-provided-dependencies";
  };

  plugin =
    if lock.plugin == null then
      null
    else
      let
        p = lock.plugin;
        repo =
          if pluginRepository != null then
            pluginRepository
          else if plugin-repository != null then
            plugin-repository
          else
            throw "sbt-purifier: the lock takes the plugin (${p.organization}:${p.name}:${p.version}) from a repository the Nix build supplies; apply the sbt-purifier overlay or pass `pluginRepository`.";
        orgPath = lib.replaceStrings [ "." ] [ "/" ] p.organization;
      in
      assert
        lib.elem p.via [
          "global-plugin"
          "environment"
        ]
        || throw "sbt-purifier: the lock reaches the plugin via '${p.via}', which this library does not know";
      {
        inherit (p) via version;
        inherit repo;
        file = writeText "sbt-nix-backend-plugin.sbt" (
          ''
            resolvers += MavenRepository("sbt-nix-backend-plugin", "file://${repo}")
            addSbtPlugin("${p.organization}" % "${p.name}" % "${p.version}")
          ''
          + lib.optionalString legacy (builtins.readFile ./legacy-global-settings.sbt)
        );
        # An sbt plugin publishes as <name>_<scala>_<sbt> in Maven layout.
        # The check runs in the build rather than at evaluation, which
        # would have to build the repository first.
        check = ''
          shopt -s nullglob
          found=(${lib.escapeShellArg "${repo}/${orgPath}/${p.name}_"}*/${lib.escapeShellArg p.version})
          shopt -u nullglob
          if [ ''${#found[@]} -eq 0 ]; then
            echo "sbt-purifier: ${repo} does not hold ${p.organization}:${p.name}:${p.version}, the plugin version the lock was made with." >&2
            echo "Use the sbt-purifier flake revision that matches the lock, or run \`sbt-purifier lock\`." >&2
            exit 1
          fi
        '';
      };

  legacy = lib.hasPrefix "0.13." lock.sbtVersion;

  # The sbt invocation shared by every package build; nixLock runs sbt the
  # same way, so a lock made from it holds what this needs.
  runSbt = command: ''
    # Java 8 follows the locale for source and Javadoc encoding. The Nix
    # sandbox's default C locale is ASCII, unlike ordinary UTF-8 capture.
    # Include forked Java tools through the locale rather than forcing a
    # JVM property over a project's explicitly configured encoding.
    export LANG=${if stdenvNoCC.hostPlatform.isDarwin then "en_US.UTF-8" else "C.UTF-8"}
    export LC_ALL="$LANG"
    export HOME="$TMPDIR/home"
    export XDG_RUNTIME_DIR="$TMPDIR/run"
    export XDG_CACHE_HOME="$HOME/.cache" XDG_CONFIG_HOME="$HOME/.config"
    export XDG_DATA_HOME="$HOME/.local/share" XDG_STATE_HOME="$HOME/.local/state"
    export SBT_NIX_MODE=sandbox
    export SBT_NIX_OUTPUT="$TMPDIR/sbt-nix-output"
    export SBT_NIX_FLAKE_REPOSITORIES=${lib.escapeShellArg (builtins.toJSON flakeRepositoryPaths)}
    export SBT_NIX_FLAKE_REPOSITORY_CATALOGS=${lib.escapeShellArg (builtins.toJSON flakeRepositoryCatalogs)}
    export COURSIER_CACHE="$TMPDIR/coursier-cache"
    export COURSIER_MODE=offline
    ${lib.optionalString (plugin != null) plugin.check}
    ${lib.optionalString (plugin != null && plugin.via == "global-plugin") ''
      # The plugin is the build's only global plugin, which sbt loads below
      # every level of the build (project/project, project/, the build), so
      # the plugin governs the resolution of each. sbt writes target/
      # directories into a global plugins directory, so it is a copy.
      mkdir -p "$TMPDIR/global-plugins"
      cp ${plugin.file} "$TMPDIR/global-plugins/sbt-nix-backend.sbt"
    ''}
    ${lib.optionalString (plugin != null) ''
      # A build that installs the plugin in its project/project/plugins.sbt
      # resolves it from the repository this variable names; the sbt
      # wrapper sets it too, so a lock made with the wrapper and such a
      # build needs it as well as the global plugin.
      export SBT_NIX_BACKEND_REPOSITORY=${plugin.repo}
      export SBT_NIX_BACKEND_PLUGIN_VERSION=${lib.escapeShellArg plugin.version}
    ''}
    ${lib.optionalString (projectDependencies != [ ]) ''
      export SBT_PURIFIER_PROVIDED_REPOSITORY=${provided.maven}
      export SBT_PURIFIER_PROVIDED_CATALOG=${provided.maven.catalog}
    ''}
    mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$TMPDIR/java-tmp" "$COURSIER_CACHE"
    # Coursier and the launcher write lock and marker files next to what
    # they read, so the cache is a writable tree of links into the store.
    cp -rs --no-preserve=mode ${dependencies}/. "$COURSIER_CACHE"
    ${lib.optionalString legacy (''
      cat > "$TMPDIR/launcher-repositories" <<EOF
      ${import ./legacy-boot-repositories.nix { base = "file://$COURSIER_CACHE"; }}
      EOF
    '')}
    java ${lib.escapeShellArgs lock.javaOptions} \
      -Duser.home="$HOME" \
      -Djava.io.tmpdir="$TMPDIR/java-tmp" \
      -Dsbt.global.base="$HOME/.sbt" \
      -Dsbt.boot.directory="$HOME/.sbt/boot" \
      -Dsbt.ivy.home="$HOME/.ivy2" \
      -Dsbt.server.autostart=false \
      -Dsbt.server.forcestart=true \
      -Dsbt.log.noformat=true \
      -Dsbt.supershell=false \
      ${
        lib.optionalString (plugin != null && plugin.via == "global-plugin")
          ''-Dsbt.global.plugins="$TMPDIR/global-plugins" -Dsbt.nix.backend.inject="$TMPDIR/global-plugins/sbt-nix-backend.sbt"''
      } \
      ${lib.optionalString legacy ''-Dsbt.repository.config="$TMPDIR/launcher-repositories"''} \
      -jar "$COURSIER_CACHE/${lock.launcher}" \
      ${lib.escapeShellArg command} </dev/null
  '';

  # The build links each artifact of a variant, and its module descriptor,
  # at names without the version of the project:
  # <linkDirectory>/<variant>/<key> and …/descriptor. The variant is named
  # for the cross version of the publish, such as scala3, scala2_13 or
  # scala2_12_sbt1_0.
  linkDirectory = "nix-support/artifacts";
  artifactFile =
    package: variant: key:
    "${package}/${linkDirectory}/${variant}/${key}";
  linksOf = v: [ "${v.name}/descriptor" ] ++ map (a: "${v.name}/${a.key}") v.artifacts;

  mkPackage =
    check: project:
    stdenvNoCC.mkDerivation (finalAttrs: {
      # The packages of `tests` are the packages themselves when the
      # packages run their tests anyway.
      name = project.name + lib.optionalString (check && !doCheck) "-tests";
      # Forked sbt tests exchange results over loopback, even offline.
      ${if stdenvNoCC.hostPlatform.isDarwin && check then "__darwinAllowLocalNetworking" else null} =
        true;
      src = filteredSrc;
      postUnpack = lib.optionalString (lock ? buildDirectory) (
        assert
          builtins.match "[^/]+" lock.buildDirectory != null
          && !(builtins.elem lock.buildDirectory [
            "."
            ".."
          ]);
        ''
          mkdir -p legacy-source
          mv "$sourceRoot" ${lib.escapeShellArg ("legacy-source/" + lock.buildDirectory)}
          sourceRoot=${lib.escapeShellArg ("legacy-source/" + lock.buildDirectory)}
        ''
      );
      outputs = [
        "out"
        "repository"
        "publications"
      ];
      nativeBuildInputs = [ jdk' ];
      dontConfigure = true;
      # The outputs are jars and POMs; fixup has nothing to patch, and it
      # must not rewrite them.
      dontFixup = true;
      buildPhase = ''
        ${lib.optionalString (stdenvNoCC.hostPlatform.isDarwin && check) ''
          # Fresh forked JVMs must use IPv4 loopback under the Darwin sandbox.
          export JAVA_TOOL_OPTIONS="''${JAVA_TOOL_OPTIONS:+$JAVA_TOOL_OPTIONS }-Djava.net.preferIPv4Stack=true"
        ''}
        runHook preBuild
        ${lib.optionalString (stdenvNoCC.hostPlatform.isDarwin && check) ''
          ${python3}/bin/python3 ${../../tools/local-networking.py}
        ''}
        # The lockfile the build was given, which may lie outside src, takes
        # the place of any lockfile in it.
        cp --no-preserve=mode ${lockFile} sbt-purifier.lock.json
        ${runSbt ("nixSandboxBuild " + lib.optionalString check "--test " + project.id)}
        runHook postBuild
      '';
      installPhase = ''
        runHook preInstall
        cp -r "$SBT_NIX_OUTPUT/${project.id}/artifacts" "$out"
        cp -r "$SBT_NIX_OUTPUT/${project.id}/repository" "$repository"
        # Every project actually built for this runtime closure contributes
        # owned publications; downloaded dependencies retain their provenance.
        mkdir -p "$publications"
        for artifacts in "$SBT_NIX_OUTPUT"/*/artifacts; do
          cp -r --no-preserve=mode "$artifacts"/. "$publications"/
        done
        rm -rf "$publications/nix-support" "$publications/sbt-nix-artifacts.json"
        # The lock names the artifacts; the manifest it took them from
        # stays out of the output.
        rm -f "$out/sbt-nix-artifacts.json"
        for link in ${lib.escapeShellArgs (lib.concatMap linksOf project.variants)}; do
          if [ ! -f "$out/${linkDirectory}/$link" ]; then
            echo "sbt-purifier: the lockfile names $link, which this build of ${project.id} did not publish; run \`sbt-purifier lock\`" >&2
            exit 1
          fi
        done
        runHook postInstall
      '';
      passthru = {
        inherit dependencies lock;
        sbtProject = project;
        projections = purifier.jvmProjections {
          repository = finalAttrs.finalPackage.publications;
          producer = finalAttrs.finalPackage;
          runtimeRepository = finalAttrs.finalPackage.repository;
          formats = [
            "maven"
          ]
          ++ lib.optional (builtins.all (
            variant: builtins.all (artifact: artifact.type != "pom") variant.artifacts
          ) project.variants) "ivy";
          dependencies = allProjectDependencies;
          inherit runCommand python3;
          name = "sbt-jvm-publications";
        };
        # For each variant the lock builds the project in, its name, its
        # Scala version, and its artifacts as the lock records them (key,
        # name, type, extension, classifier, configurations, extra
        # attributes), each with `file`, its file in `out`, and
        # `descriptorFile`, the file of its POM or ivy.xml. They are known
        # from the lock alone, before the package is built.
        variants = map (
          v:
          v
          // {
            artifacts = map (a: a // { file = artifactFile finalAttrs.finalPackage v.name a.key; }) v.artifacts;
            descriptorFile = artifactFile finalAttrs.finalPackage v.name "descriptor";
          }
        ) project.variants;
        # The artifact files of the package by variant and key, such as
        # artifacts.scala3."core-sources.jar".
        artifacts = builtins.listToAttrs (
          map (v: {
            inherit (v) name;
            value = builtins.listToAttrs (
              map (a: {
                name = a.key;
                value = artifactFile finalAttrs.finalPackage v.name a.key;
              }) v.artifacts
            );
          }) project.variants
        );
      };
      meta.description = "${project.organization}:${project.name}, built by sbt-purifier";
    });

  packages = builtins.mapAttrs (_: mkPackage doCheck) read.outputs;
  publicationRepository = runCommand "sbt-release" { } ''
    mkdir $out
    ${lib.concatStrings (lib.mapAttrsToList (_: p: "cp -r --no-preserve=mode ${p}/. $out/\n") packages)}
    rm -rf $out/nix-support
  '';
in
assert
  (lock.implementation or null) == null
  || (
    if implementation == null then
      throw "sbt-purifier: a pinned project requires the implementation-aware overlay"
    else
      implementation.check lock.implementation true
  );
{
  inherit
    lock
    dependencies
    packages
    ;
  fetches = read.entries;
  sbtRepositories = builtins.mapAttrs (_: p: p.repository) packages;
  # Every package built with its tests run.
  tests = runCommand "sbt-tests" { } ''
    mkdir $out
    ${lib.concatStrings (
      lib.mapAttrsToList (id: p: "ln -s ${p} $out/${id}\n") (
        builtins.mapAttrs (_: mkPackage true) read.outputs
      )
    )}
  '';
  # The published artifacts of every package in one repository, as
  # `sbt-purifier publish` uploads them; the links of each package to its
  # own artifacts stay out.
  release = publicationRepository;
  projections = purifier.jvmProjections {
    repository = publicationRepository;
    dependencies = allProjectDependencies;
    inherit runCommand python3;
    name = "sbt-jvm-publications";
  };
  jdk = jdk';
}
