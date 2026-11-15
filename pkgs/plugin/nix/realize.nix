# What the plugin's interactive mode builds (feature 3).
#
# `fetches` is every locked artifact; `nix-build -A fetches` downloads them
# in parallel. `manifest` is then a store file mapping each artifact's
# coursier-cache path to its store path. The file refers to every artifact,
# so one GC root on it keeps them all alive, and the plugin builds its
# writable cache directory from it.
#
# A file made with builtins.toFile may refer to store paths but not to
# derivation outputs, so the manifest names each artifact through
# builtins.fetchurl: with the same name and hash, it is the same
# fixed-output store path a fetch derivation produces, and it is already
# valid once `fetches` is built, so nothing downloads twice.
{
  lockFile,
  libDir ? null,
}:
let
  # The plugin realizes a lock it has checked; only the downloads count.
  read =
    import
      (if libDir == null then ../../../lib-overlays/default/read-lock.nix else libDir + "/read-lock.nix")
      {
        src = null;
        inherit lockFile;
      };
  fetcher = import (if libDir == null then ./fetch.nix else libDir + "/fetch.nix") { };
  byPath =
    f:
    builtins.listToAttrs (
      map (artifact: {
        name = artifact.path;
        value = f artifact;
      }) read.entries
    );
in
{
  fetches = builtins.attrValues (byPath fetcher.fetch);
  manifest = builtins.toFile "sbt-purifier-coursier-cache.json" (
    builtins.toJSON (
      byPath (
        artifact:
        builtins.fetchurl {
          inherit (artifact) url;
          name = fetcher.nameOf artifact;
          sha256 = artifact.hash;
        }
      )
    )
  );
}
