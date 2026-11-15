package sbtnixbackend

import java.io.File
import java.nio.file.Files

/** The sbt-nix-backend resource tree, assembled from its source owners, which the
  * plugin carries in its jar: the same files whose builder the flake overlay
  * adds to a package set as `buildSbtProject`, from the same revision.
  * Interactive mode evaluates realize.nix from it, and nixPackagedArtifacts
  * builds package.nix.
  */
object NixLibrary {

  lazy val sources: Vector[(String, String)] = {
    val text = new String(resource("resources.tsv"), java.nio.charset.StandardCharsets.UTF_8)
    text.split("\n").iterator.filter(_.nonEmpty).map { line =>
      val columns = line.split("\t", 2)
      (columns(0), columns(1))
    }.toVector
  }

  lazy val files: Vector[String] = sources.map(_._1)

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
      f.getParentFile.mkdirs()
      val content = resource(n)
      if (!(f.isFile && java.util.Arrays.equals(Files.readAllBytes(f.toPath), content)))
        Files.write(f.toPath, content)
    }
    dir
  }
}
