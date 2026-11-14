name := "scalafmt-session-fixture"

scalaVersion := "2.13.18"

Compile / scalafmtCheck / scalafmtConfig := baseDirectory.value / ".scalafmt-check.conf"
