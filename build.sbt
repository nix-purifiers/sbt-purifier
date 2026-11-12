// SPDX-License-Identifier: MIT

ThisBuild / organization := "io.github.clhodapp"
ThisBuild / organizationName := "io.github.clhodapp"
// Releases pass their version explicitly; source builds identify the plugin
// source bytes, using the same manifest as the Nix wrapper's injected version.
ThisBuild / version := sys.env
  .get("SBT_NIX_BACKEND_RELEASE_VERSION")
  .filter(_.nonEmpty)
  .getOrElse(SourceVersion.version((ThisBuild / baseDirectory).value))
ThisBuild / licenses := Seq("MIT" -> url("https://opensource.org/licenses/MIT"))
ThisBuild / homepage := Some(url("https://nix-purifiers.github.io/sbt-purifier/"))

lazy val root = (project in file("."))
  .settings(
    name := "sbt-nix-backend",
    sbtPlugin := true,
    sbtPluginPublishLegacyMavenStyle := scalaBinaryVersion.value == "2.10",
    Compile / unmanagedSourceDirectories ++= {
      if (scalaBinaryVersion.value == "2.10") Nil
      else Seq(baseDirectory.value / "src" / "main" / "scala-modern")
    },
    Compile / unmanagedSourceDirectories ++= {
      if (scalaBinaryVersion.value == "3") Nil else Seq(baseDirectory.value / "src" / "main" / "scala-ivy")
    },
    // sbt 0.13 uses Scala 2.10 and Ivy; sbt 1 and 2 use Coursier.
    scalaVersion := "2.12.21",
    crossScalaVersions := Seq("2.12.21", "2.10.7", "3.8.4"),
    pluginCrossBuild / sbtVersion := (scalaBinaryVersion.value match {
      case "2.10" => "0.13.18"
      case "2.12" => "1.13.1"
      case _      => "2.0.12"
    }),
    scalacOptions ++= Seq("-deprecation", "-feature", "-unchecked") ++
      (if (scalaBinaryVersion.value == "2.12") Seq("-Xlint:-unused,_") else Nil),
    // sbt 2's lm-coursier classes carry the @data annotation of
    // data-class, a library sbt compiles them with but does not depend on.
    // Scaladoc reads the annotation from their TASTy and fails without its
    // class, so the documentation classpath, and only that, gets the jar;
    // a library dependency would put it in the published POM. The version
    // is the one whose annotation constructor matches what the TASTy calls
    // (0.2.9 and later take nine arguments where sbt 2.0.12 passes none).
    Compile / doc / dependencyClasspath ++= {
      if (scalaBinaryVersion.value != "3") Nil
      else
        dependencyResolution.value
          .retrieve(
            ("io.github.alexarchambault" % "data-class_3" % "0.2.8").intransitive(),
            scalaModuleInfo.value,
            target.value / "doc-classpath",
            streams.value.log
          )
          .fold(w => throw w.resolveException, _.map(Attributed.blank))
    },
    resolvers += Resolver.url(
      "sbt-archive",
      url("https://scala.jfrog.io/artifactory/api/download/ivy-releases/")
    )(Resolver.ivyStylePatterns),
    libraryDependencies ++= {
      if (scalaBinaryVersion.value == "2.10") Seq("com.novocode" % "junit-interface" % "0.11" % Test)
      else Seq("org.scalameta" %% "munit" % "1.0.4" % Test)
    },
    Test / unmanagedSourceDirectories := {
      if (scalaBinaryVersion.value == "2.10") Seq(baseDirectory.value / "src" / "test" / "scala-2.10")
      else (Test / unmanagedSourceDirectories).value
    },
    NixLibraryResources.settings,
    // NixBackendPlugin reads its coordinates from the jar manifest to
    // record which plugin version a lock was made with.
    Compile / packageBin / packageOptions += Package.ManifestAttributes(
      "Implementation-Title" -> name.value,
      "Implementation-Version" -> version.value,
      "Implementation-Vendor" -> organization.value
    )
  )
