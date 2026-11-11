package sbtnixbackend

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files

import sbt._
import sbt.Keys._

/** sbt-nix-backend: Nix as the dependency and build backend of sbt.
  *
  * The plugin runs in one of three modes, chosen by the environment:
  *
  *  - sandbox (`SBT_NIX_MODE=sandbox`): inside a Nix build. Resolution reads
  *    the coursier cache the build was given (`COURSIER_CACHE`), offline.
  *  - lock (`SBT_NIX_MODE=lock`): inside the sbt that `nixLock` forks. The
  *    same build as in the sandbox, with an empty cache and the network.
  *  - interactive (neither): the sbt a developer runs. With a lock present,
  *    resolution reads a cache built from the lock's store paths, offline,
  *    so compile, run and package are incremental builds on the locked
  *    downloads (feature 3).
  */
object NixBackendPlugin extends AutoPlugin {

  override def trigger = allRequirements
  override def requires = plugins.JvmPlugin

  object autoImport {

    /** A flake reference to an sbt-nix-backend repository output: `ref` is
      * a flake reference whose fragment names the attribute under
      * `sbtRepositories.<system>` (`github:owner/repo/<rev>#core`); the
      * fragment defaults to `name`.
      */
    final case class NixFlakeRepository(name: String, ref: String) {
      def flakeRef: String = ref.takeWhile(_ != '#')
      def attribute: String = if (ref.contains('#')) ref.dropWhile(_ != '#').drop(1) else name
    }

    val nixBuildRoot = settingKey[File](
      "The root of the concrete build. In a meta-build (project/, project/project/, ...) it is the build " +
        "the meta-build defines, so every level reads the one lockfile and coursier cache."
    )
    val nixLockFile = settingKey[File](
      "The lockfile, which buildSbtProject (from the flake overlay) builds the build from " +
        "(default: the file SBT_PURIFIER_LOCKFILE names, or else sbt-purifier.lock.json in the build root)."
    )
    val nixBackend = settingKey[Boolean](
      "Whether interactive resolution reads the locked downloads through Nix, offline. " +
        "Defaults to true when the lock file exists; SBT_NIX_BACKEND=0 turns it off."
    )
    val nixPackaged = taskKey[Boolean](
      "Whether the lockfile names this project as a package to build (default: !(publish / skip))."
    )
    val nixNetworkDependencies = taskKey[Seq[String]](
      "URLs this project's build downloads outside coursier. nixLock fetches and locks them, and sandboxed " +
        "builds find them in the coursier cache at the path coursier would use (see nixNetworkFile). " +
        "A plugin can declare them without depending on sbt-nix-backend by defining a task key of " +
        "the same name and type, and read them from the cache the system property " +
        "sbt.nix.backend.coursierCache names."
    )
    val nixFlakeRepositories = settingKey[Seq[NixFlakeRepository]](
      "Flake references to purified JVM packages; inferred native coordinates select their Nix-built artifacts before other resolvers."
    )
    val nixFlakeRepositoryPaths = settingKey[Map[String, File]](
      "The store path of each flake repository, by name."
    )
    val nixSbtJavaOptions = settingKey[Seq[String]](
      "JVM options of the sbt that nixLock and the Nix builds run."
    )
    val nixLauncherUrl = settingKey[String]("Where the sbt launcher jar of this build's sbt version is downloaded from.")
    val nixCoursierCache = taskKey[File](
      "Interactive mode: realizes the lock through Nix and returns the coursier cache built from its store paths."
    )
    val nixLock = taskKey[File](
      "Writes the lockfile when the build definition changed since the last one: runs the sandboxed build " +
        "once, with an empty cache and the network, and locks every file it downloaded."
    )
    val nixRelock = taskKey[File]("Writes the lockfile whether or not the build definition changed.")
    val nixNixpkgs = settingKey[String](
      "The nixpkgs nixPackagedArtifacts builds with: a nixpkgs source directory or a flake reference. " +
        "The sbt-nix-backend sbt wrapper sets the nixpkgs its flake pins; elsewhere the default is flake:nixpkgs."
    )
    val nixPackagedArtifacts = taskKey[Map[Artifact, File]](
      "Like publish / packagedArtifacts, but built by Nix in its sandbox from the lockfile (relocking first if " +
        "the build definition changed): each artifact publish publishes for this Scala version, as a file in " +
        "the Nix build's output."
    )
    val nixPublication = taskKey[File]("Builds the immutable JVM publication and its catalog with Nix.")
    val nixRuntimeRepository = taskKey[File](
      "Builds this project's repository output in the Nix sandbox: a Maven repository of its artifacts and " +
        "their runtime dependencies."
    )
    val nixLockUpdate = taskKey[File]("Like nixRelock, and also locks every flake repository afresh.")
    val nixImplementationRelock = taskKey[File]("Refresh purifier metadata without resolving project dependencies.")
    val nixPublishArtifacts =
      taskKey[File]("Sandbox: publishes packagedArtifacts as a Maven repository under SBT_NIX_OUTPUT.")
    val nixPublishRepository = taskKey[File](
      "Sandbox: writes this project, its internal dependencies and their runtime dependencies as a Maven " +
        "repository under SBT_NIX_OUTPUT."
    )
    val nixLockedProject = taskKey[Option[LockedProject]]("How the lock names this project, if it is packaged.")

    /** The module a flake repository was built for, read from the
      * repository's `sbt-nix-repository.json`. A repository of a cross-built
      * project holds a module per Scala version; this takes the one whose
      * name ends in this project's Scala binary version.
      */
    def nixFlakeModule(name: String): Def.Initialize[ModuleID] = Def.setting {
      val paths = (ThisBuild / nixFlakeRepositoryPaths).value
      val views = (ThisBuild / nixFlakeRepositoryViews).value
      val binary = scalaBinaryVersion.value
      val dir = paths.getOrElse(
        name,
        sys.error(s"sbt-nix-backend: no flake repository named $name in ThisBuild / nixFlakeRepositories")
      )
      val legacy = new File(dir, RepositoryMetadata)
      val metadata = if (legacy.isFile) legacy else views.get(name).flatMap(_.catalog).getOrElse(
        sys.error(s"sbt-purifier: repository $name has no module metadata")
      )
      val meta = Json.parse(new String(Files.readAllBytes(metadata.toPath), StandardCharsets.UTF_8)).asObject
      val all = meta("modules").asArray.map(_.asObject)
      val modules = if (legacy.isFile) all else {
        val producer = meta.get("producer").getOrElse(
          sys.error(s"sbt-purifier: repository $name needs a catalog that identifies its producer")
        )
        all.filter(_.get("producer").exists(_ == producer))
      }
      val module = modules match {
        case Vector(only) => only
        case several =>
          several.find(_("name").asString.endsWith("_" + binary)).getOrElse(
            sys.error(
              s"sbt-nix-backend: flake repository $name holds ${several.map(_("name").asString).mkString(", ")}, " +
                s"none for Scala $binary"
            )
          )
      }
      val attributes =
        module.get("attributes").fold(Map.empty[String, String])(_.asObject.fields.map { case (k, v) => k -> v.asString }.toMap)
      ModuleID(module.get("organization").getOrElse(module("group")).asString, module("name").asString, module("version").asString)
        .withExtraAttributes(attributes)
    }

