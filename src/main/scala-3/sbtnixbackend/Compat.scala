package sbtnixbackend

import java.io.File

import sbt.*
import sbt.Keys.*

/** The sbt 2 side of the sbt APIs that differ between sbt 1 and sbt 2; src/main/scala-2.12 holds the sbt 1 side.
  */
object Compat {
  def runEditorTask[A](extracted: Extracted, key: TaskKey[A], state: State): (State, A) =
    extracted.runTask(key, state)

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
    Artifact(name, tpe, ext, classifier, configurations, url.map(_.toURI), attributes, checksum, insecure)

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

  /** The artifacts `publish` would publish, as files: sbt 2 packages them as virtual file references, which the file
    * converter resolves.
    */
  def publishedArtifacts: Def.Initialize[Task[Vector[(Artifact, File)]]] = Def.task {
    val converter = fileConverter.value
    (publish / packagedArtifacts).value.toVector.map { case (a, ref) => a -> converter.toPath(ref).toFile }
  }

  /** The value of a task result, or its failure. */
  def toEither[A](result: Result[A]): Either[Incomplete, A] = result match {
    case Result.Value(a)   => Right(a)
    case Result.Inc(cause) => Left(cause)
  }

  /** Runs the tests of `ref`; `test` is an input task in sbt 2. */
  def runTests(extracted: Extracted, ref: ProjectRef, state: State): State =
    extracted.runInputTask(ref / Test / test, "", state)._1

  /** Whether this project resolves its dependencies with Apache Ivy; sbt 2 resolves with coursier only.
    */
  def usesIvyResolution: Def.Initialize[Boolean] = Def.setting(false)

  /** Publishes this project into the Nix output `dir` with `config`. A Maven-style project publishes through sbt's
    * publisher. sbt 2 publishes an Ivy-style project without Apache Ivy, which has no resolution of the project to
    * deliver an ivy.xml from, so this writes the standard Ivy layout the way sbt 2's own publishing to a file
    * repository does: an ivy.xml from lm-coursier, and each artifact at `<type>s/<module>(-<classifier>).<ext>` with
    * its checksums.
    */
  def publishToOutput(
      config: TaskKey[PublishConfiguration],
      skipKey: TaskKey[Boolean],
      dir: Def.Initialize[File]
  ): Def.Initialize[Task[Unit]] =
    Def.taskDyn {
      if (publishMavenStyle.value) Classpaths.publishOrSkip(config, skipKey)
      else
        Def.task {
          val project = csrProject.value.withPublications(csrPublications.value)
          val conf = config.value
          val attributes = project.module.attributes
          val crossPath =
            attributes.get("scalaVersion").map("scala_" + _).toSeq ++ attributes.get("sbtVersion").map("sbt_" + _)
          val module = project.module.name.value
          val moduleDir =
            crossPath.foldLeft(dir.value / project.module.organization.value / module)(_ / _) / project.version
          def withChecksums(file: File): Unit =
            conf.checksums.foreach { algorithm =>
              val digest =
                java.security.MessageDigest.getInstance(if (algorithm == "sha1") "SHA-1" else algorithm.toUpperCase)
              val hex = digest.digest(IO.readBytes(file)).map(b => f"${b & 0xff}%02x").mkString
              IO.write(new File(file.getPath + "." + algorithm), hex)
            }
          val ivyXml = moduleDir / "ivys" / "ivy.xml"
          IO.write(ivyXml, lmcoursier.IvyXml(project, Nil, Nil))
          withChecksums(ivyXml)
          conf.artifacts.foreach { (artifact, file) =>
            val target = moduleDir / IvyLayout.typeDirectory(artifact.`type`) /
              (module + artifact.classifier.fold("")("-" + _) + "." + artifact.extension)
            IO.copyFile(file, target)
            withChecksums(target)
          }
        }
    }

  /** `ref / publish / packagedArtifacts` as the files the Nix build made, as the virtual file references sbt 2
    * publishes from.
    */
  def publishProjection(ref: ProjectRef, module: ProjectedPublication.Module): Seq[Setting[_]] = Seq(
    ref / projectID := module.id,
    ref / publish / packagedArtifacts := Def.uncached {
      val converter = (ref / fileConverter).value
      module.artifacts.map { (artifact, file) =>
        artifact -> (converter.toVirtualFile(file.toPath): xsbti.HashedVirtualFileRef)
      }
    },
    ref / makePom := Def.uncached {
      (ref / fileConverter).value.toVirtualFile(module.pom.toPath): xsbti.HashedVirtualFileRef
    },
    ref / deliver := module.ivy,
    ref / publish / skip := false
  )

  def publishFromNix(ref: ProjectRef): Seq[Setting[?]] = Seq(
    ref / publish / packagedArtifacts := Def.uncached {
      val converter = (ref / fileConverter).value
      (ref / NixBackendPlugin.autoImport.nixPackagedArtifacts).value.map { (a, f) =>
        a -> (converter.toVirtualFile(f.toPath): xsbti.HashedVirtualFileRef)
      }
    }
  )

  /** sbt 2 reads no Ivy settings file. */
  def ivySettingsResolvers(
      mirror: Def.Initialize[Task[Option[IvyMirror.Setup]]]
  ): Def.Initialize[Task[Vector[Resolver]]] =
    Def.task(Vector.empty[Resolver])

  /** sbt 2 has no Ivy configuration to rewrite. */
  def ivyMirrorSettings(mirror: Def.Initialize[Task[Option[IvyMirror.Setup]]]): Seq[Setting[?]] = Nil

  /** sbt 2 caches task results; a task with effects outside its result (a lockfile written, a Nix build run) opts out.
    */
  inline def uncached[A](inline a: A): A = Def.uncached(a)
}
