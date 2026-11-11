# SPDX-License-Identifier: MIT
#
# The default package overlay. It adds one attribute, the scope
# `pkgs.sbt-purifier` made with lib.makeScope, which holds the packages of
# pkgs/: the builder `buildSbtProject`, `plugin`, `plugin-repository`,
# `sbt` and the commands `sbt-purifier`. A package written for callPackage
# takes the scope by name. A consumer that lists this flake in `projects`
# holds the overlay as `sbt-purifier/default`, which its package sets apply
# by default.
{ closure-lib, ... }:
{
  overlay = closure-lib.caisson.nixpkgs.mkPackagesOverlay (
    args@{ callPackage, lib, ... }:
    import ../../pkgs (
      args
      // {
        implementationSource = closure-lib.caisson.libManifest.root // {
          dirty =
            (closure-lib.caisson.libManifest.root.dirty or false)
            || builtins.any (input: input.pin.overridden or false) (
              builtins.attrValues closure-lib.caisson.libManifest.sources
            );
        };
      }
    )
  ) "sbt-purifier";
}