    /** Where a declared network dependency is in the coursier cache, as a
      * task for a plugin to read the file from.
      */
    def nixNetworkFile(url: String): Def.Initialize[Task[File]] = Def.task {
      val dir = Mode.current match {
        case Mode.Interactive if (ThisBuild / nixBackend).value => (ThisBuild / nixCoursierCache).value
        case Mode.Interactive => cacheRoot((ThisBuild / nixBuildRoot).value) / "network"
        case _                => Mode.coursierCacheFromEnv
      }
      val f = dir / CachePaths.relativePath(url)
      if (!f.exists() && Mode.current == Mode.Interactive && !(ThisBuild / nixBackend).value)
        LockGeneration.download(url, f)
      if (!f.exists())
        sys.error(s"sbt-nix-backend: $url is not in the coursier cache; declare it in nixNetworkDependencies and relock")
      f
    }
  }

  import autoImport._

  private val nixFlakeRepositoryViews =
    settingKey[Map[String, Nix.RepositoryView]]("The realized JVM repository views and catalogs.")
  private val nixProvidedModules = settingKey[Vector[ModuleID]]("Native coordinates selected by Nix inputs.")


  /** The file at the root of every repository the plugin writes, naming
    * the module it was built for.
    */
  val RepositoryMetadata = "sbt-nix-repository.json"

  sealed trait Mode
  object Mode {
    case object Sandbox extends Mode
    case object Lock extends Mode
    case object Interactive extends Mode

    lazy val current: Mode = sys.env.get("SBT_NIX_MODE") match {
      case Some("sandbox") => Sandbox
      case Some("lock")    => Lock
      case None | Some("") => Interactive
      case Some(other)     => sys.error(s"sbt-nix-backend: unknown SBT_NIX_MODE '$other'")
    }

    def output: File = new File(
      sys.env.getOrElse("SBT_NIX_OUTPUT", sys.error("sbt-nix-backend: SBT_NIX_OUTPUT is not set"))
    )

    def coursierCacheFromEnv: File = new File(
      sys.env.getOrElse("COURSIER_CACHE", sys.error("sbt-nix-backend: COURSIER_CACHE is not set"))
    )

    def flakeRepositoryPathsFromEnv: Map[String, File] =
      sys.env.get("SBT_NIX_FLAKE_REPOSITORIES").filter(_.trim.nonEmpty) match {
        case None => Map.empty
        case Some(json) =>
          Json.parse(json).asObject.fields.map { case (k, v) => k -> new File(v.asString) }.toMap
      }
  }

  final case class PluginCoordinates(organization: String, name: String, version: String)

  /** The coordinates this plugin was published under, from its jar
    * manifest. A build that compiles the plugin from source (this
    * repository's own) has no manifest, and then no plugin to pass on.
    */
  lazy val pluginCoordinates: Option[PluginCoordinates] = {
    val pkg = getClass.getPackage
    for {
      org <- Option(pkg.getImplementationVendor)
      name <- Option(pkg.getImplementationTitle)
      version <- Option(pkg.getImplementationVersion)
    } yield PluginCoordinates(org, name, version)
  }

  /** The root of the Maven repository this plugin's jar sits in, when its
    * path ends in `<organization>/<module>/<version>/<jar>`: a file
    * repository it was resolved from, or the part of a coursier cache that
    * mirrors a remote one.
    */
  lazy val pluginRepositoryRoot: Option[File] =
    for {
      c <- pluginCoordinates
      source <- Option(getClass.getProtectionDomain.getCodeSource)
      jar = new File(source.getLocation.toURI)
      if jar.isFile
      versionDir <- Option(jar.getParentFile)
      if versionDir.getName == c.version
      moduleDir <- Option(versionDir.getParentFile)
      if moduleDir.getName.startsWith(c.name + "_")
      orgPath = c.organization.split('.').toSeq
      root <- orgPath.foldLeft(Option(moduleDir.getParentFile))((d, _) => d.flatMap(f => Option(f.getParentFile)))
      if orgPath.foldLeft(root)((d, s) => new File(d, s)).getCanonicalFile == moduleDir.getParentFile.getCanonicalFile
    } yield root.getCanonicalFile

  /** The repository `SBT_NIX_BACKEND_REPOSITORY` names, if it is set. */
  def environmentRepository: Option[File] =
    sys.env.get(LockedPlugin.RepositoryVariable).filter(_.nonEmpty).map(new File(_).getCanonicalFile)

  /** How a Nix build of the build reaches this plugin, if it must supply
    * the repository; see [[LockedPlugin]].
    */
  def lockedPlugin: Option[LockedPlugin] =
    pluginCoordinates.flatMap { c =>
      def as(via: String) = LockedPlugin(c.organization, c.name, c.version, via)
      if (injectedPluginFile.isDefined) Some(as(LockedPlugin.GlobalPlugin))
      else if (environmentRepository.isDefined && environmentRepository == pluginRepositoryRoot)
        Some(as(LockedPlugin.Environment))
      else None
    }

  /** In a meta-build, the plugin adds itself to the build the meta-build
    * defines, so a build installs it once, in its meta-most build
    * (`project/project/plugins.sbt`), and every level above has it: the
    * plugin governs each level's resolution, and the `*.sbt` files of each
    * level can use its keys. The resolvers are the repository its jar came
    * from and the one `SBT_NIX_BACKEND_REPOSITORY` names. A plugin the
    * wrapper loaded as a global plugin is at every level already.
    */
  private def passOnSettings: Seq[Setting[_]] = Seq(
    libraryDependencies ++= {
      if (!isMetaBuild.value || injectedPluginFile.isDefined) Nil
      else
        pluginCoordinates.toSeq.map { c =>
          Defaults.sbtPluginExtra(ModuleID(c.organization, c.name, c.version), sbtBinaryVersion.value, scalaBinaryVersion.value)
        }
    },
    resolvers ++= {
      if (!isMetaBuild.value || injectedPluginFile.isDefined) Nil
      else
        (environmentRepository.toSeq ++ pluginRepositoryRoot.toSeq).distinct.zipWithIndex.map { case (dir, i) =>
          MavenRepository(s"sbt-nix-backend-plugin-$i", dir.toURI.toString)
        }
    }
  )

  def pluginVersion: String = pluginCoordinates.map(_.version).getOrElse("(from source)")

