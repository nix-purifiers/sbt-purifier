package sbtnixbackend

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, StandardCopyOption}
import java.security.MessageDigest
import java.util.Base64

/** One file in the locked coursier cache: a file entry of the purifier
  * library.
  *
  * @param path    the coursier-cache-relative path (see [[CachePaths]])
  * @param url     where the Nix side downloads it from
  * @param hash    an SRI SHA-256 hash (`sha256-<base64>`)
  * @param mirrors other URLs that serve the same file, tried after `url`
  */
final case class LockedArtifact(path: String, url: String, hash: String, mirrors: Vector[String] = Vector.empty)

/** A project of the build, as the Nix side names and builds it.
  *
  * @param id           the sbt project id (`core` in `core/compile`)
  * @param organization the published organization
  * @param name         the published artifact name for the first Scala
  *                     version, cross-version suffix included
  * @param variants     for each Scala version the project is built for,
  *                     the artifacts its package output holds, as the
  *                     build of the project in the lock run published them
  */
final case class LockedProject(
    id: String,
    organization: String,
    name: String,
    variants: Vector[NixArtifacts.Variant] = Vector.empty
)

/** A flake reference to an sbt-nix-backend repository output (feature 5).
  *
  * @param name      the resolver name the build declared it under
  * @param ref       the flake reference as the build declared it
  * @param locked    the locked flake reference (`rev` and `narHash` pinned)
  * @param attribute the attribute under `sbtRepositories.<system>`
  */
final case class LockedFlakeRepository(name: String, ref: String, locked: String, attribute: String)

/** The sbt-nix-backend plugin, when the lock run took it from a repository
  * the Nix build must supply, and how the build reached it (`via`):
  *
  *  - [[LockedPlugin.GlobalPlugin]]: the sbt wrapper loaded it as a global
  *    plugin (`sbt.global.plugins`), and the Nix build does the same.
  *  - [[LockedPlugin.Environment]]: the build's own
  *    `project/project/plugins.sbt` resolved it from the repository named by
  *    `SBT_NIX_BACKEND_REPOSITORY`, and the Nix build sets that variable.
  *
  * Either way the Nix build checks that its repository holds this version.
  * A plugin resolved from a remote repository is an ordinary locked
  * artifact and has no record here.
  */
final case class LockedPlugin(organization: String, name: String, version: String, via: String)

object LockedPlugin {
  val GlobalPlugin = "global-plugin"
  val Environment = "environment"

  /** The variable a build's `project/project/plugins.sbt` names the plugin
    * repository with.
    */
  val RepositoryVariable = "SBT_NIX_BACKEND_REPOSITORY"
}

/** The lockfile: every input a sandboxed build of this sbt build needs.
  *
  * `definitionHash` is a digest of the build definition files alone (see
  * [[Lock.buildDefinitionHash]]); every level of the build can compute it,
  * and interactive resolution reads the lock only while it matches.
  * `inputsHash` adds the inputs only the concrete build knows (the
  * plugin, declared network URLs, flake references, the projects); nixLock
  * relocks when it changes.
  */
