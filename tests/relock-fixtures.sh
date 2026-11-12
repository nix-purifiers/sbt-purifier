#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Brings the lockfiles of this repository's own build and of the test
# fixtures up to date.
#
#   tests/relock-fixtures.sh                 relock what is stale (nixLock)
#   tests/relock-fixtures.sh --relock        relock everything (nixRelock)
#   tests/relock-fixtures.sh --skip-consumer leave the consumer fixture
#
# Run it from anywhere, with the network: a relock downloads every artifact
# of a build afresh. The fixtures run with the plugin injected by this
# flake's `sbt` wrapper, the way a build without the plugin in
# project/plugins.sbt uses it.
#
# The consumer fixture refers to the repositories of the multi and ivy
# fixtures through tests/producers, a flake in this repository, on GitHub.
# Its lockfile pins the commit this script runs from, which must be pushed;
# the flake checks pass the repository outputs of this checkout instead.
# Its lockfile depends on its own build only, so a change to the other
# fixtures leaves it valid, and --skip-consumer leaves it as it is (as
# .github/workflows/update.yml does, before it has pushed anything).
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
task=nixLock
consumer=true
for arg in "$@"; do
  case $arg in
    --relock) task=nixRelock ;;
    --skip-consumer) consumer=false ;;
    *)
      echo "relock-fixtures: unknown argument $arg" >&2
      exit 2
      ;;
  esac
done

# The root build compiles the plugin from source in its meta-build.
(cd "$root" && sbt -batch "$task")

sbt_wrapper=$(nix build --no-link --print-out-paths "$root#sbt")

# A meta-build keeps its resolution of the plugin in project/target, and
# sbt 2 keeps one in its disk cache under XDG_CACHE_HOME, while the plugin
# version stays the same, even when the plugin changed. Fresh meta-builds
# and a cache directory of this run's own resolve the current plugin.
for fixture in multi sbt013 sbt2 ivy matrix matrix2 metameta consumer tool-classpath; do
  rm -rf "$root/tests/fixtures/$fixture/project/target" "$root/tests/fixtures/$fixture/project/project/target"
done
XDG_CACHE_HOME=$(mktemp -d)
export XDG_CACHE_HOME
trap 'rm -rf "$XDG_CACHE_HOME"' EXIT

for fixture in multi sbt013 sbt2 ivy matrix matrix2 tool-classpath; do
  if [[ $fixture == sbt013 ]]; then
    legacy_java_home=${SBT_PURIFIER_TEST_LEGACY_JAVA_HOME:-$(nix develop "$root#legacy-tests" --command printenv JAVA_HOME)}
    (cd "$root/tests/fixtures/$fixture" && "$sbt_wrapper/bin/sbt" --server --java-home "$legacy_java_home" -batch "$task")
  else
    (cd "$root/tests/fixtures/$fixture" && "$sbt_wrapper/bin/sbt" --server -batch "$task")
  fi
done

# metameta installs the plugin in its own project/project/plugins.sbt and
# runs under plain sbt, from the flake's dev shell.
(cd "$root/tests/fixtures/metameta" && nix develop "$root" -c sbt --server -batch "$task")

if [[ $consumer == false ]]; then exit 0; fi

# The consumer fixture locks tests/producers at the commit this checkout is
# at, on GitHub.
rev=$(git -C "$root" rev-parse HEAD)
if [[ -z $(git -C "$root" branch -r --contains "$rev" 2>/dev/null) ]]; then
  echo "relock-fixtures: push $rev first; the consumer fixture locks it from GitHub" >&2
  exit 1
fi

(cd "$root/tests/fixtures/consumer" &&
  SBT_NIX_BACKEND_TEST_PRODUCER="github:nix-purifiers/sbt-purifier/$rev?dir=tests/producers" \
    "$sbt_wrapper/bin/sbt" --server -batch "$task")