  private def requirePinnedPlugin(pin: Json): Unit = {
    ImplementationPin.requireMatch(pin, ImplementationPin.installed)
    // Checking an environment marker alone would let a separately declared
    // Maven plugin claim the wrapper's identity. Compare the loaded jar with
    // the actual plugin repository supplied by the selected wrapper.
    val matching = for {
      coordinates <- pluginCoordinates
      repository <- environmentRepository
      code <- Option(getClass.getProtectionDomain.getCodeSource)
      jar = new File(code.getLocation.toURI)
      if jar.isFile
      organization = coordinates.organization.split('.').foldLeft(repository)((dir, segment) => new File(dir, segment))
      modules = Option(organization.listFiles()).toVector.flatten.filter(_.isDirectory)
      if modules.exists { module =>
        val name = module.getName
        val expected = new File(new File(module, coordinates.version), name + "-" + coordinates.version + ".jar")
        (name == coordinates.name || name.startsWith(coordinates.name + "_")) &&
          expected.isFile && Lock.sriHash(expected) == Lock.sriHash(jar)
      }
    } yield true
    if (!matching.getOrElse(false))
      sys.error("sbt-purifier: loaded plugin bytes differ from the pinned wrapper's plugin repository; use its sbt wrapper")
  }

  /** Follow cache symlinks: Nix may supply a published Maven artifact too.
    * A classes directory belongs to a source build, not Maven resolution.
    */
  private[sbtnixbackend] def impurePluginResolution(location: File, store: File = new File("/nix/store")): Boolean =
    location.isFile && !location.getCanonicalFile.toPath.startsWith(store.getCanonicalFile.toPath)

  private def warnImpurePluginResolution(state: State): State = {
    val metaBuild = Project.extract(state).getOpt(isMetaBuild).getOrElse(false)
    val location = Option(getClass.getProtectionDomain.getCodeSource).map(s => new File(s.getLocation.toURI))
    if (!metaBuild && location.exists(impurePluginResolution(_)))
      state.log.warn(
        "sbt-purifier: the sbt-purifier plugin is being resolved impurely through Maven. " +
          "Use sbt-purifier's sbt wrapper (`nix shell github:nix-purifiers/sbt-purifier#sbt`) " +
          "for pure resolution of the plugin."
      )
    state
  }

  /** The plugin file the sbt-nix-backend `sbt` wrapper put into the global
    * plugins it starts sbt with; the wrapper names the file in this system
    * property too.
    */
  def injectedPluginFile: Option[File] =
    sys.props.get("sbt.nix.backend.inject").filter(_.nonEmpty).map(new File(_))

  private val NixOutputResolver = "sbt-nix-output"

  /** Whether an sbt binary version is sbt 2 or later. */
  private def isSbt2(sbtBinary: String): Boolean = !sbtBinary.startsWith("1.")

  /** The published module name of `id` built with Scala `scalaFull`.
    *
    * An sbt 1 plugin publishes Maven-style as <name>_<scala>_<sbt>.
    * Ivy-style, an sbt 1 plugin keeps its name, without the cross version
    * of its projectID, and carries the versions as extra attributes. An
    * sbt 2 plugin publishes as <name>_sbt2_<scala> in either style.
    */
  private def publishedName(
      id: ModuleID,
      plugin: Boolean,
      mavenStyle: Boolean,
      sbtBinary: String,
      scalaFull: String
  ): String = {
    val scalaBinary = CrossVersion.binaryScalaVersion(scalaFull)
    val crossed = CrossVersion(id.crossVersion, scalaFull, scalaBinary).fold(id.name)(f => f(id.name))
    if (!plugin) crossed
    else if (!isSbt2(sbtBinary) && !mavenStyle) id.name
    else if (!isSbt2(sbtBinary)) s"${crossed}_${scalaBinary}_$sbtBinary"
    else s"${id.name}_sbt${sbtBinary}_$scalaBinary"
  }

  /** The Scala versions the Nix build builds a project for. */
  private def lockScalaVersions: Def.Initialize[Seq[String]] = Def.setting {
    val cross = crossScalaVersions.value
    if (cross.isEmpty) Seq(scalaVersion.value) else cross
  }

  private val nixLockedNames =
    taskKey[Seq[String]]("The published names of this project for each Scala version the lock builds it for.")

  override def globalSettings: Seq[Setting[_]] = Seq(
    commands ++= Seq(sandboxBuildCommand, editorDownloadsCommand, nixPublishCommand),
    // The default lives in Global only, so a project-level append from any
    // plugin survives whatever order the plugins apply in.
    nixNetworkDependencies := Nil,
    onLoad := onLoad.value.andThen(warnImpurePluginResolution).andThen(publishCoursierCache)
  )

  /** The system property that names the coursier cache holding the locked
    * downloads, including every declared network dependency at the path
    * coursier would use. A plugin that bridges itself without depending on
    * sbt-nix-backend reads its files from there whenever the property is
    * set, in a Nix build, in the lock run and interactively.
    */
  val CoursierCacheProperty = "sbt.nix.backend.coursierCache"

  /** Sets [[CoursierCacheProperty]] once the build is loaded. Interactively
    * that means realizing the lock now, so its files exist before any task
    * reads them.
    */
  private def publishCoursierCache(state: State): State = {
    Mode.current match {
      case Mode.Interactive =>
        val extracted = Project.extract(state)
        if (extracted.getOpt(ThisBuild / nixBackend).getOrElse(false)) {
          val (next, dir) = extracted.runTask(ThisBuild / nixCoursierCache, state)
          System.setProperty(CoursierCacheProperty, dir.getAbsolutePath)
          next
        } else {
          System.clearProperty(CoursierCacheProperty)
          state
        }
      case _ =>
        System.setProperty(CoursierCacheProperty, Mode.coursierCacheFromEnv.getAbsolutePath)
        state
    }
  }

  override def buildSettings: Seq[Setting[_]] = Seq(
    nixBuildRoot := buildRootSetting.value,
    // A build kept external names its lockfile in SBT_PURIFIER_LOCKFILE,
    // which the sbt-purifier commands set from --lockfile.
    nixLockFile := sys.env
      .get(Lock.LockFileVariable)
      .filter(_.nonEmpty)
      .map(new File(_).getAbsoluteFile)
      .getOrElse(nixBuildRoot.value / Lock.FileName),
    nixBackend := {
      val log = sLog.value
      val lockFile = nixLockFile.value
      val disabled = sys.env.get("SBT_NIX_BACKEND").exists(value => value == "0" || value == "false")
      if (Mode.current == Mode.Interactive && !disabled && lockFile.isFile)
        Lock.read(lockFile).implementation.foreach(requirePinnedPlugin)
      sys.env.get("SBT_NIX_BACKEND") match {
        case Some("0") | Some("false")                      => false
        case _ if Mode.current != Mode.Interactive          => false
        case _ if !lockFile.isFile                          => false
        case _ =>
          // A lock made from another build definition would leave this
          // level unable to load, and nixLock with it; resolution goes
          // its usual way until the lock is renewed.
          val current = Lock.buildDefinitionHash(nixBuildRoot.value, Nil)
          Lock.readOption(lockFile) match {
            case Some(l) if l.definitionHash == current => true
            case _ =>
              log.warn(
                s"[sbt-nix-backend] ${lockFile.getName} was made from another build definition; " +
                  s"resolution does not read the locked downloads until `${Lock.LockCommand}` renews it"
              )
              false
          }
      }
    },
    nixFlakeRepositories := Nil,
    nixFlakeRepositoryViews := flakeRepositoryViewsSetting.value,
    nixFlakeRepositoryPaths := nixFlakeRepositoryViews.value.map { case (name, view) => name -> view.path },
    nixProvidedModules := ProjectedDependencies.merge(
      ProjectedDependencies.read(sys.env) ++ nixFlakeRepositoryViews.value.values.toVector
        .flatMap(_.catalog)
        .flatMap(ProjectedDependencies.readCatalog)
    ),
    nixSbtJavaOptions := Seq("-Xss4m", "-Xmx2g"),
    nixLauncherUrl := {
      val v = sbtVersion.value
      BootRepositories.launcher(v)
    },
    nixCoursierCache := Compat.uncached(coursierCacheTask.value),
    nixNixpkgs := sys.props.get("sbt.nix.backend.nixpkgs").filter(_.nonEmpty).getOrElse("flake:nixpkgs"),
    nixLock := Compat.uncached(lockTask(force = false, updateFlakes = false).value),
    nixRelock := Compat.uncached(lockTask(force = true, updateFlakes = false).value),
    nixLockUpdate := Compat.uncached(lockTask(force = true, updateFlakes = true).value),
    nixImplementationRelock := Compat.uncached(implementationRelockTask.value)
  ) ++ ResolutionSupport.buildSettings

