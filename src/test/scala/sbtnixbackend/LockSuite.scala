package sbtnixbackend

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files

class LockSuite extends munit.FunSuite {

  test("implementation pin survives the native lock codec and is covered by its hash") {
    val pin = Json.parse("""{"version":1,"tool":"sbt-purifier","source":{"type":"github","owner":"nix-purifiers","repo":"sbt-purifier","rev":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","narHash":"sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}""")
    val pinned = lock.copy(implementation = Some(pin))
    assertEquals(Lock.parse(pinned.render).implementation, Some(pin))
    assert(Lock.contentProblem(pinned.render.replace("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")).nonEmpty)
  }

  private val lock = Lock(
    sbtVersion = "1.11.7",
    javaVersion = "21.0.12",
    launcher = "https/repo1.maven.org/maven2/org/scala-sbt/sbt-launch/1.11.7/sbt-launch-1.11.7.jar",
    javaOptions = Vector("-Xmx2g"),
    plugin = Some(LockedPlugin("io.github.clhodapp", "sbt-nix-backend", "0.1.0", LockedPlugin.Environment)),
    projects = Vector(
      LockedProject("b", "o", "b_3"),
      LockedProject(
        "a",
        "o",
        "a_3",
        Vector(
          NixArtifacts.Variant(
            "scala3",
            "3.3.6",
            Vector(
              sbt.librarymanagement.Artifact("a", "jar", "jar"),
              sbt.librarymanagement.Artifact("a", "src", "jar", "sources")
            )
          )
        )
      )
    ),
    flakeRepositories = Vector(LockedFlakeRepository("r", "github:x/y#z", "github:x/y/abc?narHash=h", "z")),
    artifacts = Vector(
      LockedArtifact("https/h/z.jar", "https://h/z.jar", "sha256-z"),
      LockedArtifact("https/h/a.jar", "https://h/a.jar", "sha256-a")
    ),
    inputsHash = "sha256-i",
    definitionHash = "sha256-d"
  )

  private def tempDir(): File = Files.createTempDirectory("lock").toFile

  private def write(f: File, s: String): Unit = {
    f.getParentFile.mkdirs()
    Files.write(f.toPath, s.getBytes(StandardCharsets.UTF_8))
  }

  test("a lock survives writing and reading, with its lists sorted") {
    val f = new File(tempDir(), "lock.json")
    lock.write(f)
    val read = Lock.read(f)
    assertEquals(read.projects.map(_.id), Vector("a", "b"))
    assertEquals(read.artifacts.map(_.path), Vector("https/h/a.jar", "https/h/z.jar"))
    assertEquals(read.copy(projects = lock.projects, artifacts = lock.artifacts), lock)
  }

  test("a legacy build preserves its directory-derived project identity") {
    val legacy = lock.copy(sbtVersion = "0.13.18", buildDirectory = Some("old-project"))
    assertEquals(Lock.parse(legacy.render).buildDirectory, Some("old-project"))
    assert(!lock.toJson.asObject.fields.exists(_._1 == "buildDirectory"))
  }

  test("header checksums and pre-seeded downloads lock the same published sidecars") {
    val cache = tempDir()
    val rel = "https/repo1.maven.org/maven2/org/example/a/1/a-1.jar"
    write(new File(cache, rel), "artifact")
    write(new File(cache, rel.replace("a-1.jar", ".a-1.jar__sha1")), "header hash")
    write(new File(cache, rel + ".pom"), "pre-seeded artifact without headers")
    val mirror = "https://mirror.example/maven2/"
    var downloads = Vector.empty[String]
    def fetch(url: String, file: File): Unit = {
      downloads :+= url
      write(file, "published checksum\n")
    }
    LockGeneration.materializeCentralChecksums(cache, Some(mirror), fetch)
    assertEquals(downloads, Vector(mirror + "org/example/a/1/a-1.jar.sha1", mirror + "org/example/a/1/a-1.jar.pom.sha1"))
    assertEquals(new String(Files.readAllBytes(new File(cache, rel + ".sha1").toPath), StandardCharsets.UTF_8),
      "published checksum\n")
    val files = CachePaths.downloadedFiles(cache)
    downloads = Vector.empty
    LockGeneration.materializeCentralChecksums(cache, Some(mirror), fetch)
    assertEquals(downloads, Vector.empty)
    assertEquals(CachePaths.downloadedFiles(cache), files)
  }

