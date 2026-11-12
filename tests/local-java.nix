# Frontend runtime selection regression with a real JVM outside the store.
# Backend sandbox replay avoids a daemon; dependency realization uses an
# already built tree. The local workflow is also exercised outside this check.
{ pkgs, wrapper, dependencies, fixture }:
pkgs.runCommand "sbt-local-java-runtime" {
  nativeBuildInputs = [ pkgs.jdk8_headless ];
} ''
  cp -r ${pkgs.jdk8_headless.home} caller-java
  export JAVA_HOME="$PWD/caller-java"
  provided_java="$JAVA_HOME"
  unset JAVACMD
  export HOME="$TMPDIR/home" XDG_CACHE_HOME="$TMPDIR/cache"
  export XDG_CONFIG_HOME="$TMPDIR/config" XDG_RUNTIME_DIR="$TMPDIR/run"
  export SBT_NIX_MODE=sandbox SBT_NIX_OUTPUT="$TMPDIR/output"
  export SBT_OPTS="-Xmx1g -Xss4m -XX:ActiveProcessorCount=2 -Duser.home=$HOME -Djava.io.tmpdir=$TMPDIR/java-tmp -Dsbt.server.forcestart=true -Dsbt.server.autostart=false -Dsbt.supershell=false -Dsbt.color=false"
  mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_RUNTIME_DIR" "$TMPDIR/java-tmp" bin
  mkdir "$TMPDIR/downloads"
  cp -rs --no-preserve=mode ${dependencies}/. "$TMPDIR/downloads/"
  cat > bin/nix-build <<'SH'
  #!${pkgs.runtimeShell}
  for arg in "$@"; do
    case "$arg" in
      *javaPackages.compiler*) touch "$TMPDIR/sdk-request"; exit 79 ;;
    esac
  done
  echo "$TMPDIR/downloads"
  SH
  chmod +x bin/nix-build
  export PATH="$PWD/bin:$PATH"
  cp -r ${fixture} project
  chmod -R u+w project
  cd project
  if ! ${wrapper}/bin/sbt --server -batch 'eval System.getProperty("java.home")' > "$TMPDIR/runtime.log" 2>&1; then
    cat "$TMPDIR/runtime.log"
    exit 1
  fi
  cat "$TMPDIR/runtime.log"
  grep -F "String = $provided_java" "$TMPDIR/runtime.log"
  test ! -e "$TMPDIR/sdk-request"
  touch "$out"
''