final case class Lock(
    sbtVersion: String,
    javaVersion: String,
    launcher: String,
    javaOptions: Vector[String],
    plugin: Option[LockedPlugin],
    projects: Vector[LockedProject],
    flakeRepositories: Vector[LockedFlakeRepository],
    artifacts: Vector[LockedArtifact],
    inputsHash: String,
    definitionHash: String,
    buildDirectory: Option[String] = None,
    implementation: Option[Json] = None
) {
  def toJson: Json = {
    import Json._
    val base = obj(
      "version" -> num(Lock.FormatVersion),
      "sbtVersion" -> str(sbtVersion),
      "javaVersion" -> str(javaVersion),
      "launcher" -> str(launcher),
      "javaOptions" -> Arr(javaOptions.map(str)),
      "plugin" -> plugin.fold[Json](Null) { p =>
        obj(
          "organization" -> str(p.organization),
          "name" -> str(p.name),
          "version" -> str(p.version),
          "via" -> str(p.via)
        )
      },
      "projects" -> Arr(projects.sortBy(_.id).map { p =>
        obj(
          "id" -> str(p.id),
          "organization" -> str(p.organization),
          "name" -> str(p.name),
          "variants" -> Arr(p.variants.map(NixArtifacts.variantJson))
        )
      }),
      "flakeRepositories" -> Arr(flakeRepositories.sortBy(_.name).map { r =>
        obj(
          "name" -> str(r.name),
          "ref" -> str(r.ref),
          "locked" -> str(r.locked),
          "attribute" -> str(r.attribute)
        )
      }),
      "inputsHash" -> str(inputsHash),
      "definitionHash" -> str(definitionHash),
      "artifacts" -> Arr(artifacts.sortBy(_.path).map { a =>
        obj("path" -> str(a.path), "url" -> str(a.url), "mirrors" -> Arr(a.mirrors.map(str)), "hash" -> str(a.hash))
      })
    )
    val located = buildDirectory.fold(base)(directory => Obj(base.asObject.fields :+ ("buildDirectory" -> str(directory))))
    implementation.fold(located)(pin => Obj(located.asObject.fields :+ ("implementation" -> pin)))
  }

  /** The text of the lockfile, with its content hash (see [[Lock.parse]]). */
  def render: String = Lock.withContentHash(toJson.render + "\n")

  def write(file: File): Unit = {
    Option(file.getParentFile).foreach(_.mkdirs())
    val tmp = new File(file.getPath + ".tmp")
    Files.write(tmp.toPath, render.getBytes(StandardCharsets.UTF_8))
    Files.move(tmp.toPath, file.toPath, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE)
  }
}

object Lock {

  /** Bumped when the format changes incompatibly; [[parse]] rejects others. */
  val FormatVersion = 3

  /** The major version of the Java version `v`: `21` of `21.0.12`, and `8`
    * of `1.8.0_402`.
    */
  def javaMajor(v: String): String = {
    val parts = v.split("[.+_-]")
    if (parts.length > 1 && parts(0) == "1") parts(1) else parts(0)
  }

  /** The name of the lockfile in the build root. */
  val FileName = "sbt-purifier.lock.json"

  /** The environment variable that names a lockfile kept outside the
    * build, for a build kept external.
    */
  val LockFileVariable = "SBT_PURIFIER_LOCKFILE"

  /** The command that writes the lockfile, which every error names. */
  val LockCommand = "sbt-purifier lock"

  private val VersionLine = """  "version": ([0-9]+),""".r
  private val ContentHashLine = """  "contentHash": "([0-9a-f]{64})",""".r

  private def sha256Hex(s: String): String =
    MessageDigest
      .getInstance("SHA-256")
      .digest(s.getBytes(StandardCharsets.UTF_8))
      .map(b => f"${b & 0xff}%02x")
      .mkString

  /** `text`, a lockfile without its content hash, with the line
    * `  "contentHash": "<hex>",` after its version line. The hash is the
    * SHA-256 of `text`. The line has a fixed place and shape so that Nix
    * can check it with string functions before it parses the JSON (see
    * lib-overlays/default/read-lock.nix).
    */
  def withContentHash(text: String): String = {
    val afterVersion = text.indexOf('\n', text.indexOf('\n') + 1) + 1
    text.substring(0, afterVersion) + "  \"contentHash\": \"" + sha256Hex(text) + "\",\n" + text.substring(afterVersion)
  }

  /** Why `text` is not a lockfile of this format with the content hash
    * it records, or None when it is.
    */
  def contentProblem(text: String): Option[String] =
    text.split("\n", 4) match {
      case Array("{", VersionLine(v), ContentHashLine(h), rest) =>
        if (v.toInt != FormatVersion)
          Some(s"it has lock format version $v, and this plugin reads version $FormatVersion")
        else if (sha256Hex("{\n" + "  \"version\": " + v + ",\n" + rest) != h)
          Some("its content does not match its content hash: it was edited, or it is damaged")
        else None
      case Array("{", VersionLine(v), _*) if v.toInt != FormatVersion =>
        Some(s"it has lock format version $v, and this plugin reads version $FormatVersion")
      case _ => Some("it is not a lockfile that sbt-purifier wrote")
    }

