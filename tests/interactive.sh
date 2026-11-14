#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Tests of what runs outside the Nix sandbox, on copies of the multi (sbt 1),
# sbt2 (sbt 2) and ivy (sbt 1, resolving with Ivy) fixtures with their
# committed lockfiles:
#
#  - interactive mode (feature 3): compile, test and run read every
#    dependency from the locked downloads, at every level of the build
#    (the meta-build's sbt-buildinfo included), with sbt's network cut off;
#  - nixPackagedArtifacts: sbt runs the Nix build and returns its files;
#  - a lockfile made from another build definition: sbt warns and still
#    loads, so nixLock can run.
#
# It needs Nix (the daemon downloads the locked artifacts) but gives sbt no
# network: the JVM's proxies point at a closed port. Run it from anywhere.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
sbt_wrapper=$(nix build --no-link --print-out-paths "$root#sbt")
# These fixtures have committed Java 21 locks; choose that test profile
# explicitly, independently of the runner's host Java. The local-runtime
# check separately exercises a caller-provided Java outside the store.
test_java_home=${SBT_PURIFIER_TEST_JAVA_HOME:-$(nix develop "$root" --command printenv JAVA_HOME)}
legacy_java_home=${SBT_PURIFIER_TEST_LEGACY_JAVA_HOME:-$(nix develop "$root#legacy-tests" --command printenv JAVA_HOME)}

# Short, for sbt's boot socket under $work/<fixture>-run (a Unix socket path
# is limited to 104 bytes on macOS, where mktemp is under /var/folders), and
# resolved, as sbt reports the build root (/tmp is /private/tmp on macOS).
work=$(mktemp -d /tmp/sbt-it.XXXXXX)
work=$(cd "$work" && pwd -P)
trap 'chmod -R u+w "$work"; rm -rf "$work"' EXIT

