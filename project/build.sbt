// SPDX-License-Identifier: MIT
//
// The meta-build compiles the plugin from this repository's own sources,
// so the plugin's build runs with the plugin: `nixLock` locks it and the
// flake builds it in the sandbox without a published copy. The resource
// generator is the one in NixLibraryResources.scala, which the meta-build
// compiles and so cannot use itself.
Compile / unmanagedSourceDirectories ++= Seq("scala", "scala-2.12", "scala-modern", "scala-ivy").map(baseDirectory.value.getParentFile / "src" / "main" / _)
Compile / resourceGenerators += Def.task {
  val out = (Compile / resourceManaged).value / "sbtnixbackend" / "nix"
  val root = baseDirectory.value.getParentFile
  IO.readLines(root / "pkgs" / "plugin" / "resources.tsv").map { line =>
    val columns = line.split("\t", 2)
    val target = out / columns(0)
    IO.copyFile(root / columns(1), target)
    target
  }
}.taskValue
