package sbtnixbackend

import java.io.File

import sbt._
import sbt.Keys._
import lmcoursier.CoursierDependencyResolution
import sbt.internal.librarymanagement.IvySbt
import sbt.librarymanagement.ivy.{
  Credentials,
  ExternalIvyConfiguration,
  InlineIvyConfiguration,
  IvyConfiguration,
  IvyDependencyResolution,
  IvyPaths,
  IvyPublisher
}

/** The sbt 1 side of the sbt APIs that differ between sbt 1 and sbt 2; src/main/scala-3 holds the sbt 2 side.
  */
object Compat {
  def runEditorTask[A](extracted: Extracted, key: TaskKey[A], state: State): (State, A) =
    ClassifierDownloads.run(
      () => extracted.runTask(key, state),
      (next, _) => state.log.warn(s"[sbt-nix-backend] transient classifier download failed; retry $next of 3 after ${if (next == 2) 1 else 2}s")
    )

  def implicitRoot(project: ResolvedProject): Boolean = project.projectOrigin == ProjectOrigin.GenericRoot

  def moduleRevision(module: ModuleID, revision: String): ModuleID = module.withRevision(revision)

  def branchName(m: ModuleID): Option[String] = m.branchName

  def hasTask[A](e: Extracted, key: TaskKey[A]): Boolean = e.getOpt(key).isDefined
  def appendWithSession(e: Extracted, settings: Seq[Setting[_]], state: State): State =
    e.appendWithSession(settings, state)

  def artifact(
      name: String,
      tpe: String,
      ext: String,
      classifier: Option[String],
      configurations: Vector[sbt.librarymanagement.ConfigRef],
      url: Option[java.net.URL],
      attributes: Map[String, String],
      checksum: Option[sbt.librarymanagement.Checksum],
      insecure: Boolean
  ): Artifact =
    Artifact(name, tpe, ext, classifier, configurations, url, attributes, checksum, insecure)

  def publishConfig(
      maven: Boolean,
      pattern: String,
      status: String,
      configs: Vector[sbt.librarymanagement.ConfigRef],
      artifacts: Vector[(Artifact, File)],
      checksums: Vector[String],
      resolver: String,
      logging: sbt.librarymanagement.UpdateLogging,
      overwrite: Boolean
  ): PublishConfiguration =
    Classpaths.publishConfig(maven, pattern, status, configs, artifacts, checksums, resolver, logging, overwrite)

  def mavenCacheRoot(r: Resolver): Option[File] = r match {
    case m: sbt.librarymanagement.MavenCache => Some(m.rootFile); case _ => None
  }

  def mavenCache(name: String, dir: File): Resolver = sbt.librarymanagement.MavenCache(name, dir)

  /** The artifacts `publish` would publish, as files. */
  def publishedArtifacts: Def.Initialize[Task[Vector[(Artifact, File)]]] = Def.task {
    (publish / packagedArtifacts).value.toVector
  }

  /** The value of a task result, or its failure. */
  def toEither[A](result: Result[A]): Either[Incomplete, A] = result match {
    case Value(a)   => Right(a)
    case Inc(cause) => Left(cause)
  }

  /** Runs the tests of `ref`. */
  def runTests(extracted: Extracted, ref: ProjectRef, state: State): State =
    extracted.runTask(ref / Test / test, state)._1

  /** Whether this project resolves its dependencies with Apache Ivy. */
  def usesIvyResolution: Def.Initialize[Boolean] = Def.setting(!useCoursier.value)

  /** A project that resolves with Ivy reads the locked downloads through a mirror of their URLs, with an Ivy home of
    * its own (see IvyMirror). The rewrite applies to the Ivy configuration alone: coursier, which the plugin's own
    * repository step still uses, keeps the real URLs, so its downloads lock by them.
    */
  def ivyMirrorSettings(mirror: Def.Initialize[Task[Option[IvyMirror.Setup]]]): Seq[Setting[_]] = Seq(
    // A build may set ivyConfiguration itself (externalIvySettings does),
    // and the settings of a project come after the settings of its plugins,
    // so the rewrite is in a key of its own, which the tasks that read the
    // configuration read instead.
    nixIvyConfiguration := withIvyMirror(
      ivyConfiguration.value,
      mirror.value,
      baseDirectory.value,
      otherResolvers.value
    ),
    ivySbt := {
      Credentials.register(credentials.value, streams.value.log)
      new IvySbt(nixIvyConfiguration.value)
    },
    dependencyResolution := Def.taskIf {
      if (useCoursier.value) CoursierDependencyResolution(csrConfiguration.value)
      else IvyDependencyResolution(nixIvyConfiguration.value)
    }.value,
    publisher := IvyPublisher(nixIvyConfiguration.value),
    // The compiler bridge of Scala 2.12 and older is compiled from sources
    // that a configuration of its own resolves.
    scalaCompilerBridgeScope / ivyConfiguration :=
      withIvyMirror((scalaCompilerBridgeScope / ivyConfiguration).value, mirror.value, baseDirectory.value, Nil)
  )

  private val nixIvyConfiguration =
    taskKey[IvyConfiguration]("The Ivy configuration of the project, read through the mirror of the locked downloads.")