  test("writing is deterministic") {
    val d = tempDir()
    lock.write(new File(d, "1.json"))
    lock.copy(artifacts = lock.artifacts.reverse, projects = lock.projects.reverse).write(new File(d, "2.json"))
    assertEquals(
      Files.readAllBytes(new File(d, "1.json").toPath).toSeq,
      Files.readAllBytes(new File(d, "2.json").toPath).toSeq
    )
  }

  test("another format version is refused, and readOption treats it as no lock") {
    val f = new File(tempDir(), "lock.json")
    write(f, lock.toJson.render.replace("\"version\": " + Lock.FormatVersion, "\"version\": 99"))
    intercept[IllegalStateException](Lock.read(f))
    assertEquals(Lock.readOption(f), None)
    assertEquals(Lock.readOption(new File(tempDir(), "absent.json")), None)
  }

  private def readError(text: String): String = {
    val f = new File(tempDir(), "lock.json")
    write(f, text)
    intercept[IllegalStateException](Lock.read(f)).getMessage
  }

  test("the content hash line follows the version line, and an edit fails naming the command") {
    val text = lock.render
    assert(text.startsWith("{\n  \"version\": " + Lock.FormatVersion + ",\n  \"contentHash\": \""), text.take(80))
    val message = readError(text.replace("sha256-a", "sha256-b"))
    assert(message.contains("does not match its content hash"), message)
    assert(message.contains("run `sbt-purifier lock`"), message)
  }

  test("a malformed lockfile fails naming the command") {
    for (text <- Seq("", "not json", lock.render.dropRight(10), lock.render.replace("\"contentHash\"", "\"hash\""))) {
      val message = readError(text)
      assert(message.contains("run `sbt-purifier lock`"), message)
    }
  }

  test("a lockfile of an earlier format fails naming the command") {
    val message = readError("{\n  \"version\": 2,\n  \"sbtVersion\": \"1.11.7\"\n}\n")
    assert(message.contains("lock format version 2"), message)
    assert(message.contains("run `sbt-purifier lock`"), message)
  }

  test("the definition hash is the hash of one line per file") {
    val root = tempDir()
    write(new File(root, "build.sbt"), "a")
    write(new File(root, "project/build.properties"), "b")
    def hex(s: String) =
      java.security.MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8)).map(b => f"${b & 0xff}%02x").mkString
    assertEquals(Lock.buildDefinitionHash(root, Nil), hex(s"build.sbt ${hex("a")}\nproject/build.properties ${hex("b")}\n"))
  }

  test("the definition hash covers *.sbt and project/, and ignores target/ and sources") {
    val root = tempDir()
    write(new File(root, "build.sbt"), "a")
    write(new File(root, "project/plugins.sbt"), "b")
    write(new File(root, "project/Build.scala"), "c")
    write(new File(root, "src/main/scala/A.scala"), "d")
    val h0 = Lock.buildDefinitionHash(root, Nil)
    write(new File(root, "project/target/x"), "noise")
    write(new File(root, "project/project/target/y"), "noise")
    write(new File(root, "src/main/scala/A.scala"), "changed")
    assertEquals(Lock.buildDefinitionHash(root, Nil), h0)
    write(new File(root, "project/plugins.sbt"), "b2")
    val h1 = Lock.buildDefinitionHash(root, Nil)
    assertNotEquals(h1, h0)
    write(new File(root, "other.sbt"), "e")
    assertNotEquals(Lock.buildDefinitionHash(root, Nil), h1)
  }

  test("extra inputs change the hash, and moving bytes between files does too") {
    val root = tempDir()
    write(new File(root, "a.sbt"), "xy")
    write(new File(root, "b.sbt"), "z")
    val h = Lock.buildDefinitionHash(root, Nil)
    assertNotEquals(Lock.buildDefinitionHash(root, Seq("url=x")), h)
    write(new File(root, "a.sbt"), "x")
    write(new File(root, "b.sbt"), "yz")
    assertNotEquals(Lock.buildDefinitionHash(root, Nil), h)
  }

  test("sriHash is the SRI form of SHA-256") {
    val f = new File(tempDir(), "f")
    write(f, "abc")
    assertEquals(Lock.sriHash(f), "sha256-ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=")
  }
}
