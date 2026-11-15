// SPDX-License-Identifier: MIT
import java.io.File
import java.nio.charset.StandardCharsets.UTF_8
import java.nio.file.Files
import java.security.MessageDigest

/** Keep the manifest format and inputs identical to pkgs/plugin/nix/source-version.nix. */
object SourceVersion {
  private val parts = Seq("build.sbt", "flake.lock", "project", "src", "lib-overlays", "pkgs", "tools", "sbt-purifier.lock.json")
  private def sha(bytes: Array[Byte]): String =
    MessageDigest.getInstance("SHA-256").digest(bytes).map(b => f"${b & 255}%02x").mkString

  private def paths(root: File, file: File): Seq[String] = {
    require(!Files.isSymbolicLink(file.toPath), "unsupported source symlink: " + file)
    if (file.isDirectory) {
      if (file.getName == "target") Nil
      else Option(file.listFiles()).getOrElse(sys.error("cannot list " + file)).toSeq.flatMap(paths(root, _))
    } else {
      require(file.isFile, "missing or unsupported source version input: " + file)
      Seq(root.toPath.relativize(file.toPath).toString.replace(File.separatorChar, '/'))
    }
  }

  // Nix sorts strings by their UTF-8 bytes, including non-ASCII filenames.
  private def before(a: String, b: String): Boolean = {
    val left = a.getBytes(UTF_8)
    val right = b.getBytes(UTF_8)
    var i = 0
    while (i < math.min(left.length, right.length)) {
      val difference = (left(i) & 255) - (right(i) & 255)
      if (difference != 0) return difference < 0
      i += 1
    }
    left.length < right.length
  }

  def manifest(root: File): String =
    parts.flatMap(part => paths(root, new File(root, part))).sorted(Ordering.fromLessThan(before)).map { path =>
      sha(Files.readAllBytes(new File(root, path).toPath)) + "  " + path + "\n"
    }.mkString

  def version(root: File): String = "0.0.0-dev." + sha(manifest(root).getBytes(UTF_8))
}
