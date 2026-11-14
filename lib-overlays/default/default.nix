# SPDX-License-Identifier: MIT
#
# The `sbt-purifier` library namespace: `readLock { src, lockFile ? ... }`,
# the lock in memory of the purifier contract (CONTRACT.md of the purifier
# library), which tools that read every purifier call. The builder needs a
# package set, so it is the package `buildSbtProject` that the default
# package overlay adds. pkgs/plugin/nix/library.nix composes the same namespace for
# an evaluation outside the flake.
{ ... }:
{

  overlay = final: prev: {
    sbt-purifier = {
      readLock = import ./read-lock.nix;
    };
  };

}