  override def projectSettings: Seq[Setting[_]] = passOnSettings ++ Compat.ivyMirrorSettings(ivyMirror) ++ ResolutionSupport.projectSettings ++ Seq(
    nixPackaged := !(publish / skip).value,
    libraryDependencies := {
      val supplied = (ThisBuild / nixProvidedModules).value
      val full = scalaVersion.value
      val binary = scalaBinaryVersion.value
      libraryDependencies.value.map(module => ProjectedDependencies.select(module, full, binary, supplied))
    },
    dependencyOverrides := {
      val supplied = (ThisBuild / nixProvidedModules).value
      val full = scalaVersion.value
      val binary = scalaBinaryVersion.value
      dependencyOverrides.value.map(module =>
        ProjectedDependencies.select(module, full, binary, supplied)
      ) ++ supplied
    },
    pomPostProcess := {
      val previous = pomPostProcess.value
      val supplied = (ThisBuild / nixProvidedModules).value
      node => ProjectedDependencies.pom(previous(node), supplied)
    },

    // The lock describes every Scala version a project is built for, as the
    // Nix build builds each, so `++` leaves it as it is: it names a project
    // by its first Scala version and hashes its names for all of them.
    nixLockedProject := Compat.uncached {
      if (!nixPackaged.value) None
      else {
        val id = projectID.value
        val name = publishedName(id, sbtPlugin.value, publishMavenStyle.value, sbtBinaryVersion.value, lockScalaVersions.value.head)
        Some(LockedProject(thisProject.value.id, id.organization, name))
      }
    },
    nixLockedNames := Compat.uncached {
      if (!nixPackaged.value) Nil
      else {
        val id = projectID.value
        val versions = lockScalaVersions.value
        // The lockfile keys the artifacts of a project by variant, so two
        // Scala versions with one variant name cannot both be locked.
        versions.groupBy(v => NixArtifacts.variantName(id.crossVersion, sbtPlugin.value, sbtBinaryVersion.value, v)).foreach {
          case (name, vs) if vs.size > 1 =>
            sys.error(NixArtifacts.conflict(name, vs) + s" (project ${thisProject.value.id})")
          case _ =>
        }
        versions.map(v => publishedName(id, sbtPlugin.value, publishMavenStyle.value, sbtBinaryVersion.value, v))
      }
    },
    resolvers := {
      val flake = (ThisBuild / nixFlakeRepositoryPaths).value.toVector.sortBy(_._1).flatMap { case (n, dir) =>
        IvyLayout.resolvers("nix-flake-" + n, dir)
      }
      val supplied = sys.env.get("SBT_PURIFIER_PROVIDED_REPOSITORY").filter(_.nonEmpty).toSeq.map { path =>
        MavenRepository("nix-project-dependencies", new File(path).toURI.toString)
      }
      supplied ++ flake ++ resolvers.value.filterNot(r => flake.exists(_.name == r.name))
    },
    otherResolvers ++= {
      Mode.current match {
        case Mode.Interactive => Nil
        case _ =>
          // A project publishes into its output in the layout it publishes
          // in anywhere: Maven, or with publishMavenStyle := false the
          // standard Ivy layout.
          val dir = Mode.output / thisProject.value.id / "artifacts"
          Seq(if (publishMavenStyle.value) Compat.mavenCache(NixOutputResolver, dir) else IvyLayout.ivy(NixOutputResolver, dir))
      }
    },
    nixPublishArtifacts / publishConfiguration := Compat.uncached {
      // The stock publishConfiguration fails when publishTo is unset, so
      // this one is built the same way with the output resolver named.
      Compat.publishConfig(
        publishMavenStyle.value,
        Classpaths.deliverPattern(crossTarget.value),
        if (isSnapshot.value) "integration" else "release",
        ivyConfigurations.value.map(c => sbt.librarymanagement.ConfigRef(c.name)).toVector,
        Compat.publishedArtifacts.value,
        (publish / checksums).value.toVector,
        NixOutputResolver,
        ivyLoggingLevel.value,
        overwrite = true
      )
    },
    // The sandbox publishes every packaged project and its internal
    // dependencies, whatever `publish / skip` says.
    nixPublishArtifacts / skip := false,
    nixPublishArtifacts := Compat.uncached {
      Compat
        .publishToOutput(
          nixPublishArtifacts / publishConfiguration,
          nixPublishArtifacts / skip,
          Def.setting(Mode.output / thisProject.value.id / "artifacts")
        )
        .value
      val dir = Mode.output / thisProject.value.id / "artifacts"
      val name = NixArtifacts.variantName(projectID.value.crossVersion, sbtPlugin.value, (pluginCrossBuild / sbtBinaryVersion).value, scalaVersion.value)
      NixArtifacts.record(dir, name, scalaVersion.value, Compat.publishedArtifacts.value)
      dir
    },
    nixPublishRepository := Compat.uncached(publishRepositoryTask.value),
    nixPackagedArtifacts := Compat.uncached {
      val out = nixBuildPackage("out").value
      val id = thisProject.value.id
      val version = scalaVersion.value
      val project = Lock.read((ThisBuild / nixLockFile).value).projects.find(_.id == id)
      val variant = project
        .flatMap(_.variants.find(_.scalaVersion == version))
        .getOrElse(
          sys.error(
            s"sbt-nix-backend: the lockfile builds $id for Scala ${project.fold("")(_.variants.map(_.scalaVersion).mkString(", "))}, " +
              s"not $version"
          )
        )
      NixArtifacts.files(out, variant)
    },
    nixPublication := Compat.uncached(nixBuildPackage("publication").value),
    nixRuntimeRepository := Compat.uncached(nixBuildPackage("repository").value),
    // javadoc writes the time it ran into every page, so a Nix build of a
    // Java project would differ from a rebuild of it; it leaves the time
    // out with -notimestamp.
    Compile / doc / javacOptions ++= (if (Mode.current == Mode.Interactive) Nil else Seq("-notimestamp")),
    update := Compat.uncached {
      val result = update.result.value
      val log = streams.value.log
      Compat.toEither(result) match {
        case Right(report) => report
        case Left(cause) =>
          if (Mode.current == Mode.Sandbox)
            log.error(
              s"[sbt-nix-backend] the Nix build reads only the downloads ${Lock.FileName} locks, and it lacks one " +
                s"this resolution needs; run `${Lock.LockCommand}`."
            )
          else if ((ThisBuild / nixBackend).value)
            log.error(
              s"[sbt-nix-backend] resolution reads only the downloads ${(ThisBuild / nixLockFile).value.getName} locks. " +
                s"If the build's dependencies changed, run `${Lock.LockCommand}`; SBT_NIX_BACKEND=0 resolves without the lock."
            )
          throw cause
      }
    }
  )

