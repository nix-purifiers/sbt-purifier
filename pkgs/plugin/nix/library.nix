# The library of sbt-purifier for an evaluation outside its flake, such as
# the package.nix that the plugin builds: the nixpkgs library `lib`,
# extended with the purifier library (at the revision this sbt-purifier
# pins) and with `sbt-purifier`, as the flake composes them.
{ lib }:
lib.extend (
  final: prev: {
    purifier = import (import ./purifier-source.nix) { lib = final; };
    sbt-purifier = {
      readLock = import ../../../lib-overlays/default/read-lock.nix;
    };
  }
)
