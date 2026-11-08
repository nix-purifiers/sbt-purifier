{ pkgs, purifier }:
let
  producer =
    pkgs.runCommand "ivy-pin-provider"
      {
        nativeBuildInputs = [ pkgs.jdk_headless ];
        passthru.projections = purifier.jvmProjections {
          repository = producer;
          inherit (pkgs) runCommand python3;
        };
      }
      ''
        mkdir classes
        cat > Marker.java <<'JAVA'
        package proof;
        public final class Marker {
          public static String value() { return "leaf-42"; }
        }
        JAVA
        javac --release 8 -d classes Marker.java
        dir=$out/proof/leaf/42
        mkdir -p "$dir"
        jar --create --file "$dir/leaf-42.jar" --date=1980-01-01T00:00:02Z -C classes .
        cat > "$dir/leaf-42.pom" <<'POM'
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <modelVersion>4.0.0</modelVersion><groupId>proof</groupId>
          <artifactId>leaf</artifactId><version>42</version>
        </project>
        POM
      '';
in
producer
