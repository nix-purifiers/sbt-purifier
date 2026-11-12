{ pkgs }:
let
  root = ../.;
  expected = (import ../nix/lib/source-version.nix root).version;
  # Isolate the version bridge from lock validation and artifact compilation.
  plugin = pkgs.callPackage ../nix/lib/plugin.nix {
    buildSbtProject = _: {
      packages.root = pkgs.runCommand "version-bridge-proof" { } "touch $out";
    };
  };
  release = plugin.overrideAttrs { version = "9.8.7"; };
in
assert plugin.version == expected;
assert plugin.SBT_NIX_BACKEND_RELEASE_VERSION == expected;
assert release.version == "9.8.7";
assert release.SBT_NIX_BACKEND_RELEASE_VERSION == "9.8.7";
pkgs.runCommand "sbt-source-version"
  {
    nativeBuildInputs = [ pkgs.scala_2_12 ];
    JAVA_OPTS = "-XX:ActiveProcessorCount=2 -Xmx512m";
  }
  ''
    cp -r ${root} source
    chmod -R u+w source
    mkdir classes
    cat > VersionProof.scala <<'SCALA'
    object VersionProof {
      def main(args: Array[String]): Unit =
        println(SourceVersion.version(new java.io.File(args(0))))
    }
    SCALA
    scalac -d classes source/project/SourceVersion.scala VersionProof.scala
    version() { scala -cp classes VersionProof "$PWD/source"; }
    test "$(version)" = ${pkgs.lib.escapeShellArg expected}
    mkdir -p source/project/target
    echo generated > source/project/target/noise
    test "$(version)" = ${pkgs.lib.escapeShellArg expected}
    echo changed > source/src/source-version-proof.txt
    test "$(version)" != ${pkgs.lib.escapeShellArg expected}
    rm source/src/source-version-proof.txt
    echo changed >> source/sbt-purifier.lock.json
    test "$(version)" != ${pkgs.lib.escapeShellArg expected}
    touch "$out"
  ''
