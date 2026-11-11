# SPDX-License-Identifier: MIT
#
# The packages of the `sbt-purifier` scope. `lib` is the composed library,
# with `lib.purifier` and `lib.sbt-purifier`.
{
  callPackage,
  lib,
  implementationSource,
  ...
}:
let
  implementation = callPackage lib.purifier.implementation {
    source = implementationSource;
    repository = "nix-purifiers/sbt-purifier";
    tool = "sbt-purifier";
    nativeCommand = "sbt";
    checksum = "sbt";
    lockVersions = [ 3 ];
  };
  # The builder of the purifier contract (nix/lib/build-sbt-project.nix).
  buildSbtProject = callPackage ../nix/lib/build-sbt-project.nix {
    inherit implementation;
    inherit (lib) purifier;
    inherit (lib.sbt-purifier) readLock;
  };
  # The plugin's published artifacts, in Maven layout (nix/lib/plugin.nix).
  plugin = callPackage ../nix/lib/plugin.nix { };
in
{
  inherit buildSbtProject plugin;
  # The plugin with its runtime dependencies, as a Maven repository: what
  # the `sbt` wrapper and a sandboxed build of an injected lock resolve the
  # plugin from.
  plugin-repository = plugin.repository;
  # sbt with the plugin injected.
  sbt = implementation.wrap (
    callPackage ./sbt {
      inherit implementation;
      pluginRepository = plugin.repository;
      # The version of builds from source, which build.sbt reads too.
      pluginVersion = builtins.replaceStrings [ "\n" ] [ "" ] (builtins.readFile ../version.txt);
    }
  );
}
// {
  # The commands of the purifier contract: lock, update, prefetch, publish.
  sbt-purifier = implementation.wrap (callPackage ./sbt-purifier { inherit implementation; });
}
