package sbtnixbackend
import java.io.File
import sbt._
import sbt.Keys._
object Compat {
  def runEditorTask[A](extracted: Extracted, key: TaskKey[A], state: State): (State, A) =
    extracted.runTask(key, state)

  def implicitRoot(project: ResolvedProject): Boolean = true

  def moduleRevision(module: ModuleID, revision: String): ModuleID = module.copy(revision = revision)

  def archivedResolver(r: Resolver): Resolver = {
    def url(s: String): String = s
      .replace("https://repo.typesafe.com/typesafe/ivy-releases/", BootRepositories.archive)
      .replace("http://repo.typesafe.com/typesafe/ivy-releases/", BootRepositories.archive)
      .replace("https://repo.scala-sbt.org/scalasbt/ivy-releases/", BootRepositories.archive)
      .replace("http://repo.scala-sbt.org/scalasbt/ivy-releases/", BootRepositories.archive)
      .replace(
        "https://repo.scala-sbt.org/scalasbt/sbt-plugin-releases/",
        "https://scala.jfrog.io/artifactory/api/download/sbt-plugin-releases/"
      )
      .replace("http://repo1.maven.org/maven2/", BootRepositories.central)
      .replace("https://repo.maven.apache.org/maven2/", BootRepositories.central)
    r match {
      case u: URLRepository =>
        URLRepository(
          u.name,
          Patterns(
            u.patterns.ivyPatterns.map(url),
            u.patterns.artifactPatterns.map(url),
            u.patterns.isMavenCompatible,
            u.patterns.descriptorOptional,
            u.patterns.skipConsistencyCheck
          )
        )
      case m: MavenRepository => MavenRepository(m.name, url(m.root))
      case other              => other
    }
  }
  def branchName(m: ModuleID): Option[String] = m.extraAttributes.get("e:branch")

  def hasTask[A](e: Extracted, key: TaskKey[A]): Boolean = e.structure.data.get(key.scope, key.key).isDefined
  def appendWithSession(e: Extracted, settings: Seq[Setting[_]], state: State): State = {
    val appended = Load.transformSettings(Load.projectScope(e.currentRef), e.currentRef.build, e.rootProject, settings)
    val rebuilt = Load.reapply(e.session.mergeSettings ++ appended, e.structure)(e.showKey)
    Project.setProject(e.session, rebuilt, state)
  }

  def mavenCacheRoot(r: Resolver): Option[File] = None

