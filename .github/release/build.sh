#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# build.sh <version> <dir>: builds the plugin's published artifacts in the
# Nix sandbox with <version> as their version, and copies them into <dir>
# in Maven layout, with their checksum files and a SHA256SUMS of the
# artifacts. Run it from the root of a checkout; it builds the files git
# tracks there, as the flake does.
#
# The version reaches sbt as SBT_NIX_BACKEND_RELEASE_VERSION, set on the
# plugin's derivation; the lockfile is the same for every version.
set -euo pipefail

version=$1
dir=$2
root=$(pwd)
system=$(nix eval --impure --raw --expr builtins.currentSystem)

out=$(nix build --no-link --print-out-paths --impure --expr "
  ((builtins.getFlake \"git+file://$root\").packages.$system.plugin).overrideAttrs {
    version = \"$version\";
  }")

mkdir -p "$dir"
cp -r --no-preserve=mode "$out/." "$dir/"
# The links the Nix library finds the artifacts by are no part of the
# Maven repository.
rm -rf "$dir/nix-support"
sums=$(cd "$dir" && find . -type f -not -name '*.sha1' -not -name '*.md5' -not -name SHA256SUMS | sort | xargs sha256sum)
printf '%s\n' "$sums" >"$dir/SHA256SUMS"
echo "built $version into $dir"
