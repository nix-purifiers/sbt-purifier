# The coursier cache of a lockfile, every locked file at its coursier path
# as a link into the store, which the sbt wrapper boots sbt from. It is a
# package for callPackage, with the lockfile among its arguments, and
# needs no build source. buildSbtProject builds the same tree with the
# function `tree` of the purifier library.
{
  lib,
  runCommand,
  lockFile,
  # Replaces the built-in fetcher; see fetch.nix.
  fetchurl ? null,
}:
let
  # The wrapper boots from a lock it has checked; only the downloads count.
  read = import ../../../lib-overlays/default/read-lock.nix {
    src = null;
    inherit lockFile;
  };
  inherit (import ./fetch.nix { inherit fetchurl; }) fetch;
in
# The manifest goes to the builder as a file, since a large build locks
# thousands of artifacts and the list would not fit in an environment
# variable.
runCommand "sbt-coursier-cache"
  {
    manifest = lib.concatMapStrings (a: "${a.path}\t${fetch a}\n") read.entries;
    passAsFile = [ "manifest" ];
    preferLocalBuild = true;
    allowSubstitutes = false;
  }
  ''
    mkdir -p $out
    while IFS=$'\t' read -r rel target; do
      mkdir -p "$out/$(dirname "$rel")"
      ln -s "$target" "$out/$rel"
    done < "$manifestPath"
  ''