  def mavenCache(name: String, dir: File): Resolver = MavenRepository(name, dir.toURI.toString)
  def artifact(
      name: String,
      tpe: String,
      ext: String,
      classifier: Option[String],
      configurations: Vector[Configuration],
      url: Option[java.net.URL],
      attributes: Map[String, String],
      checksum: Option[Nothing],
      insecure: Boolean
  ): Artifact = Artifact(name, tpe, ext, classifier, configurations, url, attributes)
  def publishConfig(
      maven: Boolean,
      pattern: String,
      status: String,
      configs: Vector[Configuration],
      artifacts: Vector[(Artifact, File)],
      checksums: Vector[String],
      resolver: String,
      logging: UpdateLogging.Value,
      overwrite: Boolean
  ): PublishConfiguration =
    Classpaths.publishConfig(
      artifacts.toMap,
      if (maven) None else Some(new File(pattern)),
      checksums,
      resolver,
      logging,
      overwrite
    )
  def publishedArtifacts: Def.Initialize[Task[Vector[(Artifact, File)]]] = Def.task {
    packagedArtifacts.in(publish).value.toVector
  }
  def toEither[A](result: Result[A]): Either[Incomplete, A] = result match {
    case Value(a) => Right(a); case Inc(c) => Left(c)
  }
  def runTests(extracted: Extracted, ref: ProjectRef, state: State): State =
    extracted.runTask(test.in(ref, Test), state)._1
  def usesIvyResolution: Def.Initialize[Boolean] = Def.setting(true)
  def uncached[A](a: A): A = a
  def ivyMirrorSettings(mirror: Def.Initialize[Task[Option[IvyMirror.Setup]]]): Seq[Setting[_]] = Seq(
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
    ivySbt.in(updateSbtClassifiers) := {
      Credentials.register(credentials.value, streams.value.log)
      new IvySbt(withIvyMirror(ivyConfiguration.in(updateSbtClassifiers).value, mirror.value, baseDirectory.value))
    }
  )
  private val nixIvyConfiguration = taskKey[IvyConfiguration]("Ivy configuration reading locked downloads")
  private def withIvyMirror(
      configuration: IvyConfiguration,
      mirror: Option[IvyMirror.Setup],
      base: File,
      publishTargets: Seq[Resolver] = Nil
  ): IvyConfiguration =
    (configuration, mirror) match {
      case (inline: InlineIvyConfiguration, Some(setup)) =>
        val userHome = inline.paths.ivyHome.getOrElse(new File(sys.props("user.home"), ".ivy2"))
        val resolvers = inline.resolvers.map { r =>
          val real = setup.base.fold(r)(IvyMirror.unrewrite(r, _))
          val pinned = IvyMirror.pinIvyHome(archivedResolver(real), userHome)
          setup.base.fold(pinned)(IvyMirror.rewrite(pinned, _))
        }
        val others = inline.otherResolvers.map(IvyMirror.pinIvyHome(_, userHome))
        val home = IvyMirror.home(setup, resolvers ++ others)
        new InlineIvyConfiguration(
          new IvyPaths(base, Some(home)),
          resolvers,
          others,
          inline.moduleConfigurations,
          inline.localOnly,
          inline.lock,
          inline.checksums,
          Some(new File(home, "resolution-cache")),
          inline.updateOptions,
          inline.log
        )
      case (external: ExternalIvyConfiguration, Some(setup)) =>
        val staging = new File(setup.homes, "settings-" + java.util.UUID.randomUUID())
        val copy = IvySettingsFile.copy(external.uri, staging, setup.base)
        val extra = external.extraResolvers.map(r => setup.base.fold(r)(IvyMirror.rewrite(r, _))) ++
          publishTargets.filterNot(r => external.extraResolvers.exists(_.name == r.name))
        val home = IvyMirror.home(setup.copy(key = setup.key + IvySettingsFile.digest(copy)), extra)
        val settingsDir = new File(home, "settings")
        IO.delete(settingsDir)
        IO.move(staging, settingsDir)
        val top = new File(settingsDir, copy.top.getName)
        IvySettingsFile.withCaches(top, home)
        new ExternalIvyConfiguration(base, top.toURI, external.lock, extra, external.updateOptions, external.log)
      case _ => configuration
    }
  def publishToOutput(
      config: TaskKey[PublishConfiguration],
      skipKey: TaskKey[Boolean],
      dir: Def.Initialize[File]
  ): Def.Initialize[Task[Unit]] = Def.task {
    val configuration = config.value
    val delivered = deliver.value
    val fixed = new PublishConfiguration(
      configuration.ivyFile.map(_ => delivered),
      configuration.resolverName,
      configuration.artifacts,
      configuration.checksums,
      configuration.logging,
      configuration.overwrite
    )
    IvyActions.publish(ivyModule.value, fixed, streams.value.log)
  }
  def publishProjection(ref: ProjectRef, module: ProjectedPublication.Module): Seq[Setting[_]] = Seq(
    projectID.in(ref) := module.id,
    packagedArtifacts.in(ref, publish) := module.artifacts,
    makePom.in(ref) := module.pom,
    deliver.in(ref) := module.ivy,
    skip.in(ref, publish) := false
  )
  def publishFromNix(ref: ProjectRef): Seq[Setting[_]] = Seq(packagedArtifacts.in(ref, publish) := {
    update.in(ref).value; NixBackendPlugin.autoImport.nixPackagedArtifacts.in(ref).value
  })
}