  /** The concrete build's root. sbt runs every level of a build in one JVM
    * started in the concrete build's root, and level k lives at
    * `<root>/project` repeated k times; a meta-build checks that its own
    * base has that shape before it takes the working directory as the root.
    */
  private def buildRootSetting: Def.Initialize[File] = Def.setting {
    val base = (ThisBuild / baseDirectory).value.getCanonicalFile
    if (!(LocalRootProject / isMetaBuild).value) base
    else {
      val root = new File(sys.props("user.dir")).getCanonicalFile
      val rel = root.toPath.relativize(base.toPath).toString.replace(File.separatorChar, '/')
      if (rel.isEmpty || rel.split('/').exists(_ != "project"))
        sys.error(s"sbt-nix-backend: the meta-build at $base is not a project/ directory under the build root $root")
      root
    }
  }

  /** Builds `output` of this project's package in the Nix sandbox: the
    * lockfile is brought up to date first, and the build runs
    * buildSbtProject from the plugin's copy of the Nix library.
    */
  private def nixBuildPackage(output: String): Def.Initialize[Task[File]] = Def.task(Compat.uncached {
    val log = streams.value.log
    if (Mode.current != Mode.Interactive)
      sys.error("sbt-nix-backend: nixPackagedArtifacts runs only outside the sandbox")
    val lockFile = (ThisBuild / nixLock).value
    val id = thisProject.value.id
    val lock = Lock.read(lockFile)
    if (!lock.projects.exists(_.id == id))
      sys.error(s"sbt-nix-backend: $lockFile builds no project $id (nixPackaged is false for it)")
    val base = (ThisBuild / nixBuildRoot).value / "target" / "sbt-nix-backend"
    val lib = NixLibrary.write(base / "nix-lib")
    // The repository this sbt takes the plugin from, which the Nix build
    // supplies too. The library's own default builds the plugin from the
    // source around the library, which the copy written above lacks.
    val repository = lock.plugin.map(_.via) match {
      case None => ""
      case Some(LockedPlugin.GlobalPlugin) =>
        sys.props.get("sbt.nix.backend.repository").filter(_.nonEmpty).getOrElse(
          sys.error("sbt-nix-backend: the lockfile injects the plugin; run sbt through the sbt-nix-backend wrapper")
        )
      case Some(_) =>
        environmentRepository.map(_.getPath).getOrElse(
          sys.error(s"sbt-nix-backend: the lockfile takes the plugin from ${LockedPlugin.RepositoryVariable}, which is not set")
        )
    }
    log.info(s"[sbt-nix-backend] building $id ($output) in the Nix sandbox")
    new Nix(line => log.info("[nix] " + line)).buildFile(
      new File(lib, "package.nix"),
      output,
      Seq(
        "nixpkgs" -> (ThisBuild / nixNixpkgs).value,
        "src" -> (ThisBuild / nixBuildRoot).value.getAbsolutePath,
        "project" -> id,
        "lockFile" -> (ThisBuild / nixLockFile).value.getAbsolutePath,
        "pluginRepository" -> repository
      ),
      base / "gcroots" / s"package-$id-$output"
    )
  })

  /** How a project that resolves with Ivy reads its downloads (see
    * IvyMirror.Setup): through the coursier cache of the Nix build, the
    * lock run's mirror server, or interactively the cache built from the
    * lock. Interactively without a usable lock it resolves the usual way,
    * with an Ivy home of its own. None for a project that resolves with
    * coursier.
    */
  private def ivyMirror: Def.Initialize[Task[Option[IvyMirror.Setup]]] = Def.taskDyn {
    lazy val homes = cacheRoot((ThisBuild / nixBuildRoot).value) / "ivy-homes"
    if (!Compat.usesIvyResolution.value) Def.task(Option.empty[IvyMirror.Setup])
    else
      Mode.current match {
        case Mode.Sandbox => Def.task(Option(IvyMirror.Setup(Some(Mode.coursierCacheFromEnv.toURI.toString), homes, "")))
        case Mode.Lock =>
          val base = sys.env.getOrElse(IvyMirror.Variable, sys.error(s"sbt-nix-backend: ${IvyMirror.Variable} is not set"))
          Def.task(Option(IvyMirror.Setup(Some(base), homes, "")))
        case Mode.Interactive =>
          if ((ThisBuild / nixBackend).value) Def.task {
            val lock = Files.readAllBytes((ThisBuild / nixLockFile).value.toPath)
            val key = java.security.MessageDigest.getInstance("SHA-256").digest(lock).map(b => f"${b & 0xff}%02x").mkString
            Option(IvyMirror.Setup(Some((ThisBuild / nixCoursierCache).value.toURI.toString), homes, key))
          }
          else Def.task(Option(IvyMirror.Setup(None, homes, "unlocked")))
      }
  }

  /** Resolution settings, caches and GC roots must survive every project clean. */
  private def cacheRoot(root: File): File = {
    val dir = root / ".sbt-purifier"
    Files.createDirectories(dir.toPath)
    try Files.write((dir / ".gitignore").toPath, "*\n".getBytes(StandardCharsets.UTF_8), java.nio.file.StandardOpenOption.CREATE_NEW)
    catch { case _: java.nio.file.FileAlreadyExistsException => () }
    dir
  }

  private val coursierCacheLock = new Object

