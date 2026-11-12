# SPDX-License-Identifier: MIT
#
# nixpkgs' sbt with sbt-nix-backend as a global plugin. sbt loads global
# plugins below every level of a build, so the plugin applies to
# project/project, project/ and the build itself, and governs the
# resolution of every plugin the build declares. No entry in the build's
# project/plugins.sbt is needed.
#
# sbt writes target/ directories into the global plugins directory, so the
# wrapper keeps a writable one per plugin version under
# $XDG_CACHE_HOME/sbt-nix-backend, holding the plugin's .sbt file and links
# to the user's own global plugins (~/.sbt/1.0/plugins, or the directory in
# SBT_NIX_BACKEND_USER_GLOBAL_PLUGINS).
#
# System properties tell the plugin what the wrapper knows:
# `sbt.nix.backend.inject` names the plugin's .sbt file, so a lock made
# with this sbt records the injection; `sbt.nix.backend.repository` is the
# repository a Nix build of such a lock injects the plugin from; and
# `sbt.nix.backend.nixpkgs` is the nixpkgs this flake pins, the default
# package set of the Nix builds nixPackagedArtifacts runs.
{
  lib,
  # nixpkgs' sbt, taken from `pkgs`: in this package scope `sbt` names
  # this wrapper.
  pkgs,
  runtimeShell,
  writeText,
  writeTextFile,
  pluginRepository,
  pluginVersion,
  implementation,
}:
let
  # The pinned nixpkgs recipe appends a fixed -java-home to conf/sbtopts.
  # Keep the upstream runner's Java selection and project compatibility checks.
  # Its installation check assumes that bundled runtime; exercise this variant
  # with an actual caller-provided runtime instead.
  nativeSbt = pkgs.sbt.overrideAttrs {
    postPatch = "";
    doInstallCheck = false;
  };
  injectText = ''
    resolvers += MavenRepository("sbt-nix-backend-plugin", "file://${pluginRepository}")
    addSbtPlugin("io.github.clhodapp" % "sbt-nix-backend" % "${pluginVersion}")
  '';
  inject = writeText "sbt-nix-backend-plugin.sbt" injectText;
  legacyInject = writeText "sbt-purifier-legacy-plugin.sbt" (
    injectText + builtins.readFile ../../nix/lib/legacy-global-settings.sbt
  );
  legacyLauncher = pkgs.fetchurl {
    url = "https://scala.jfrog.io/artifactory/api/download/ivy-releases/org.scala-sbt/sbt-launch/0.13.18/jars/sbt-launch.jar";
    hash = "sha256-ZvklAnUM2fxS3RAg071BQK+ptYC9I6dGMrP/RMxzqbI=";
  };
  # The directory name changes with the plugin, so a new plugin version
  # never meets the old one's resolution in target/.
  dirName = "global-plugins-" + builtins.substring 11 32 (toString inject);
