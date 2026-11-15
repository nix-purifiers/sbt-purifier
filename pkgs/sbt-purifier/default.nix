# SPDX-License-Identifier: MIT
#
# The commands of the purifier contract, each a thin wrapper around a task
# of the plugin, run through the sbt wrapper (pkgs/sbt) from the root of
# the sbt build:
#
#   sbt-purifier lock      nixLock: lock the build, keeping a lockfile
#                          that is up to date as it is
#   sbt-purifier update    nixLockUpdate: resolve every dependency afresh,
#                          dynamic versions included, lock every flake
#                          repository at the newest revision of its
#                          reference, and relock
#   sbt-purifier prefetch  realize every locked download in
#                          the Nix store, with garbage-collector roots
#                          beside the lockfile, so sbt and an editor work
#                          offline
#   sbt-purifier publish   nixPublish: publish what Nix built
#
# `--lockfile <path>` names a lockfile kept outside the build; it sets
# SBT_PURIFIER_LOCKFILE, which the plugin and the sbt wrapper read, and
# which a dev shell can set instead.
{
  lib,
  pkgs,
  writeShellScriptBin,
  sbt,
  implementation,
}:
let
  nixLib = ../../lib-overlays/default;
  # dependencies.nix imports the lock reader from lib-overlays/default by a
  # relative path, so both directories go to the store at their places in
  # the repository.
  dependencySource = lib.fileset.toSource {
    root = ../..;
    fileset = lib.fileset.unions [
      nixLib
      ../plugin/nix
    ];
  };
in
writeShellScriptBin "sbt-purifier" ''
  set -euo pipefail
  originalArgs=("$@")

  usage() {
    echo "usage: sbt-purifier <lock|update|prefetch|publish|implementation|self-update> [--lockfile <path>] [arguments...]" >&2
    exit 2
  }

  [ $# -ge 1 ] || usage
  command=$1
  shift
  args=()
  while [ $# -gt 0 ]; do
    case $1 in
      --lockfile)
        [ $# -ge 2 ] || usage
        SBT_PURIFIER_LOCKFILE=$(${pkgs.coreutils}/bin/realpath -m "$2")
        export SBT_PURIFIER_LOCKFILE
        shift 2
        ;;
      *)
        args+=("$1")
        shift
        ;;
    esac
  done
  lockfile=''${SBT_PURIFIER_LOCKFILE:-$PWD/sbt-purifier.lock.json}
  if [ "$command" = self-update ]; then
    exec ${pkgs.python3}/bin/python ${implementation.tool} self-update \
      --config ${implementation.manifest} --lockfile "$lockfile" \
      --installed-command "$0" "''${args[@]}"
  fi
  selected="$(${pkgs.python3}/bin/python ${implementation.tool} dispatch \
    --config ${implementation.manifest} --lockfile "$lockfile" --entry sbt-purifier)"
  if [ -n "$selected" ]; then exec "$selected" "''${originalArgs[@]}"; fi
  export SBT_PURIFIER_IMPLEMENTATION_CONFIG=${implementation.manifest}
  if [ "$command" = implementation ]; then
    exec ${pkgs.python3}/bin/python ${implementation.tool} current \
      --config ${implementation.manifest} --lockfile "$lockfile"
  fi

  # The problems readLock finds in the lockfile, one per line, each
  # naming the command that fixes it.
  problems() {
    nix-instantiate --eval --strict --json --expr \
      '{ src, lockFile }: (import ${nixLib}/read-lock.nix { src = /. + src; lockFile = /. + lockFile; }).problems' \
      --argstr src "$PWD" --argstr lockFile "$lockfile" |
      ${lib.getExe pkgs.jq} -r '.[]'
  }

  check() {
    found=$(problems)
    if [ -n "$found" ]; then
      echo "sbt-purifier: $found" >&2
      exit 1
    fi
  }

  case $command in
    implementation-relock)
      exec ${lib.getExe sbt} --server -batch "''${args[@]}" nixImplementationRelock
      ;;
    lock)
      exec ${lib.getExe sbt} -batch "''${args[@]}" nixLock
      ;;
    update)
      if [ ''${#args[@]} -gt 0 ]; then
        echo "sbt-purifier: sbt resolves every dependency of the build at once, so update takes no package names; run \`sbt-purifier update\`" >&2
        exit 2
      fi
      exec ${lib.getExe sbt} -batch nixLockUpdate
      ;;
    prefetch)
      check
      nix-build --expr \
        '{ lockFile }: let pkgs = import ${pkgs.path} { }; in
          pkgs.callPackage ${dependencySource}/pkgs/plugin/nix/dependencies.nix { lockFile = /. + lockFile; }' \
        --argstr lockFile "$lockfile" --out-link "$lockfile.gcroot"
      echo "sbt-purifier: every download of $lockfile is in the Nix store; $lockfile.gcroot keeps them" >&2
      ;;
    publish)
      check
      exec ${lib.getExe sbt} -batch "''${args[@]}" nixPublish
      ;;
    *)
      usage
      ;;
  esac
''
