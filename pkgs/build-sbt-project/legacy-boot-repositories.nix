# SPDX-License-Identifier: MIT
# `base` may name the runtime's cache through a shell variable.
{
  base ? null,
}:
let
  prefix =
    url: if base == null then url else "${base}/${builtins.replaceStrings [ "://" ] [ "/" ] url}";
in
''
  [repositories]
  local
  maven-central: ${prefix "https://repo1.maven.org/maven2/"}
  sbt-ivy: ${prefix "https://scala.jfrog.io/artifactory/api/download/ivy-releases/"}, [organization]/[module]/[revision]/[type]s/[artifact](-[classifier]).[ext]
''