# A suite with the network cut off points the JVM's proxies at a closed
# port; one with the network keeps the proxy settings it was started with.
network_java_options=${JAVA_TOOL_OPTIONS:-}
unset JAVA_TOOL_OPTIONS COURSIER_MODE COURSIER_REPOSITORIES SBT_NIX_BACKEND
no_network="-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort=9 -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=9"
network=off

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}
pass() { echo "ok: $*"; }
# expect <description> <command>...: passes when the command succeeds.
expect() {
  local description=$1
  shift
  if "$@"; then pass "$description"; else fail "$description"; fi
}
# Large text goes through a process substitution, not a here-string: bash
# writes a here-string into a pipe itself, which deadlocks when macOS gives
# the pipe a smaller buffer than the text (under pipe memory pressure).
has() { grep -qE "$1" < <(printf '%s\n' "$output"); }
from_store() { [[ -L $1 && $(readlink "$1") == /nix/store/* ]]; }

# Each fixture runs with a home, caches and runtime directory of its own.
use_fixture() {
  fixture=$1
  build="$work/$fixture"
  if [[ -d $build ]]; then chmod -R u+w "$build"; rm -rf "$build"; fi
  cp -r "$root/tests/fixtures/$fixture" "$build"
  chmod -R u+w "$build"
  rm -rf "$build/target" "$build"/*/target "$build/project/target" "$build/project/project/target"
  export HOME="$work/$fixture-home" XDG_CACHE_HOME="$work/$fixture-cache" XDG_RUNTIME_DIR="$work/$fixture-run"
  export COURSIER_CACHE="$work/$fixture-user-coursier-cache"
  mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
  local proxies=$no_network
  if [[ $network == on ]]; then
    proxies=""
    [[ -n $network_java_options ]] && export JAVA_TOOL_OPTIONS=$network_java_options
  else
    unset JAVA_TOOL_OPTIONS
  fi
  export SBT_OPTS="-Xss4m -Xmx2g -Duser.home=$HOME -Djava.io.tmpdir=$work -Dsbt.global.base=$HOME/.sbt \
    -Dsbt.boot.directory=$HOME/.sbt/boot -Dsbt.ivy.home=$HOME/.ivy2 -Dsbt.server.autostart=false \
    -Dsbt.server.forcestart=true -Dsbt.log.noformat=true -Dsbt.supershell=false $proxies"
}

# The modules of the build and its meta-build: under plain sbt, where sbt
# itself boots through the user's coursier cache, none of these may come
# from there.
locked_only_modules=(upickle munit sbt-buildinfo scala3-library)
# The sbt a suite runs: the sbt-nix-backend wrapper, or plain nixpkgs sbt
# from the flake's dev shell (`plain`), for a build that installs the plugin
# itself.
runner=wrapper
network=off
run_sbt() {
  if [[ $runner == plain ]]; then
    (cd "$build" && nix develop "$root" -c sbt --server -batch "$@") 2>&1 | tee -a "$work/sbt.log"
  else
    local java_home=$test_java_home
    [[ $fixture == sbt013 ]] && java_home=$legacy_java_home
    (cd "$build" && "$sbt_wrapper/bin/sbt" --server --java-home "$java_home" -batch "$@") 2>&1 | tee -a "$work/sbt.log"
  fi
}

# interactive_suite <fixture> <buildinfo jar path in the cache> <jar regex> <run command> <expected line regex>...
interactive_suite() {
  local fixture=$1 buildinfo=$2 jar=$3 run=$4
  shift 4
  use_fixture "$fixture"

  echo "== $fixture: interactive build with the network cut off"
  output=$(run_sbt compile app/test "$run") || {
    local native_exit=$?
    if [[ $fixture == metameta ]]; then
      printf 'metameta: compile app/test %s exited with status %s\n%s\n' \
        "$run" "$native_exit" "$output" >&2
    fi
    fail "$fixture: the offline build failed"
  }
  for line in "$@"; do expect "$fixture: the output has $line" has "$line"; done
  expect "$fixture: the tests run" has 'Passed: Total [1-9]'
  # An editor's build server resolves the sources of every dependency
  # (dependencySources runs updateClassifiers); the lock holds them.
  output=$(run_sbt app/updateClassifiers) || fail "$fixture: the sources of the dependencies did not resolve offline"

  if [[ $network == off ]]; then
    downloads=$(find "$COURSIER_CACHE" -type f -not -name '.*' 2>/dev/null | head -5 || true)
    expect "$fixture: no level downloaded into the user's coursier cache (found: ${downloads:-nothing})" \
      test -z "$downloads"
  else
    for module in "${locked_only_modules[@]}"; do
      found=$(find "$COURSIER_CACHE" -type d -name "$module*" 2>/dev/null | head -1 || true)
      expect "$fixture: $module comes from the locked downloads, not the user's cache (found: ${found:-nothing})" \
        test -z "$found"
    done
  fi
  expect "$fixture: the meta-build's plugin comes from the store" \
    from_store "$build/.sbt-purifier/coursier-cache/$buildinfo"
  expect "$fixture: the locked downloads have a GC root" test -L "$build/.sbt-purifier/gcroots/coursier-cache"

  echo "== $fixture: nixPackagedArtifacts"
  output=$(run_sbt 'show app/nixPackagedArtifacts' 'show app/nixRuntimeRepository') || {
    printf '%s\n' "$output" >&2
    fail "$fixture: nixPackagedArtifacts failed"
  }
  expect "$fixture: nixPackagedArtifacts returns the jar the Nix build made" has "$jar"
  expect "$fixture: nixRuntimeRepository returns the repository output" has 'app_[0-9.]+-repository'

  echo "== $fixture: a lockfile made from another build definition"
  echo '// changed' >>"$build/build.sbt"
  output=$(run_sbt 'show app/version') || fail "$fixture: sbt did not load with a stale lockfile"
  expect "$fixture: sbt warns that the lockfile is stale" has 'was made from another build definition'
}

maven=https/repo1.maven.org/maven2

interactive_suite multi \
  "$maven/com/eed3si9n/sbt-buildinfo_2.12_1.0/0.13.1/sbt-buildinfo_2.12_1.0-0.13.1.jar" \
  '/nix/store/[a-z0-9]+-app_3/nix-support/artifacts/scala3/app\.jar' \
  'app/runMain example.Main' \
  '\{"hello":"Nix World"\}' 'version 1\.0\.0'

interactive_suite sbt2 \
  "$maven/com/eed3si9n/sbt-buildinfo_sbt2_3/0.13.2/sbt-buildinfo_sbt2_3-0.13.2.jar" \
  '/nix/store/[a-z0-9]+-app_3/nix-support/artifacts/scala3/app\.jar' \
  'app/runMain example2.Main' \
  '\{"sbt":2\}' 'version 2\.0\.0' 'interface xsbti\.Logger'

# sbt 2 keeps the resolution of the global plugins in the build's target,
# which the wrapper removes when another plugin file made it.
echo "== sbt2: a global plugins target made with another plugin"
use_fixture sbt2
stale="$build/target/out/jvm/scala-3.8.4/global-plugins"
stamp="$build/target/sbt-nix-backend/global-plugins-inject"
mkdir -p "$stale" "$(dirname "$stamp")"
touch "$stale/made-by-another-plugin"
echo /nix/store/another-sbt-nix-backend-plugin.sbt >"$stamp"
output=$(run_sbt 'show app/version') || fail "sbt2: sbt did not load"
expect "sbt2: the wrapper removes the target made with another plugin" test ! -e "$stale/made-by-another-plugin"
expect "sbt2: the stamp names the plugin file of the wrapper" grep -qx '/nix/store/[a-z0-9]*-sbt-nix-backend-plugin.sbt' "$stamp"

# sbt 1 keeps it in the targets of the meta-builds.
echo "== multi: a meta-build target made with another plugin"
use_fixture multi
stamp="$build/target/sbt-nix-backend/global-plugins-inject"
mkdir -p "$build/project/target" "$(dirname "$stamp")"
touch "$build/project/target/made-by-another-plugin"
echo /nix/store/another-sbt-nix-backend-plugin.sbt >"$stamp"
output=$(run_sbt 'show app/version') || fail "multi: sbt did not load"
expect "multi: the wrapper removes the meta-build target made with another plugin" \
  test ! -e "$build/project/target/made-by-another-plugin"

# ivy resolves with Ivy (useCoursier := false) in the build and its
# meta-build; Ivy reads the locked downloads through a file: mirror.
interactive_suite ivy \
  "$maven/com/eed3si9n/sbt-buildinfo_2.12_1.0/0.13.1/sbt-buildinfo_2.12_1.0-0.13.1.jar" \
  '/nix/store/[a-z0-9]+-app_2\.13/nix-support/artifacts/scala2_13/app\.jar' \
  'app/runMain ivyexample.Main' \
  '\{"resolvedWith":"ivy"\}' 'version 4\.0\.0' 'interface xsbti\.Logger'

# ivy's settingsfile resolves through an Ivy settings file, which Ivy reads
# as a copy on the mirror, with the network cut off.
echo "== ivy: a project that resolves through an Ivy settings file"
use_fixture ivy
output=$(run_sbt 'settingsfile/runMain ivyexample.Settings') || fail "ivy: settingsfile did not run offline"
expect "ivy: settingsfile runs from the locked downloads" has '\{"resolvedWith":"ivysettings.xml"\}'
expect "ivy: settingsfile has the module of the Ivy repository" has 'interface xsbti\.Logger'
downloads=$(find "$COURSIER_CACHE" "$HOME/.ivy2/cache" -type f -not -name '.*' 2>/dev/null | head -5 || true)
expect "ivy: nothing went to the user's caches (found: ${downloads:-nothing})" test -z "$downloads"

# A rebuilt plugin keeps its version but is a new store path. A meta-build
# that resolves with Ivy loads the new one: Ivy reads a module of a local
# repository where its cache first found it, and each set of local
# repositories has an Ivy home of its own. The second wrapper takes a copy
# of the plugin repository at another store path.
echo "== ivy: a rebuilt plugin repository under the same version"
# shellcheck disable=SC2016 # the expression is Nix, and Nix expands it
other_wrapper=$(nix build --no-link --print-out-paths --impure --argstr root "$root" --expr '
  { root }:
  let
    flake = builtins.getFlake "git+file://${root}";
    system = builtins.currentSystem;
    pkgs = flake.inputs.nixpkgs.legacyPackages.${system};
    sbt = flake.packages.${system}.sbt;
    # This deliberately rebuilt repository is not the source default
    # implementation. Construct an unpinned test wrapper instead of claiming
    # that an arbitrary outer override preserves the portable identity.
    implementation = pkgs.callPackage flake.lib.purifier.implementation {
      source = { };
      repository = "nix-purifiers/sbt-purifier";
      tool = "sbt-purifier";
      nativeCommand = "sbt";
      checksum = "sbt";
      lockVersions = [ 3 ];
    };
  in
  implementation.wrap (pkgs.callPackage (root + "/pkgs/sbt") {
    inherit implementation;
    pluginVersion = (import (root + "/pkgs/plugin/nix/source-version.nix") root).version;
    pluginRepository = pkgs.runCommand "plugin-repository-copy" { } "cp -r ${sbt.pluginRepository} $out";
  })')
use_fixture ivy
location='eval sbtnixbackend.NixBackendPlugin.getClass.getProtectionDomain.getCodeSource.getLocation.toString'
output=$(run_sbt "$location") || fail "ivy: sbt did not load"
first=$output
saved_wrapper=$sbt_wrapper
sbt_wrapper=$other_wrapper
output=$(run_sbt "$location") || fail "ivy: sbt did not load with the rebuilt plugin"
sbt_wrapper=$saved_wrapper
copy=$(grep -o '/nix/store/[a-z0-9]*-plugin-repository-copy' "$other_wrapper/bin/sbt" | head -1)
expect "ivy: the first run loads the plugin from the flake's repository" grep -q 'ans: String = file:/nix/store/[a-z0-9]*-sbt-nix-backend_2.12_1.0-repository/' < <(printf '%s\n' "$first")
expect "ivy: the second run loads the rebuilt plugin (${copy:-no copy found})" has "ans: String = file:$copy/"

# metameta installs the plugin only in project/project/plugins.sbt and runs
# under plain sbt, without the wrapper. Without the wrapper, sbt boots
# itself and the meta-most build (project/project) resolves the plugin
# before the plugin exists, so those two take the network and the user's
# cache; the levels the plugin reaches (project/ and the build) resolve
# from the locked downloads.
runner=plain
network=on
interactive_suite metameta \
  "$maven/com/eed3si9n/sbt-buildinfo_2.12_1.0/0.13.1/sbt-buildinfo_2.12_1.0-0.13.1.jar" \
  '/nix/store/[a-z0-9]+-app_3/nix-support/artifacts/scala3/app\.jar' \
  'app/runMain metameta.Main' \
  '\{"installed":"project/project"\}' 'version 3\.0\.0'

echo "== metameta: the plugin is active in the meta-build"
use_fixture metameta
output=$(run_sbt 'reload plugins' 'show nixBackend' 'show nixBuildRoot' 'reload return') ||
  fail "metameta: sbt did not load the meta-build"
expect "metameta: the meta-build reads the lockfile" has 'info\] true'
expect "metameta: the meta-build takes the build root for its own" has "info\] $build\$"

# The same build, which installs the plugin itself, run through the
# wrapper, which injects it too. Relocking needs the network.
echo "== metameta through the wrapper"
runner=wrapper
use_fixture metameta
unset SBT_NIX_BACKEND_REPOSITORY
wrapper_repository=$(sed -n 's/^export SBT_NIX_BACKEND_REPOSITORY=//p' "$sbt_wrapper/bin/sbt")
output=$(run_sbt nixRelock 'app/runMain metameta.Main' 'show app/nixPackagedArtifacts') ||
  fail "metameta through the wrapper: the build failed"
expect "metameta through the wrapper: the app runs" has '\{"installed":"project/project"\}'
expect "metameta through the wrapper: the lockfile records the injected plugin" \
  grep -q '"via": "global-plugin"' "$build/sbt-purifier.lock.json"
expect "metameta through the wrapper: the Nix build of that lockfile succeeds" \
  has '/nix/store/[a-z0-9]+-app_3/nix-support/artifacts/scala3/app\.jar'
# project/project resolves the plugin from the repository the wrapper
# injects it from, so one plugin build runs at every level.
output=$(run_sbt 'reload plugins' 'reload plugins' 'export Runtime/fullClasspath' 'reload return' 'reload return') ||
  fail "metameta through the wrapper: sbt did not load the meta-meta-build"
plugin_jars=$(tr ':' '\n' < <(printf '%s\n' "$output") | grep 'sbt-nix-backend_2\.12_1\.0-.*\.jar' | sort -u || true)
expect "metameta through the wrapper: the meta-meta-build has one plugin jar, from the wrapper's repository (found: $plugin_jars)" \
  test "$(wc -l <<<"$plugin_jars")" -eq 1 -a "${plugin_jars#"$wrapper_repository"/}" != "$plugin_jars"
runner=wrapper
network=off

# nixPublish publishes what the Nix sandbox built, through sbt's own
# publish, with sbt's network cut off. Each project publishes into one
# directory, a Maven-style project with a Maven repository and an
# Ivy-style one with an Ivy repository; `set` keeps the build definition,
# and so the lockfile, as they are.
# nixpublish_suite <fixture> <ivy-style project>... -- <sbt command>...
nixpublish_suite() {
  local fixture=$1
  shift
  local ivy_projects=()
  while [[ $1 != -- ]]; do
    ivy_projects+=("$1")
    shift
  done
  shift
  use_fixture "$fixture"
  published="$work/$fixture-published"
  local sets=("set ThisBuild / publishTo := Some(MavenCache(\"nix-publish-test\", file(\"$published\")))")
  for p in "${ivy_projects[@]}"; do
    sets+=("set $p / publishTo := Some(Resolver.file(\"nix-publish-test\", file(\"$published\"))(Resolver.ivyStylePatterns))")
  done
  echo "== $fixture: nixPublish"
  output=$(run_sbt "${sets[@]}" "$@") || fail "$fixture: nixPublish failed"
  classes=$(find "$build" -path '*/target/*' -name '*.class' -not -path '*/project/*' | head -3)
  expect "$fixture: nixPublish compiles nothing outside the sandbox (found: ${classes:-nothing})" test -z "$classes"
}

# same_as_nix <published file> <description>: the file has the bytes of a
# file in a Nix package output.
same_as_nix() {
  local file=$1 description=$2 digest
  if [[ ! -f $file ]]; then
    fail "$description: $file was not published"
    return
  fi
  digest=$(sha256sum "$file" | cut -d' ' -f1)
  if grep -qx "$digest" < <(printf '%s\n' "$nix_digests"); then pass "$description"; else fail "$description: $file differs from the Nix build"; fi
}

# The digests of every file in the Nix package outputs nixPackagedArtifacts
# names, for the projects given.
nix_outputs() {
  local outputs
  outputs=$(run_sbt "$@" | grep -o '/nix/store/[a-z0-9]*-[^/ ]*' | sort -u) || true
  nix_digests=$(for o in $outputs; do find "$o/" -type f -exec sha256sum {} + | cut -d' ' -f1; done)
}

nixpublish_suite multi -- nixPublish
nix_outputs 'show app/nixPackagedArtifacts' 'show core/nixPackagedArtifacts' 'show javalib/nixPackagedArtifacts'
same_as_nix "$published/example/app_3/1.0.0/app_3-1.0.0.jar" "multi: nixPublish publishes the app jar Nix built"
same_as_nix "$published/example/app_3/1.0.0/app_3-1.0.0.pom" "multi: nixPublish publishes the app POM Nix built"
same_as_nix "$published/example/javalib/1.0.0/javalib-1.0.0.jar" "multi: nixPublish publishes the Java library jar Nix built"

nixpublish_suite sbt2 lib ivyplugin2 -- nixPublish
nix_outputs 'show app/nixPackagedArtifacts' 'show lib/nixPackagedArtifacts' 'show ivyplugin2/nixPackagedArtifacts'
same_as_nix "$published/example/app_3/2.0.0/app_3-2.0.0.jar" "sbt2: nixPublish publishes the app jar Nix built"
same_as_nix "$published/example/lib_3/2.0.0/jars/lib_3.jar" "sbt2: nixPublish publishes the Ivy-style lib jar Nix built"
expect "sbt2: the Ivy-style lib has its ivy.xml" test -f "$published/example/lib_3/2.0.0/ivys/ivy.xml"
same_as_nix "$published/example/ivyplugin2_sbt2_3/2.0.0/jars/ivyplugin2_sbt2_3.jar" \
  "sbt2: nixPublish publishes the Ivy-style plugin jar Nix built"

nixpublish_suite ivy lib ivyplugin ivyjava crosslib -- nixPublish 'project crosslib' '+nixPublish'
nix_outputs 'show app/nixPackagedArtifacts' 'show lib/nixPackagedArtifacts' 'show ivyplugin/nixPackagedArtifacts' \
  'show crosslib/nixPackagedArtifacts'
same_as_nix "$published/example/app_2.13/4.0.0/app_2.13-4.0.0.jar" "ivy: nixPublish publishes the app jar Nix built"
same_as_nix "$published/example/lib_2.13/4.0.0/jars/lib_2.13.jar" "ivy: nixPublish publishes the Ivy-style lib jar Nix built"
expect "ivy: the Ivy-style lib has its ivy.xml" test -f "$published/example/lib_2.13/4.0.0/ivys/ivy.xml"
same_as_nix "$published/example/ivyplugin/scala_2.12/sbt_1.0/4.0.0/jars/ivyplugin.jar" \
  "ivy: nixPublish publishes the Ivy-style sbt plugin jar Nix built"
same_as_nix "$published/example/crosslib_2.13/4.0.0/jars/crosslib_2.13.jar" "ivy: +nixPublish publishes crosslib for Scala 2.13"
same_as_nix "$published/example/crosslib_3/4.0.0/jars/crosslib_3.jar" "ivy: +nixPublish publishes crosslib for Scala 3"

# With projectMatrix each Scala version is a project of its own, so a
# plain nixPublish publishes every version, on sbt 1 and on sbt 2.
nixpublish_suite matrix -- nixPublish
nix_outputs 'show app/nixPackagedArtifacts' 'show app3/nixPackagedArtifacts' 'show core/nixPackagedArtifacts' \
  'show core3/nixPackagedArtifacts'
same_as_nix "$published/example/app_2.13/5.0.0/app_2.13-5.0.0.jar" "matrix: nixPublish publishes app for Scala 2.13"
same_as_nix "$published/example/app_3/5.0.0/app_3-5.0.0.jar" "matrix: nixPublish publishes app for Scala 3"
same_as_nix "$published/example/core_3/5.0.0/core_3-5.0.0.jar" "matrix: nixPublish publishes core for Scala 3"

nixpublish_suite matrix2 -- nixPublish
nix_outputs 'show app/nixPackagedArtifacts' 'show app2_13/nixPackagedArtifacts' 'show core/nixPackagedArtifacts' \
  'show core2_13/nixPackagedArtifacts'
same_as_nix "$published/example/app_3/6.0.0/app_3-6.0.0.jar" "matrix2: nixPublish publishes app for Scala 3"
same_as_nix "$published/example/app_2.13/6.0.0/app_2.13-6.0.0.jar" "matrix2: nixPublish publishes app for Scala 2.13"
same_as_nix "$published/example/core_2.13/6.0.0/core_2.13-6.0.0.jar" "matrix2: nixPublish publishes core for Scala 2.13"

# Start legacy sbt from an empty home as well as the newer fixtures.
network=off
use_fixture sbt013
if ! output=$(run_sbt clean test 'run nix'); then
  printf '%s\n' "$output" >&2
  fail "sbt013: offline development failed"
fi
expect "sbt013: the application runs offline" has 'Hello, Nix!'
if has 'plugin is being resolved impurely'; then
  fail "sbt013: the wrapper did not supply its plugin from Nix"
else
  pass "sbt013: the wrapper supplies its plugin purely"
fi


# Generated resolver settings stay live across clean, including parallel projects.
mapfile -t ivy_settings < <(find "$build" -type f -name '*-ivysettings.xml' | sort)
expect "sbt013: generated Ivy settings exist" test "${#ivy_settings[@]}" -gt 0
run_sbt clean
for settings in "${ivy_settings[@]}"; do
  expect "sbt013: clean preserves active Ivy settings" test -f "$settings"
done

if ((failures > 0)); then
  echo "$failures failure(s); the sbt output is in $work/sbt.log (kept)" >&2
  trap - EXIT
  exit 1
fi
echo "all interactive tests passed"
