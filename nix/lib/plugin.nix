# The sbt-purifier plugin, built by itself from the lockfile at the root of
# this repository: its meta-build compiles the plugin from source, so no
# published copy is needed. Its unit tests run in the build. The package's
# `repository` output holds the plugin with its runtime dependencies as a
# Maven repository, which builds of injected locks resolve the plugin from.
# It is a package for callPackage; the plugin's own lock names no plugin,
# so its build never asks the scope for a plugin repository.
{
  lib,
  buildSbtProject,
}:
let
  root = ../..;
  # Only what the plugin's own build reads: the test fixtures and the flake
  # wiring do not change it.
  parts = [
    "build.sbt"
    "flake.lock"
    "project"
    "src"
    "nix"
    "sbt-purifier.lock.json"
  ];
in
(buildSbtProject {
  src = root;
  doCheck = true;
  srcFilter =
    path: type:
    let
      rel = lib.removePrefix (toString root + "/") (toString path);
      top = builtins.head (lib.splitString "/" rel);
      name = baseNameOf path;
    in
    builtins.elem top parts && !(type == "directory" && name == "target");
}).packages.root.overrideAttrs (finalAttrs: previousAttrs: {
  version = (import ./source-version.nix root).version;
  # A publisher can override `version` explicitly. The sbt build must publish
  # that exact value, rather than recomputing a development version inside it.
  SBT_NIX_BACKEND_RELEASE_VERSION = finalAttrs.version;
})