  private def coursierCacheTask: Def.Initialize[Task[File]] = Def.task(Compat.uncached {
    val log = streams.value.log
    val lockFile = nixLockFile.value
    val base = cacheRoot((ThisBuild / nixBuildRoot).value)
    val dir = base / "coursier-cache"
    if (!lockFile.isFile) sys.error(s"sbt-nix-backend: no lock at $lockFile; run `${Lock.LockCommand}`")
    coursierCacheLock.synchronized {
      val marker = dir / ".sbt-nix-backend-lock"
      val lockHash = Lock.sriHash(lockFile)
      val upToDate = marker.isFile && new String(Files.readAllBytes(marker.toPath), StandardCharsets.UTF_8) == lockHash
      if (!upToDate) {
        log.info(s"[sbt-nix-backend] realizing ${lockFile.getName} through Nix")
        val lib = NixLibrary.write(base / "nix-lib")
        val manifest = new Nix(line => log.info("[nix] " + line)).realizeLock(lockFile, lib, base / "gcroots" / "coursier-cache")
        Nix.linkCache(dir, manifest)
        Files.write(marker.toPath, lockHash.getBytes(StandardCharsets.UTF_8))
      }
      dir
    }
  })

  /** The flake repositories' store paths. In the sandbox and the lock run
    * they come from the environment. Interactively, each one is locked
    * (reusing the lock file's pin while the declared reference is the
    * same) and built once, with a GC root and a record of the pin, so
    * later loads read the record rather than calling Nix.
    */
  private def flakeRepositoryViewsSetting: Def.Initialize[Map[String, Nix.RepositoryView]] = Def.setting {
    val declared = nixFlakeRepositories.value
    Mode.current match {
      case Mode.Interactive if declared.nonEmpty =>
        val lock = Lock.readOption(nixLockFile.value)
        val base = cacheRoot((ThisBuild / nixBuildRoot).value) / "flake-repositories"
        lazy val nix = new Nix(_ => ())
        declared.map { r =>
          val locked = lock
            .flatMap(_.flakeRepositories.find(l => l.name == r.name && l.ref == r.ref))
            .map(_.locked)
            .getOrElse(nix.lockFlakeRef(r.flakeRef))
          r.name -> realizeFlakeRepository(nix, base, r.name, locked, r.attribute)
        }.toMap
      case Mode.Interactive => Map.empty
      case _                =>
        val catalogs = sys.env
          .get("SBT_NIX_FLAKE_REPOSITORY_CATALOGS")
          .filter(_.trim.nonEmpty)
          .map(json =>
            Json.parse(json).asObject.fields.map { case (name, value) => name -> new File(value.asString) }.toMap
          )
          .getOrElse(Map.empty[String, File])
        Mode.flakeRepositoryPathsFromEnv.map { case (name, path) =>
          name -> Nix.RepositoryView(path, catalogs.get(name))
        }
    }
  }

  private def realizeFlakeRepository(
      nix: => Nix,
      base: File,
      name: String,
      locked: String,
      attribute: String
  ): Nix.RepositoryView = {
    val record = base / (name + ".json")
    val root = base / (name + "-gcroot")
    val key = Json.obj("locked" -> Json.str(locked), "attribute" -> Json.str(attribute), "viewVersion" -> Json.num(2))
    val recorded =
      if (record.isFile) {
        val o = Json.parse(new String(Files.readAllBytes(record.toPath), StandardCharsets.UTF_8)).asObject
        val path = new File(o("path").asString)
        val catalog = o.get("catalog").map(value => new File(value.asString))
        if (
          o.get("viewVersion").exists(_ == key("viewVersion")) && o("locked") == key("locked") &&
          o("attribute") == key("attribute") && path.isDirectory && catalog.forall(_.isFile)
        )
          Some(Nix.RepositoryView(path, catalog))
        else None
      } else None
    recorded.getOrElse {
      val view = nix.projectRepository(locked, attribute, NixLibrary.write(base / "nix-lib"), root)
      base.mkdirs()
      Files.write(
        record.toPath,
        Json
          .Obj(
            key.fields ++ Vector("path" -> Json.str(view.path.getAbsolutePath)) ++
              view.catalog.toVector.map(file => "catalog" -> Json.str(file.getAbsolutePath))
          )
          .render
          .getBytes(StandardCharsets.UTF_8)
      )
      view
    }
  }

  private val allProjects = ScopeFilter(inAnyProject)

  // The tasks below read files and run Nix, so sbt 2 must not reuse a
  // result it cached for the same inputs: a lockfile, a store path or the
  // plugin itself may have changed since.
  private def lockInputs(updateFlakes: Boolean): Def.Initialize[Task[LockGeneration.Inputs]] = Def.task(Compat.uncached {
    val root = (ThisBuild / nixBuildRoot).value
    val projects = nixLockedProject.all(allProjects).value.flatten.toVector.sortBy(_.id)
    val preserveBuildDirectory = thisProject.all(allProjects).value.exists(p => p.base == root && (Compat.implicitRoot(p) || p.id == root.getName))
    val crossNames = nixLockedNames.all(allProjects).value.flatten.distinct.sorted
    val urls = (nixNetworkDependencies.all(allProjects).value.flatten ++ (ThisBuild / nixNetworkDependencies).value).distinct.sorted.toVector
    val declared = (ThisBuild / nixFlakeRepositories).value.toVector
    val existing = Lock.readOption((ThisBuild / nixLockFile).value)
    val nix = new Nix(_ => ())
    val lockedRepos = declared.map { r =>
      val kept =
        if (updateFlakes) None
        else existing.flatMap(_.flakeRepositories.find(l => l.name == r.name && l.ref == r.ref))
      kept.getOrElse(LockedFlakeRepository(r.name, r.ref, nix.lockFlakeRef(r.flakeRef), r.attribute))
    }
    val repoBase = cacheRoot((ThisBuild / nixBuildRoot).value) / "flake-repositories"
    val repoViews =
        lockedRepos.map(r => r.name -> realizeFlakeRepository(nix, repoBase, r.name, r.locked, r.attribute)).toMap
      val repoPaths = repoViews.map { case (name, view) => name -> view.path }
      val repoCatalogs = repoViews.toVector.flatMap { case (name, view) => view.catalog.map(name -> _) }.toMap
      val injectFile = injectedPluginFile
    val plugin = lockedPlugin
    if (injectFile.isDefined && plugin.isEmpty)
      sys.error("sbt-nix-backend: the plugin was injected but its jar names no coordinates")
    val javaOptions = (ThisBuild / nixSbtJavaOptions).value.toVector
    val launcherUrl = (ThisBuild / nixLauncherUrl).value
    val hash = Lock.buildDefinitionHash(
      root,
      Seq("plugin=" + plugin.fold("none")(p => s"${p.organization}:${p.name}:${p.version} via ${p.via}"), "launcher=" + launcherUrl) ++
        javaOptions.map("javaOption=" + _) ++ urls.map("url=" + _) ++
        lockedRepos.map(r => s"flake=${r.name}=${r.locked}#${r.attribute}") ++
        projects.map(p => s"project=${p.id}=${p.organization}:${p.name}") ++
        crossNames.map("published=" + _)
    )
    LockGeneration.Inputs(
      buildRoot = root,
      workDir = ((ThisBuild / nixBuildRoot).value / "target") / "sbt-nix-backend" / "lock-run",
      sbtVersion = sbtVersion.value,
      launcherUrl = launcherUrl,
      javaOptions = javaOptions,
      projects = projects,
      networkUrls = urls,
      flakeRepositories = lockedRepos,
      flakeRepositoryPaths = repoPaths,
      flakeRepositoryCatalogs = repoCatalogs,
      preserveBuildDirectory = preserveBuildDirectory,
      injectedPluginFile = injectFile,
      plugin = plugin,
      inputsHash = hash
    )
  })

