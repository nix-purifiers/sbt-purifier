package sbtnixbackend

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, LinkOption, Path}

/** The plugin's uses of the Nix command line.
  *
  * Every call passes `--extra-experimental-features 'nix-command flakes'`,
  * so the plugin works whether or not the user's nix.conf enables them.
  */
final class Nix(log: String => Unit) {

  private val features = Seq("--extra-experimental-features", "nix-command flakes")

  private def nix(args: String*): String =
    Proc.output(Seq("nix") ++ features ++ args, onStderr = log)

  /** The system Nix builds for on this machine (`x86_64-linux`). */
  lazy val currentSystem: String =
    nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem").trim

  /** The locked form of a flake reference: a URL with the revision and the
    * `narHash` pinned, which builtins.getFlake accepts in pure evaluation.
    */
  def lockFlakeRef(ref: String): String = {
    val meta = Json.parse(nix("flake", "metadata", "--json", "--no-write-lock-file", ref)).asObject
    val locked = meta.get("locked").map(_.asObject)
    val url = meta.get("url").map(_.asString).getOrElse(
      throw new IllegalStateException(s"nix flake metadata printed no url for $ref")
    )
    val narHash = locked.flatMap(_.get("narHash")).map(_.asString)
    narHash match {
      case Some(h) if !url.contains("narHash=") =>
        url + (if (url.contains("?")) "&" else "?") + "narHash=" + h
      case _ => url
    }
  }

  /** Builds `installable`, a locked flake reference with an attribute, and
    * returns its output path, rooted at `outLink`. A flake without a lock
    * file of its own takes the pins of the flakes it reaches, and Nix
    * cannot write one into a remote flake.
    */
  def build(installable: String, outLink: File): File = {
    Option(outLink.getParentFile).foreach(_.mkdirs())
    val out = nix("build", "--no-write-lock-file", "--print-out-paths", "--out-link", outLink.getAbsolutePath, installable).trim
    new File(out.split("\\r?\\n").toSeq.head)
  }

  /** Realize a typed JVM projection and its separate metadata output. */
  def projectRepository(locked: String, attribute: String, library: File, outLink: File): Nix.RepositoryView = {
    Option(outLink.getParentFile).foreach(_.mkdirs())
    val arguments = Seq(
      "--file",
      new File(library, "pkgs/build-sbt-project/project-repository.nix").getAbsolutePath,
      "--argstr",
      "locked",
      locked,
      "--argstr",
      "attribute",
      attribute,
      "--argstr",
      "system",
      currentSystem
    )
    val info = Json.parse(nix((Seq("eval", "--json") ++ arguments ++ Seq("info")): _*)).asObject
    nix(
      (Seq("build", "--no-write-lock-file", "--out-link", outLink.getAbsolutePath) ++
        arguments ++ Seq("build")): _*
    )
    Nix.RepositoryView(new File(info("path").asString), info.get("catalog").map(value => new File(value.asString)))
  }

  /** Builds attribute `attribute` of the Nix file `file`, called with the
    * string arguments `args`, in an impure evaluation (the file may read
    * the build root by its absolute path), and returns its output path,
    * rooted at `outLink`. Build logs go to the logger.
    */
  def buildFile(file: File, attribute: String, args: Seq[(String, String)], outLink: File): File = {
    Option(outLink.getParentFile).foreach(_.mkdirs())
    val argv = args.flatMap { case (k, v) => Seq("--argstr", k, v) }
    val out = nix(
      Seq("build", "--impure", "--print-build-logs", "--print-out-paths", "--out-link", outLink.getAbsolutePath, "--file", file.getAbsolutePath) ++
        argv ++ Seq(attribute): _*
    ).trim
    new File(out.split("\\r?\\n").toSeq.last)
  }

  /** Downloads every artifact of `lockFile` into the store and returns the
    * map from cache-relative path to store path. One GC root at `gcRoot`
    * keeps the downloads alive.
    *
    * `libDir` holds the plugin's Nix library (realize.nix and what it
    * imports).
    */
  def realizeLock(lockFile: File, libDir: File, gcRoot: File): Map[String, String] = {
    val expr = new File(libDir, "realize.nix").getAbsolutePath
    val args = Seq("--arg", "lockFile", lockFile.getAbsolutePath, "--arg", "libDir", libDir.getAbsolutePath)
    // nix-build of an attribute set builds every derivation in it, in
    // parallel, without printing a path per artifact to a terminal.
    Proc.output(Seq("nix-build", "--no-out-link", "--quiet", expr, "-A", "fetches") ++ args, onStderr = log)
    val manifestJson = Proc.output(
      Seq("nix-instantiate", "--eval", "--strict", "--json", "--read-write-mode", expr, "-A", "manifest") ++ args,
      onStderr = log
    )
    val manifest = Json.parse(manifestJson).asString
    Option(gcRoot.getParentFile).foreach(_.mkdirs())
    Proc.output(Seq("nix-store", "--add-root", gcRoot.getAbsolutePath, "--realise", manifest), onStderr = log)
    Json.parse(new String(Files.readAllBytes(new File(manifest).toPath), StandardCharsets.UTF_8))
      .asObject
      .fields
      .map { case (k, v) => k -> v.asString }
      .toMap
  }
}

object Nix {

  final case class RepositoryView(path: File, catalog: Option[File])

  /** Makes `dir` a coursier cache whose files are symbolic links into the
    * store, as `manifest` (cache-relative path to store path) lists them.
    * The directories are real and writable: coursier writes lock and marker
    * files next to what it reads.
    *
    * Entries already linked to the right target are kept; any other file
    * under `dir` that coursier did not write is removed, so the directory
    * holds exactly the lock's artifacts.
    */
  def linkCache(dir: File, manifest: Map[String, String]): Unit = {
    dir.mkdirs()
    val base = dir.toPath
    manifest.foreach { case (rel, target) =>
      val link = base.resolve(rel)
      Files.createDirectories(link.getParent)
      val targetPath = new File(target).toPath
      val current =
        if (Files.isSymbolicLink(link)) Some(Files.readSymbolicLink(link))
        else if (Files.exists(link, LinkOption.NOFOLLOW_LINKS)) { Files.delete(link); None }
        else None
      if (!current.exists(_ == targetPath)) {
        if (current.isDefined) Files.delete(link)
        Files.createSymbolicLink(link, targetPath)
      }
    }
    val wanted = manifest.keySet
    def prune(d: Path): Unit = {
      val children = Option(d.toFile.listFiles()).getOrElse(Array.empty[File])
      children.foreach { f =>
        val p = f.toPath
        if (Files.isDirectory(p, LinkOption.NOFOLLOW_LINKS)) prune(p)
        else {
          val rel = base.relativize(p).toString.replace(File.separatorChar, '/')
          if (!CachePaths.isBookkeeping(f.getName) && !wanted.contains(rel)) Files.delete(p)
        }
      }
    }
    prune(base)
  }
}
