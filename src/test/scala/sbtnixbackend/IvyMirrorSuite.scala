package sbtnixbackend

import sbt.librarymanagement.{MavenRepository, Patterns, Resolver, URLRepository}

class IvyMirrorSuite extends munit.FunSuite {

  private val base = "file:/cache/"

  test("a Maven root moves onto its coursier path under the mirror") {
    assertEquals(
      IvyMirror.rewrite(MavenRepository("central", "https://repo1.maven.org/maven2/"), base),
      MavenRepository("central", "file:/cache/https/repo1.maven.org/maven2/").withAllowInsecureProtocol(true): Resolver
    )
  }

  test("rewriting an inherited launcher mirror is idempotent") {
    val proxy = "http://127.0.0.1:1234"
    val original = MavenRepository("central", "https://repo.maven.apache.org/maven2/")
    val rewritten = IvyMirror.rewrite(original, proxy)
    assertEquals(IvyMirror.rewrite(rewritten, proxy), rewritten)
    assertEquals(IvyMirror.unrewrite(rewritten, proxy), original: Resolver)
  }

  test("a port is escaped the way coursier stores it") {
    assertEquals(
      IvyMirror.rewritePrefix("http://localhost:8080/repo/", "http://127.0.0.1:1"),
      Some("http://127.0.0.1:1/http/localhost%3A8080/repo/")
    )
  }

  test("an Ivy pattern keeps its tokens after the rewritten prefix") {
    val pattern = "https://repo.scala-sbt.org/scalasbt/sbt-plugin-releases/[organisation]/[module](/scala_[scalaVersion])/[revision]/ivys/ivy.xml"
    val rewritten = IvyMirror.rewrite(URLRepository("sbt-plugins", Patterns(Vector(pattern), Vector(pattern), false, false, false), false), base)
    assertEquals(
      rewritten.asInstanceOf[URLRepository].patterns.ivyPatterns,
      Vector("file:/cache/https/repo.scala-sbt.org/scalasbt/sbt-plugin-releases/[organisation]/[module](/scala_[scalaVersion])/[revision]/ivys/ivy.xml")
    )
  }

  test("file mirrors read literal port percent and space cache names") {
    val directory = java.nio.file.Files.createTempDirectory("ivy mirror ").toFile
    try {
      val mirror = directory.toURI.toString
      val urls = Seq("http://localhost:8080/repo/a.pom", "https://example.org/a%20b/c%25.pom", "https://example.org/a b/c.pom")
      urls.foreach { original =>
        val cached = new java.io.File(directory, CachePaths.relativePath(original))
        cached.getParentFile.mkdirs()
        java.nio.file.Files.write(cached.toPath, original.getBytes("UTF-8"))
        val rewritten = IvyMirror.rewritePrefix(original, mirror).get
        val input = new java.net.URL(rewritten).openStream()
        val actual = try scala.io.Source.fromInputStream(input, "UTF-8").mkString finally input.close()
        assertEquals(actual, original)
        assertEquals(IvyMirror.unrewritePrefix(rewritten, mirror), original)
      }
    } finally LockGeneration.deleteRecursively(directory)
  }

  test("the local repository stays in the user's Ivy home") {
    val local = Resolver.defaultLocal
    val pinned = IvyMirror.pinIvyHome(local, new java.io.File("/home/user/.ivy2")).asInstanceOf[sbt.librarymanagement.FileRepository]
    assert(pinned.patterns.ivyPatterns.forall(_.startsWith("/home/user/.ivy2/local/")), pinned.patterns.ivyPatterns.toString)
  }

  test("each set of local repositories has an Ivy home of its own") {
    val setup = IvyMirror.Setup(None, new java.io.File("/homes"), "lock")
    def home(dir: String) = IvyMirror.home(setup, Seq(MavenRepository("plugin", s"file:$dir/")))
    assertNotEquals(home("/nix/store/a-plugin"), home("/nix/store/b-plugin"))
    assertEquals(home("/nix/store/a-plugin"), home("/nix/store/a-plugin"))
    assertNotEquals(home("/nix/store/a-plugin"), IvyMirror.home(setup.copy(key = "other lock"), Seq(MavenRepository("plugin", "file:/nix/store/a-plugin/"))))
  }

  test("a local repository is read where it is") {
    val local = MavenRepository("local", "file:/home/user/.m2/repository/")
    assertEquals(IvyMirror.rewrite(local, base), local: Resolver)
  }
}