  private def implementationRelockTask: Def.Initialize[Task[File]] = Def.task(Compat.uncached {
    val in = lockInputs(updateFlakes = false).value
    val file = (ThisBuild / nixLockFile).value
    if (Mode.current != Mode.Interactive) sys.error("sbt-purifier: implementation updates run outside the sandbox")
    val previous = Lock.read(file)
    if (previous.definitionHash != Lock.buildDefinitionHash(in.buildRoot, Nil) || previous.sbtVersion != in.sbtVersion ||
        previous.javaVersion != Lock.javaMajor(System.getProperty("java.version")))
      sys.error("sbt-purifier: the project or toolchain changed; lock its dependencies before updating the purifier")
    val pin = ImplementationPin.installed.getOrElse(sys.error("sbt-purifier: the selected implementation has no portable identity"))
    // Do not run a fresh dependency resolver: dynamic versions and snapshots
    // must retain their existing captured bytes during an implementation update.
    previous.copy(plugin = in.plugin, inputsHash = in.inputsHash, implementation = Some(pin)).write(file)
    file
  })

  private def lockTask(force: Boolean, updateFlakes: Boolean): Def.Initialize[Task[File]] = Def.task(Compat.uncached {
    val log = streams.value.log
    val in = lockInputs(updateFlakes).value
    val lockFile = (ThisBuild / nixLockFile).value
    if (Mode.current != Mode.Interactive) sys.error("sbt-nix-backend: nixLock runs only outside the sandbox")
    val existing = Lock.readOption(lockFile)
    val javaMajor = Lock.javaMajor(System.getProperty("java.version"))
    val fresh = existing.exists { l =>
      l.inputsHash == in.inputsHash && l.javaVersion == javaMajor && l.sbtVersion == in.sbtVersion
    }
    if (!force && fresh) {
      log.info(s"[sbt-nix-backend] $lockFile is up to date")
    } else {
      val pin = existing.flatMap(_.implementation).orElse {
        if (existing.isDefined) None
        else {
          val active = ImplementationPin.installed
          if (active.isEmpty && sys.env.get("SBT_PURIFIER_IMPLEMENTATION_CONFIG").exists(_.nonEmpty))
            sys.error("sbt-purifier: this installation has no portable source identity; lock with a committed, published purifier revision")
          if (active.isEmpty)
            log.warn("sbt-purifier: this lock does not pin the purifier implementation; use the sbt wrapper and explicitly run `sbt-purifier self-update --to installed`")
          active
        }
      }
      pin.foreach(requirePinnedPlugin)
      val lock = LockGeneration.run(in, log.info(_), log.warn(_)).copy(implementation = pin)
      lock.write(lockFile)
      log.info(s"[sbt-nix-backend] wrote $lockFile (${lock.artifacts.size} artifacts)")
    }
    lockFile
  })

  private def publishRepositoryTask: Def.Initialize[Task[File]] = Def.task(Compat.uncached {
    val log = streams.value.log
    val id = thisProject.value.id
    val repository = Mode.output / id / "repository"
    // A Maven-style project takes its coordinates from the POM sbt made for
    // this variant, which carries every naming convention (cross versions,
    // the sbt plugin suffix) applied. An Ivy-style project has its published
    // name for the Scala version of this build, and an Ivy-style sbt 1
    // plugin carries its Scala and sbt versions as extra attributes.
    val coordinates =
      if (publishMavenStyle.value) {
        val pom = Compat.publishedArtifacts.value
          .filter { case (a, _) => a.`type` == "pom" }
          // Prefer the standard Maven POM when a plugin also publishes
          // the legacy sbt filename for older resolvers.
          .sortBy { case (a, _) => if (a.name == moduleName.value) 1 else 0 }
          .headOption.map(_._2)
          .getOrElse(sys.error(s"sbt-nix-backend: $id packages no POM"))
        RuntimeRepository.pomCoordinates(pom)
      } else {
        // The module of the Scala version this build is for.
        val module = projectID.value
        val name = publishedName(module, sbtPlugin.value, false, sbtBinaryVersion.value, scalaVersion.value)
        val attributes =
          if (sbtPlugin.value && !isSbt2(sbtBinaryVersion.value))
            IvyLayout.pluginAttributes(scalaBinaryVersion.value, sbtBinaryVersion.value)
          else Map.empty[String, String]
        RuntimeRepository.Coordinates(module.organization, name, module.revision, attributes)
      }
    // A project that resolves through an Ivy settings file adds its
    // resolvers, which coursier cannot read from the file.
    val copied = ResolutionSupport.runtimeCompletion(ivyMirror).value.apply(repository, coordinates, log)
    val mainClassName = (Compile / mainClass).?.value.flatten
    val module = Json.obj(
      "organization" -> Json.str(coordinates.organization),
      "name" -> Json.str(coordinates.name),
      "version" -> Json.str(coordinates.version),
      "layout" -> Json.str(if (publishMavenStyle.value) "maven" else "ivy"),
      "attributes" -> Json.Obj(coordinates.attributes.toVector.sorted.map { case (k, v) => k -> Json.str(v) }),
      "mainClass" -> mainClassName.fold[Json](Json.Null)(Json.str)
    )
    // A cross-built project publishes one module per Scala version into
    // the same repository; the metadata lists each.
    val metaFile = repository / RepositoryMetadata
    val earlier =
      if (metaFile.isFile)
        Json.parse(new String(Files.readAllBytes(metaFile.toPath), StandardCharsets.UTF_8)).asObject("modules").asArray
      else Vector.empty
    val modules = (earlier.filterNot(_.asObject("name") == module("name")) :+ module).sortBy(_.asObject("name").asString)
    Files.write(metaFile.toPath, (Json.obj("modules" -> Json.Arr(modules)).render + "\n").getBytes(StandardCharsets.UTF_8))
    log.info(s"[sbt-nix-backend] $id: repository holds ${copied.size} files besides the build's own artifacts")
    repository
  })

  /** The projects `ref` depends on for compile or runtime, transitively. */
  private def runtimeProjectClosure(structure: sbt.internal.BuildStructure, ref: ProjectRef): Vector[ProjectRef] = {
    def runtimeEdge(config: Option[String]): Boolean = config match {
      case None => true
      case Some(c) =>
        c.split(';').exists { mapping =>
          val from = mapping.split("->").head.split(',').map(_.trim)
          from.exists(f => f == "compile" || f == "runtime" || f == "default" || f == "*")
        }
    }
    val seen = scala.collection.mutable.LinkedHashSet.empty[ProjectRef]
    def visit(r: ProjectRef): Unit =
      if (seen.add(r)) {
        val project = Project.getProject(r, structure).getOrElse(sys.error(s"sbt-nix-backend: no project $r"))
        project.dependencies.filter(d => runtimeEdge(d.configuration)).foreach(d => visit(d.project))
      }
    visit(ref)
    seen.toVector
  }