in
writeTextFile {
  name = "sbt-with-nix-backend-${pluginVersion}";
  executable = true;
  destination = "/bin/sbt";
  derivationArgs.passthru = { inherit inject pluginRepository; };
  meta = {
    description = "sbt with the sbt-nix-backend plugin as a global plugin";
    mainProgram = "sbt";
  };
  text = ''
    #!${runtimeShell}
    set -eu
    lockfile=''${SBT_PURIFIER_LOCKFILE:-sbt-purifier.lock.json}
    implementation_pinned=0
    case "''${SBT_NIX_BACKEND:-1}" in
      0 | false) ;;
      *)
        selected="$(${pkgs.python3}/bin/python ${implementation.tool} dispatch \
          --config ${implementation.manifest} --lockfile "$lockfile" --entry sbt)"
        if [ -n "$selected" ]; then exec "$selected" "$@"; fi
        export SBT_PURIFIER_IMPLEMENTATION_CONFIG=${implementation.manifest}
        if [ -f "$lockfile" ]; then
          implementation_pinned="$(${pkgs.python3}/bin/python - "$lockfile" <<'PY'
    import json, sys
    print(1 if json.load(open(sys.argv[1])).get("implementation") is not None else 0)
    PY
          )"
        fi
        ;;
    esac
    if [ "$implementation_pinned" = 1 ]; then
      ${pkgs.python3}/bin/python ${../../nix/lib/implementation-mode.py} \
        --sbt-home ${nativeSbt}/share/sbt -- "$@"
      # sbt 2 defaults to an existing daemon, whose plugin and environment
      # cannot be replaced by changing a client process. Keep the ordinary
      # command in a fresh foreground JVM. This flag is consumed by the runner,
      # including when project/build.properties selects an older sbt SDK.
      set -- "$@" --server
    fi
    wanted=$(sed -n 's/^[[:space:]]*sbt\.version[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' project/build.properties 2>/dev/null || true)
    legacy=0
    inject=${inject}
    binary=1.0
    case "$wanted" in
      0.13.*) legacy=1; inject=${legacyInject}; binary=0.13 ;;
      2.*) binary=2.0 ;;
    esac
    dir="''${XDG_CACHE_HOME:-$HOME/.cache}/sbt-nix-backend/${dirName}-$binary"
    user="''${SBT_NIX_BACKEND_USER_GLOBAL_PLUGINS:-$HOME/.sbt/$binary/plugins}"
    mkdir -p "$dir"
    ln -sfn "$inject" "$dir/sbt-nix-backend.sbt"
    # Links to the user's global plugins: added for new entries, removed
    # for entries that are gone. target/ stays this directory's own.
    for link in "$dir"/* "$dir"/.[!.]*; do
      if [ -L "$link" ] && [ "$(basename "$link")" != sbt-nix-backend.sbt ] && [ ! -e "$link" ]; then
        rm "$link"
      fi
    done
    if [ -d "$user" ]; then
      for entry in "$user"/* "$user"/.[!.]*; do
        [ -e "$entry" ] || continue
        name=$(basename "$entry")
        [ "$name" = target ] && continue
        [ -e "$dir/$name" ] || ln -s "$entry" "$dir/$name"
      done
    fi
    # sbt itself boots before any level of the build exists, and the
    # launcher downloads it through COURSIER_CACHE. With a lockfile for
    # this sbt version, that cache is the locked downloads, so sbt boots
    # from them. The plugin points the build's own resolution elsewhere
    # (the locked cache, or the user's cache named here), so nothing
    # writes into this read-only tree.
    lockfile=''${SBT_PURIFIER_LOCKFILE:-sbt-purifier.lock.json}
    case "''${SBT_NIX_BACKEND:-1}" in 0 | false) lockfile= ;; esac
    if [ -n "$lockfile" ] && [ ! -e "$lockfile" ]; then
      echo "sbt-purifier: warning: no sbt-purifier.lock.json found; running sbt impurely. Run \`sbt nixLock\` to use Nix-resolved dependencies." >&2
    fi
    # Local adoption uses the caller's Java runtime. The upstream runner
    # handles JAVACMD, JAVA_HOME, PATH and explicit --java-home arguments.
    # Packaged SDK selection belongs to a full sandboxed project build.
    boot_pinned=0
    if [ -n "$lockfile" ] && [ -f "$lockfile" ] && [ -f project/build.properties ]; then
      locked=$(sed -n 's/^  "sbtVersion": "\(.*\)",$/\1/p' "$lockfile")
      wanted=$(sed -n 's/^[[:space:]]*sbt\.version[[:space:]]*=[[:space:]]*\([^[:space:]]*\).*/\1/p' project/build.properties)
      # A lockfile of another format (made by another plugin version) is
      # one nixLock replaces; until then, sbt boots the usual way.
      format=$(sed -n 's/^  "version": \([0-9]*\),$/\1/p' "$lockfile")
      if [ -n "$locked" ] && [ "$locked" = "$wanted" ] && [ "$format" = "${toString (import ../../nix/lib/lock-version.nix)}" ]; then
        mkdir -p target/sbt-nix-backend/gcroots
        if tree=$(nix-build --expr \
          '{ lockFile }: (import ${pkgs.path} { }).callPackage ${../../nix/lib}/dependencies.nix { inherit lockFile; }' \
          --arg lockFile "$(realpath "$lockfile")" --out-link target/sbt-nix-backend/gcroots/boot-cache); then
          export SBT_NIX_BACKEND_USER_COURSIER_CACHE="''${COURSIER_CACHE:-}"
          export COURSIER_CACHE="$tree"
          boot_pinned=1
        else
          echo "sbt-nix-backend: the locked downloads did not build; sbt boots the usual way" >&2
        fi
      fi
    fi
    # A build that also installs the plugin itself, in its
    # project/project/plugins.sbt, resolves it from the repository this
    # variable names; naming the wrapper's own repository there gives that
    # build the jar the wrapper injects, so one plugin build runs.
    export SBT_NIX_BACKEND_REPOSITORY=${pluginRepository}
    export SBT_NIX_BACKEND_PLUGIN_VERSION=${lib.escapeShellArg pluginVersion}
    # sbt caches the resolution of the global plugin by its module and
    # version, which a plugin built from newer sources shares with the
    # one before it. sbt 2 keeps it in the build's own
    # target/out/jvm/<Scala version>/global-plugins, and sbt 1 in the
    # targets of the meta-builds, project/target and project/project/target.
    # A stamp records the plugin file this wrapper injects; when it names
    # another, the wrapper removes those targets, so sbt resolves the
    # plugin of this wrapper.
    if [ -f build.sbt ] || [ -d project ]; then
      stamp=target/sbt-nix-backend/global-plugins-inject
      if [ "$(cat "$stamp" 2>/dev/null)" != "$inject" ]; then
        for target in target/out/jvm/*/global-plugins project/target project/project/target; do
          if [ -d "$target" ]; then rm -rf "$target"; fi
        done
        mkdir -p target/sbt-nix-backend
        echo "$inject" > "$stamp"
      fi
    fi
    # The properties go in SBT_OPTS rather than on the command line: for an
    # sbt 2 build the runner starts a client, and the server it starts in
    # the background gets SBT_OPTS too.
    export SBT_OPTS="-Dsbt.global.plugins=$dir -Dsbt.nix.backend.inject=$inject -Dsbt.nix.backend.repository=${pluginRepository} -Dsbt.nix.backend.nixpkgs=${pkgs.path} ''${SBT_OPTS:-}"
    if [ "$legacy" = 1 ]; then
      launcher=${legacyLauncher}
      if [ "$boot_pinned" = 1 ]; then
        export SBT_NIX_BOOT_MIRROR="file://$COURSIER_CACHE"
        locked_launcher=$(sed -n 's/^  "launcher": "\(.*\)",$/\1/p' "$lockfile")
        launcher="$COURSIER_CACHE/$locked_launcher"
        cat > "$dir/launcher-repositories" <<EOF
    ${import ../../nix/lib/legacy-boot-repositories.nix { base = "file://$COURSIER_CACHE"; }}
    EOF
      else
        cat > "$dir/launcher-repositories" <<EOF
    ${import ../../nix/lib/legacy-boot-repositories.nix { }}
    EOF
      fi
      export SBT_OPTS="$SBT_OPTS -Dsbt.repository.config=$dir/launcher-repositories"
      exec ${lib.getExe nativeSbt} --sbt-jar "$launcher" "$@"
    fi
    exec ${lib.getExe nativeSbt} "$@"
  '';
}
