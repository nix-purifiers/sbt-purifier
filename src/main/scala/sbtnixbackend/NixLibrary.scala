package sbtnixbackend

import java.io.File
import java.nio.file.Files

/** The sbt-nix-backend Nix library (nix/lib in its repository), which the
  * plugin carries in its jar: the same files whose builder the flake overlay
  * adds to a package set as `buildSbtProject`, from the same revision.
  * Interactive mode evaluates realize.nix from it, and nixPackagedArtifacts
  * builds package.nix.
  */
object NixLibrary {

  val files: Vector[String] =
    Vector(
      "build-sbt-project.nix",
      "definition-hash.nix",
      "dependencies.nix",
      "fetch.nix",
      // The flake.lock of the repository, whose purifier node
      // purifier-source.nix reads.
      "flake.lock",
      "legacy-boot-repositories.nix",
      "legacy-global-settings.sbt",
      "library.nix",
      "lock-version.nix",
      "package.nix",
      "plugin.nix",
      "project-repository.nix",
      "purifier-source.nix",
      "read-lock.nix",
      "realize.nix",
      "source-readable.nix",
      "source-version.nix"
    )

  private[sbtnixbackend] def resource(name: String): Array[Byte] = {
    val in = getClass.getResourceAsStream("/sbtnixbackend/nix/" + name)
    if (in == null) throw new IllegalStateException(s"the plugin jar has no Nix file $name")
    try {
      val out = new java.io.ByteArrayOutputStream
      val buf = new Array[Byte](8192)
      var n = in.read(buf)
      while (n >= 0) { out.write(buf, 0, n); n = in.read(buf) }
      out.toByteArray
    } finally in.close()
  }

  /** Writes the files to `dir`, leaving unchanged ones alone, and returns
    * `dir`.
    */
  def write(dir: File): File = {
    dir.mkdirs()
    files.foreach { n =>
      val f = new File(dir, n)
      val content = resource(n)
      if (!(f.isFile && java.util.Arrays.equals(Files.readAllBytes(f.toPath), content)))
        Files.write(f.toPath, content)
    }
    dir
  }
}