  /** `nixSandboxBuild [--test] [--test-compile] <project>...`: what a Nix
    * build runs (and the lock run, with --test-compile). For each project
    * it publishes the packaged artifacts to `$$SBT_NIX_OUTPUT/<id>/artifacts`
    * and writes `$$SBT_NIX_OUTPUT/<id>/repository`.
    *
    * A project with several `crossScalaVersions` is built for each of them,
    * as sbt's `+` prefix does: the command queues `++<version>` and itself
    * (with `--scala-version <version>`) for every version, so the outputs
    * hold every published variant.
    */
  /** `nixPublish`: `publish`, with the artifacts the Nix sandbox built.
    *
    * For one run, every project the plugin applies to has
    * `publish / packagedArtifacts` set to its `nixPackagedArtifacts`, and
    * `publish` runs from the current project, aggregated projects included.
    * Everything else about the publish is sbt's own: `publishTo`,
    * credentials, the publishing style and the checksums. Nothing is
    * packaged or compiled outside the sandbox. `+nixPublish` publishes each
    * Scala version.
    */
  lazy val nixPublishCommand: Command =
    Command.command(
      "nixPublish",
      Help.more("nixPublish", "publish, with the artifacts built by Nix in its sandbox from the lockfile.")
    ) { state0 =>
      if (Mode.current != Mode.Interactive) sys.error("sbt-nix-backend: nixPublish runs outside the sandbox")
      val extracted = Project.extract(state0)
      val lock = Lock.read(extracted.get(ThisBuild / nixLockFile))
      if (lock.flakeRepositories.isEmpty) {
        val refs = extracted.structure.allProjectRefs.filter(ref => Compat.hasTask(extracted, ref / nixPackaged))
        val state = Compat.appendWithSession(extracted, refs.flatMap(Compat.publishFromNix), state0)
        val after = Project.extract(state)
        after.runAggregated(after.currentRef / publish, state)
      } else {
        def aggregated(ref: ProjectRef, seen: Set[ProjectRef]): Vector[ProjectRef] =
          if (seen(ref)) Vector.empty
          else Vector(ref) ++ extracted.get(ref / thisProject).aggregate.toVector.flatMap(child => aggregated(child, seen + ref))
        val refs = aggregated(extracted.currentRef, Set.empty).distinct.filter { ref =>
          Compat.hasTask(extracted, ref / nixPackaged) && extracted.runTask(ref / nixPackaged, state0)._2
        }
        val published = scala.collection.mutable.Set.empty[(String, String, String)]
        refs.foreach { ref =>
          val (builtState, publication) = extracted.runTask(ref / nixPublication, state0)
          val mavenStyle = extracted.get(ref / publishMavenStyle)
          ProjectedPublication.modules(publication, mavenStyle).foreach { module =>
            val coordinate = (module.id.organization, module.id.name, module.id.revision)
            if (!published(coordinate)) {
              val state = Compat.appendWithSession(
                Project.extract(builtState), Compat.publishProjection(ref, module), builtState
              )
              Project.extract(state).runTask(ref / publish, state)
              published += coordinate
            }
          }
        }
      }
      state0
    }

  /** `nixEditorDownloads`: what an editor resolves through sbt's build
    * server, so interactive mode has it in the locked downloads. The lock
    * run runs it before nixSandboxBuild, at the Scala version each project
    * has by default, which is the version an editor imports. It resolves
    * the sources and javadoc jars of every project's dependencies
    * (`updateClassifiers`, which a build server's `dependencySources`
    * request runs), unpublished projects included, and the sources of sbt
    * and the build's plugins (`updateSbtClassifiers`), which an editor
    * reads for the build's own files.
    */
  lazy val editorDownloadsCommand: Command =
    Command.command("nixEditorDownloads") { state0 =>
      if (Mode.current != Mode.Lock) sys.error("sbt-nix-backend: nixEditorDownloads runs in nixLock")
      val extracted = Project.extract(state0)
      var state = state0
      extracted.structure.allProjectRefs.foreach { ref =>
        state = Compat.runEditorTask(extracted, ref / updateClassifiers, state)._1
      }
      Compat.runEditorTask(extracted, updateSbtClassifiers, state)._1
    }

  lazy val sandboxBuildCommand: Command =
    Command.args("nixSandboxBuild", "[--test] [--test-compile] <project>...") { (state0, args) =>
      if (Mode.current == Mode.Interactive)
        sys.error("sbt-nix-backend: nixSandboxBuild runs inside Nix builds and nixLock; set SBT_NIX_MODE")
      val pinned = args.sliding(2).collectFirst { case Seq("--scala-version", v) => v }
      val rest = pinned.fold(args)(v => args.filterNot(a => a == "--scala-version" || a == v))
      val (flags, ids) = rest.partition(_.startsWith("--"))
      flags.filterNot(Set("--test", "--test-compile")).foreach(f => sys.error(s"nixSandboxBuild: unknown flag $f"))
      val extracted = Project.extract(state0)
      val structure = extracted.structure
      val known = structure.allProjectRefs.map(_.project).toSet
      ids.filterNot(known).foreach(id => sys.error(s"nixSandboxBuild: no project $id"))
      def crossVersions(id: String): Seq[String] =
        extracted.getOpt(ProjectRef(structure.root, id) / crossScalaVersions).getOrElse(Nil)
      val versions = ids.flatMap(crossVersions).distinct
      if (pinned.isEmpty && versions.size > 1)
        versions.foldRight(state0) { (v, s) =>
          val forVersion = ids.filter(id => crossVersions(id).contains(v))
          if (forVersion.isEmpty) s
          else s"++$v" :: (Seq("nixSandboxBuild", "--scala-version", v) ++ flags ++ forVersion).mkString(" ") :: s
        }
      else sandboxBuild(state0, extracted, flags, ids)
    }

  private def sandboxBuild(state0: State, extracted: Extracted, flags: Seq[String], ids: Seq[String]): State = {
    val structure = extracted.structure
    var state = state0
    def run[T](key: TaskKey[T]): T = {
      val (s, v) = extracted.runTask(key, state)
      state = s
      v
    }
    ids.foreach { id =>
      val ref = ProjectRef(structure.root, id)
      if (flags.contains("--test")) state = Compat.runTests(extracted, ref, state)
      if (flags.contains("--test-compile")) run(ref / Test / compile)
      val closure = runtimeProjectClosure(structure, ref)
      closure.foreach(r => run(r / nixPublishArtifacts))
      val repository = Mode.output / id / "repository"
      closure.foreach(r => IO.copyDirectory(Mode.output / r.project / "artifacts", repository, overwrite = true))
      // The manifest and the links of each project describe the output of
      // that project, not the repository.
      IO.delete(repository / NixArtifacts.FileName)
      IO.delete(repository / "nix-support")
      run(ref / nixPublishRepository)
    }
    state
  }
}
