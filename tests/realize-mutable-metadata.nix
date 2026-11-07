{ pkgs }:
pkgs.runCommand "sbt-realize-mutable-metadata"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.nix
    ];
  }
  ''
    export HOME="$TMPDIR/home" XDG_CACHE_HOME="$TMPDIR/cache" NIX_CONF_DIR="$TMPDIR/nix-config"
    mkdir -p "$HOME" "$XDG_CACHE_HOME" "$NIX_CONF_DIR"
    # A private store needs no daemon or host store access. Its children stay
    # inside this check's outer sandbox; only their owned loopback origin exists.
    export NIX_REMOTE="local?store=$TMPDIR/private-store&state=$TMPDIR/private-state&log=$TMPDIR/private-log"
    export NIX_CONFIG=$'experimental-features = nix-command flakes\nbuild-users-group =\nsandbox = false\nsubstituters =\nmax-jobs = 1'
    python3 ${./realize-mutable-metadata.py} \
      --source ${../.} \
      --template-lock ${./fixtures/multi/sbt-purifier.lock.json} \
      --state "$TMPDIR/evidence"
    mkdir "$out"
    cp "$TMPDIR/evidence/"*.json "$TMPDIR/evidence/"*.log "$TMPDIR/evidence/"*.xml "$out/"
    nix --version > "$out/nix-version.txt"
  ''