  def parse(text: String): Lock = {
    contentProblem(text).foreach(p => throw new Json.JsonError(s"$p; run `$LockCommand`"))
    val o = Json.parse(text).asObject
    Lock(
      sbtVersion = o("sbtVersion").asString,
      javaVersion = o("javaVersion").asString,
      launcher = o("launcher").asString,
      javaOptions = o("javaOptions").asArray.map(_.asString),
      plugin = o("plugin") match {
        case Json.Null => None
        case j =>
          val p = j.asObject
          Some(LockedPlugin(p("organization").asString, p("name").asString, p("version").asString, p("via").asString))
      },
      projects = o("projects").asArray.map { j =>
        val p = j.asObject
        LockedProject(
          p("id").asString,
          p("organization").asString,
          p("name").asString,
          p("variants").asArray.map(NixArtifacts.parseVariant)
        )
      },
      flakeRepositories = o("flakeRepositories").asArray.map { j =>
        val r = j.asObject
        LockedFlakeRepository(r("name").asString, r("ref").asString, r("locked").asString, r("attribute").asString)
      },
      artifacts = o("artifacts").asArray.map { j =>
        val a = j.asObject
        LockedArtifact(a("path").asString, a("url").asString, a("hash").asString, a("mirrors").asArray.map(_.asString))
      },
      inputsHash = o("inputsHash").asString,
      definitionHash = o("definitionHash").asString,
      buildDirectory = o.get("buildDirectory").map(_.asString),
      implementation = o.get("implementation").map(ImplementationPin.validate)
    )
  }

  def read(file: File): Lock =
    try parse(new String(Files.readAllBytes(file.toPath), StandardCharsets.UTF_8))
    catch {
      case e: Json.JsonError => throw new IllegalStateException(s"cannot read lock file $file: ${e.getMessage}", e)
    }

  /** The lock in `file`, if there is one this plugin can read. A lock of
    * another format counts as none, so the callers relock.
    */
  def readOption(file: File): Option[Lock] =
    if (!file.isFile) None
    else
      try Some(read(file))
      catch { case _: IllegalStateException => None }

  /** Names that take no part in a build: build outputs and editor state.
    * buildSbtProject leaves the same names out of the source it copies.
    */
  def outsideTheBuild(name: String, isDirectory: Boolean): Boolean =
    (isDirectory && Set("target", ".sbt-purifier", ".bsp", ".metals", ".bloop")(name)) ||
      name == ".git" || name == ".idea" || name.startsWith("result")

  /** A digest of the files that define the build's dependencies: every
    * `*.sbt` file at the root, and everything under `project/`,
    * recursively through the meta-builds, except what
    * [[outsideTheBuild]] names. The digest is the SHA-256, in hex, of one
    * line `<path> <SHA-256 of the file, in hex>` for each file, in name
    * order at each level, and then one line `extra <string>` for each of
    * `extra`. With no extra strings, buildSbtProject computes the same
    * digest from its source (lib-overlays/default/definition-hash.nix). The extra
    * strings cover dependency inputs that live outside those files (the
    * injected plugin, declared network URLs, flake references).
    */
  def buildDefinitionHash(root: File, extra: Seq[String]): String = {
    def list(dir: File) = Option(dir.listFiles()).getOrElse(Array.empty[File]).toVector.sortBy(_.getName)
    def files(dir: File, rel: String): Vector[String] =
      list(dir).flatMap { f =>
        val r = rel + "/" + f.getName
        if (outsideTheBuild(f.getName, f.isDirectory)) Vector.empty
        else if (f.isDirectory) files(f, r)
        else Vector(r)
      }
    val sbtFiles =
      list(root).filter(f => f.isFile && f.getName.endsWith(".sbt") && !outsideTheBuild(f.getName, false)).map(_.getName)
    val project = new File(root, "project")
    val all = sbtFiles ++ (if (project.isDirectory) files(project, "project") else Vector.empty)
    def fileHash(rel: String) =
      MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(new File(root, rel).toPath)).map(b => f"${b & 0xff}%02x").mkString
    sha256Hex(all.map(rel => s"$rel ${fileHash(rel)}\n").mkString + extra.map(e => s"extra $e\n").mkString)
  }

  /** The SRI SHA-256 hash of a file, as Nix's `hash` attribute takes it. */
  def sriHash(file: File): String = {
    val md = MessageDigest.getInstance("SHA-256")
    val in = Files.newInputStream(file.toPath)
    try {
      val buf = new Array[Byte](1 << 16)
      var n = in.read(buf)
      while (n >= 0) { md.update(buf, 0, n); n = in.read(buf) }
    } finally in.close()
    "sha256-" + Base64.getEncoder.encodeToString(md.digest())
  }
}
