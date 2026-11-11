# SPDX-License-Identifier: MIT
#
# The packages of the `sbt-purifier` scope. `lib` is the composed library,
# with `lib.purifier` and `lib.sbt-purifier`.
{
  callPackage,
  lib,
  implementationSource,
  implementationNixpkgs,
  implementationLib,
  ...
}:
let
  pkgs = callPackage ({ pkgs }: pkgs) { };
  canonicalPkgs = import implementationNixpkgs { system = pkgs.stdenv.hostPlatform.system; };
  implementation = canonicalPkgs.callPackage implementationLib.purifier.implementation {
    source = implementationSource;
    repository = "nix-purifiers/sbt-purifier";
    tool = "sbt-purifier";
    nativeCommand = "sbt";
    checksum = "sbt";
    lockVersions = [ 3 ];
  };
  # Scope overrides happen before the shared outer override guard. Compare
  # the actual constructor recipe with the source-owned default before it
  # can execute or advertise a portable identity. Reject unknown changes;
  # relabeling just the CLI cannot also rebind its delegated native wrapper.
  wrapDefault =
    file: actualArgs: defaultArgs:
    let
      candidate = callPackage file (actualArgs // { inherit implementation; });
      expected = canonicalPkgs.callPackage file (defaultArgs // { inherit implementation; });
    in
    if candidate.drvPath == expected.drvPath then
      implementation.wrap candidate
    else
      throw "sbt-purifier: this scope changes the source-owned wrapper recipe; use the source exported packages for implementation pins";
  pluginVersion = builtins.replaceStrings [ "\n" ] [ "" ] (builtins.readFile ../version.txt);
  canonicalBuilder = canonicalPkgs.callPackage ../nix/lib/build-sbt-project.nix {
    inherit implementation;
    inherit (implementationLib) purifier;
    inherit (implementationLib.sbt-purifier) readLock;
  };
  canonicalPlugin = canonicalPkgs.callPackage ../nix/lib/plugin.nix {
    lib = implementationLib;
    buildSbtProject = canonicalBuilder;
  };
  canonicalSbt = implementation.wrap (
    canonicalPkgs.callPackage ./sbt {
      inherit implementation pluginVersion;
      pluginRepository = canonicalPlugin.repository;
    }
  );
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
  sbt =
    wrapDefault ./sbt
      {
        inherit pluginVersion;
        pluginRepository = plugin.repository;
      }
      {
        inherit pluginVersion;
        pluginRepository = canonicalPlugin.repository;
      };
}
// {
  # The commands of the purifier contract: lock, update, prefetch, publish.
  sbt-purifier = wrapDefault ./sbt-purifier { } { sbt = canonicalSbt; };
}
