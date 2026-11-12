// SPDX-License-Identifier: MIT
import sbt._
import sbt.Keys._

/** Copies the source-owned runtime tree exactly, using the same manifest as the meta-build. */
object NixLibraryResources {
  def settings: Seq[Setting[_]] = Seq(
    Compile / resourceGenerators += Def.task {
      val out = (Compile / resourceManaged).value / "sbtnixbackend" / "nix"
      val root = (ThisBuild / baseDirectory).value
      // The flake.lock goes beside the library, for purifier-source.nix.
      IO.readLines(root / "pkgs" / "plugin" / "resources.tsv").map { line =>
        val columns = line.split("\t", 2)
        val target = out / columns(0)
        IO.copyFile(root / columns(1), target)
        target
      }
    }.taskValue
  )
}