  /** `configuration` through the mirror of `mirror`. An Ivy settings file names only the resolvers in it, so the
    * resolvers the project publishes to (`otherResolvers`, among them the Nix output) join them by name.
    */
  private def withIvyMirror(
      configuration: IvyConfiguration,
      mirror: Option[IvyMirror.Setup],
      base: File,
      publishTargets: Seq[Resolver]
  ): IvyConfiguration =
    (mirror, configuration) match {
      case (Some(setup), inline: InlineIvyConfiguration) =>
        val userHome = inline.paths
          .flatMap(_.ivyHome)
          .orElse(sys.props.get("sbt.ivy.home").map(new File(_)))
          .getOrElse(new File(sys.props("user.home"), ".ivy2"))
        val resolvers = inline.resolvers.map { r =>
          val pinned = IvyMirror.pinIvyHome(r, userHome)
          setup.base.fold(pinned)(IvyMirror.rewrite(pinned, _))
        }
        val others = inline.otherResolvers.map(IvyMirror.pinIvyHome(_, userHome))
        val home = IvyMirror.home(setup, resolvers ++ others)
        inline
          .withResolvers(resolvers)
          .withOtherResolvers(others)
          .withPaths(inline.paths.getOrElse(IvyPaths(base, None)).withIvyHome(home))
      case (Some(setup), external: ExternalIvyConfiguration) =>
        // An Ivy settings file: Ivy reads a copy with its remote URLs on
        // the mirror (see IvySettingsFile), named like an Ivy home by the
        // repositories it reads, and with its caches in that home.
        val source = external.uri.getOrElse(sys.error("sbt-nix-backend: the Ivy settings configuration names no file"))
        val userHome =
          sys.props.get("sbt.ivy.home").map(new File(_)).getOrElse(new File(sys.props("user.home"), ".ivy2"))
        val extra = external.extraResolvers.map { r =>
          val pinned = IvyMirror.pinIvyHome(r, userHome)
          setup.base.fold(pinned)(IvyMirror.rewrite(pinned, _))
        } ++ publishTargets
          .map(IvyMirror.pinIvyHome(_, userHome))
          .filterNot(r => external.extraResolvers.exists(_.name == r.name))
        val staging = new File(setup.homes, "settings-" + java.util.UUID.randomUUID())
        val copy = IvySettingsFile.copy(source, staging, setup.base)
        val home = IvyMirror.home(setup.copy(key = setup.key + IvySettingsFile.digest(copy)), extra)
        val settingsDir = new File(home, "settings")
        sbt.io.IO.delete(settingsDir)
        sbt.io.IO.move(staging, settingsDir)
        val top = new File(settingsDir, copy.top.getName)
        IvySettingsFile.withCaches(top, home)
        external.withUri(top.toURI).withExtraResolvers(extra)
      case _ => configuration
    }

  /** The resolvers of a project's Ivy settings file, for the coursier resolution that completes its repository output.
    */
  def ivySettingsResolvers(
      mirror: Def.Initialize[Task[Option[IvyMirror.Setup]]]
  ): Def.Initialize[Task[Vector[Resolver]]] =
    Def.task {
      (nixIvyConfiguration.value, mirror.value) match {
        case (external: ExternalIvyConfiguration, Some(setup)) if usesIvyResolution.value =>
          external.uri.fold(Vector.empty[Resolver])(IvySettingsFile.resolvers(_, setup.base))
        case _ => Vector.empty
      }
    }

  /** Publishes this project into the Nix output `dir` with `config`, through sbt's publisher, in either style. Ivy's
    * publisher delivers an Ivy-style module's ivy.xml from Ivy's cache; for a project that resolves with coursier, sbt
    * writes it there before it computes `makeIvyXmlConfiguration` (sbt.internal.librarymanagement.IvyXml).
    */
  def publishToOutput(
      config: TaskKey[PublishConfiguration],
      skipKey: TaskKey[Boolean],
      dir: Def.Initialize[File]
  ): Def.Initialize[Task[Unit]] =
    Classpaths.publishOrSkip(config, skipKey).dependsOn(makeIvyXmlConfiguration)

  /** `ref / publish / packagedArtifacts` as the files the Nix build made. Ivy's publisher delivers an Ivy-style
    * module's ivy.xml from Ivy's resolution of the project, so the resolution runs first; it reads the locked downloads
    * and compiles nothing.
    */
  def publishProjection(ref: ProjectRef, module: ProjectedPublication.Module): Seq[Setting[_]] = Seq(
    ref / projectID := module.id,
    ref / publish / packagedArtifacts := module.artifacts,
    ref / makePom := module.pom,
    ref / deliver := module.ivy,
    ref / publish / skip := false
  )

  def publishFromNix(ref: ProjectRef): Seq[Setting[_]] = Seq(
    ref / publish / packagedArtifacts := {
      Def.unit((ref / update).value)
      (ref / NixBackendPlugin.autoImport.nixPackagedArtifacts).value
    }
  )

  /** sbt 1 does not cache task results, so every task runs. */
  def uncached[A](a: A): A = a
}
