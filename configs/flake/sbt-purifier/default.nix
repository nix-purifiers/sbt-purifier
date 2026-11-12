# SPDX-License-Identifier: MIT
{ closure-lib, ... }:
{
  config,
  inputs,
  lib,
  ...
}:
{

  imports = [
    inputs.flake-parts.flakeModules.partitions
    closure-lib.caisson.modules.flake."purifier/purifier-packages"
  ];

  # The flake exports as `packages` the derivations of the scope
  # `pkgs.sbt-purifier`, which holds the builder, a function, besides them.
  purifier.scope = "sbt-purifier";

  debug = false;
  caisson = {
    libOverlays.exported = libOverlays: {
      inherit (libOverlays) default;
    };
  };

  # `nix run github:nix-purifiers/sbt-purifier -- lock` runs the
  # sbt-purifier commands, which run sbt with the plugin injected, which is
  # how a build without the plugin in its project/plugins.sbt locks and
  # exports itself.
  #
  # `nix develop github:nix-purifiers/sbt-purifier` is the other way in: plain
  # nixpkgs sbt, with SBT_NIX_BACKEND_REPOSITORY naming the plugin
  # repository, for a build that installs the plugin in its
  # project/project/plugins.sbt (see the README). It sets JAVA_HOME to
  # nixpkgs' default JDK, the Java major this repository's lockfiles
  # record, so a relock from it records the same major whatever JAVA_HOME
  # the caller has, as a CI runner sets one of its own.
  perSystem =
    { config, pkgs, ... }:
    {
      packages.default = config.packages.sbt-purifier;
      devShells = {
        default = pkgs.mkShell {
          packages = [ pkgs.sbt ];
          JAVA_HOME = pkgs.jdk.home;
          SBT_NIX_BACKEND_REPOSITORY = config.packages.plugin-repository;
          SBT_NIX_BACKEND_PLUGIN_VERSION = config.packages.plugin.version;
        };
      }
      // lib.optionalAttrs pkgs.stdenv.isLinux {
        # Explicit fixture profile: sbt 0.13 and Scala 2.10 need an older JVM.
        legacy-tests = pkgs.mkShell {
          JAVA_HOME = pkgs.jdk8_headless.home;
        };
      };
    };

  partitionedAttrs.checks = "checks";
  partitionedAttrs.formatter = "formatter";

  partitions.formatter = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      { inputs, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem.treefmt.programs.nixfmt.enable = true;
      };
  };

  partitions.checks = {
    extraInputs = (lib.caisson.pins.flake-compat ../../../tests/dependencies).sources;
    module =
      { inputs, self, ... }:
      {
        imports = [ inputs.treefmt-nix.flakeModule ];
        perSystem =
          { pkgs, system, ... }:
          {
            checks = import ../../../tests {
              inherit pkgs;
              overlay = self.overlays.default;
              flakeLib = self.lib;
              inherit (self.packages.${system}) plugin plugin-repository;
            };
            treefmt.programs.nixfmt.enable = true;
          };
      };
  };

}
