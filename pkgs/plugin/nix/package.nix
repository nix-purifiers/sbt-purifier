# What the plugin's nixPackagedArtifacts and nixRuntimeRepository build:
# one project of a build, by buildSbtProject (build-sbt-project.nix, called
# with the nixpkgs package set), from arguments that sbt passes on the
# command line as strings.
#
#   nixpkgs           a nixpkgs source directory, or a flake reference
#   src               the build root
#   project           the sbt project id
#   lockFile          the lockfile, or "" for the one in the build root
#   pluginRepository  the repository to inject the plugin from, or ""
#
# `out` and `repository` are the two outputs of the project's package.
{
  nixpkgs,
  src,
  project,
  # The lockfile, or "" for sbt-purifier.lock.json in src.
  lockFile ? "",
  pluginRepository ? "",
}:
let
  basePkgs =
    if builtins.substring 0 1 nixpkgs == "/" then
      import nixpkgs { }
    else
      (builtins.getFlake nixpkgs).legacyPackages.${builtins.currentSystem};
  lib = import ./library.nix { inherit (basePkgs) lib; };
  locked = lib.purifier.checkLock (
    lib.sbt-purifier.readLock (
      { src = /. + src; } // lib.optionalAttrs (lockFile != "") { lockFile = /. + lockFile; }
    )
  );
  pin =
    let
      value = locked.lock.implementation or null;
    in
    if value == null then
      null
    else if
      (value.version or null) != 1
      || (value.tool or null) != "sbt-purifier"
      ||
        builtins.attrNames value != [
          "source"
          "tool"
          "version"
        ]
    then
      throw "sbt-purifier: unsupported implementation pin"
    else
      value;
  # The embedded library remains usable by legacy Maven-loaded plugins. A
  # pinned project loads the selected source with its revision metadata and
  # uses that source's aware scope, rather than an unidentified store path.
  pkgs =
    if pin == null then
      basePkgs
    else
      basePkgs.extend (lib.purifier.implementationSource.resolve pin.source).flake.overlays.default;
  buildSbtProject =
    if pin != null then
      pkgs.sbt-purifier.buildSbtProject
    else
      pkgs.callPackage ../../build-sbt-project {
        inherit (lib) purifier;
        inherit (lib.sbt-purifier) readLock;
      };
  build = buildSbtProject (
    {
      src = /. + src;
    }
    // lib.optionalAttrs (lockFile != "") { lockFile = /. + lockFile; }
    // lib.optionalAttrs (pluginRepository != "") {
      # A store path passed as a string has no context; storePath gives
      # it one, so the package build may read the repository.
      pluginRepository = builtins.storePath pluginRepository;
    }
  );
  package =
    build.packages.${project}
      or (throw "sbt-purifier: the lockfile builds no project ${project} (it builds ${builtins.concatStringsSep ", " (builtins.attrNames build.packages)})");
in
{
  out = package;
  inherit (package) repository;
  publication = pkgs.runCommand "sbt-projected-publication" { } ''
    mkdir -p "$out"
    ln -s ${package.projections.maven} "$out/maven"
    ${pkgs.lib.optionalString (package.projections ? ivy) ''
      ln -s ${package.projections.ivy} "$out/ivy"
    ''}
    ln -s ${package.projections.maven.catalog} "$out/catalog.json"
  '';
}
