package sbtnixbackend

import java.io.IOException
import java.net.InetSocketAddress
import java.nio.file.Files
import javax.net.ssl.SSLException

import com.sun.net.httpserver.{HttpExchange, HttpHandler, HttpServer}
import lmcoursier.internal.shaded.coursier.Artifacts
import lmcoursier.internal.shaded.coursier.cache.{ArtifactError, CachePolicy, FileCache}
import lmcoursier.internal.shaded.coursier.error.FetchError
import lmcoursier.internal.shaded.coursier.util.{Artifact, Task}
import sbt.Incomplete

class ClassifierDownloadsSuite extends munit.FunSuite {
  private val url = "https://example.invalid/dependency-sources.jar"
  private def message(code: Int, at: String = url): String =
    s"Server returned HTTP response code: $code for URL: $at"
  private def download(cause: Throwable, reason: String = "native download failed"): ArtifactError.DownloadError =
    new ArtifactError.DownloadError(reason, Some(cause))
  private def failure(errors: (Artifact, ArtifactError)*): Incomplete =
    Incomplete(None, directCause = Some(new FetchError.DownloadingArtifacts(errors)))
  private def transient(code: Int = 503): Incomplete = failure(Artifact(url) -> download(new IOException(message(code))))

  private class HttpFixture(response: (String, Int) => (Int, Array[Byte], Long)) {
    val root = Files.createTempDirectory("classifier-downloads")
    private val requests = scala.collection.mutable.Map.empty[String, Int]
    private val server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0)
    server.createContext("/", new HttpHandler {
      def handle(exchange: HttpExchange): Unit = {
        val path = exchange.getRequestURI.getPath
        val count = requests.synchronized {
          val next = requests.getOrElse(path, 0) + 1
          requests.update(path, next)
          next
        }
        try {
          val (code, body, length) = response(path, count)
          exchange.sendResponseHeaders(code, length)
          exchange.getResponseBody.write(body)
        } finally exchange.close()
      }
    })
    server.start()
    // The package's own build runs Coursier offline. This fixture alone fetches
    // from its owned loopback server; the product cache policy stays unchanged.
    val cache = FileCache[Task]().withLocation(root.toFile).withCachePolicies(Seq(CachePolicy.FetchMissing))
    def count(path: String): Int = requests.synchronized { requests.getOrElse(path, 0) }
    private def url(path: String): String = s"http://127.0.0.1:${server.getAddress.getPort}$path"
    private def fetchArtifacts(artifacts: Seq[Artifact], selectedCache: FileCache[Task]): Unit = {
      Artifacts[Task](selectedCache).addExtraArtifacts(_ => artifacts).eitherResult() match {
        case Left(error) => throw Incomplete(None, directCause = Some(error))
        case Right(_) => ()
      }
    }
    def fetch(paths: String*): Unit = fetchArtifacts(paths.map(path => Artifact(url(path))), cache)
    def fetchChecked(path: String): Unit = fetchArtifacts(
      Seq(Artifact(url(path)).withChecksumUrls(Map("SHA-1" -> url(path + ".sha1")))),
      cache.withChecksums(Seq(Some("SHA-1")))
    )
    def close(): Unit = {
      server.stop(0)
      LockGeneration.deleteRecursively(root.toFile)
    }
  }
  private val body = "owned artifact bytes".getBytes("UTF-8")
  private def reply(code: Int): (Int, Array[Byte], Long) = (code, body, body.length.toLong)
  private def withFixture(response: (String, Int) => (Int, Array[Byte], Long))(test: HttpFixture => Unit): Unit = {
    val fixture = new HttpFixture(response)
    try test(fixture) finally fixture.close()
  }

  test("native classifier HTTP 503 then 200 retries within one cache and retains successful bytes") {
    withFixture((path, count) => reply(if (path == "/artifact.jar" && count == 1) 503 else 200)) { fixture =>
      var operations = 0
      val waits = scala.collection.mutable.ArrayBuffer.empty[Long]
      val retries = scala.collection.mutable.ArrayBuffer.empty[Int]
      ClassifierDownloads.run(
        () => { operations += 1; fixture.fetch("/artifact.jar") },
        (next, _) => retries += next,
        milliseconds => waits += milliseconds
      )
      assertEquals(operations, 2)
      assertEquals(fixture.count("/artifact.jar"), 2)
      assertEquals(retries.toList, List(2))
      assertEquals(waits.toList, List(1000L))
      fixture.fetch("/artifact.jar")
      assertEquals(fixture.count("/artifact.jar"), 2, "the successful native cache file is reused")
      val files = Files.walk(fixture.root)
      try {
        val artifact = files.toArray.map(_.asInstanceOf[java.nio.file.Path]).find(_.getFileName.toString == "artifact.jar").get
        assertEquals(Files.readAllBytes(artifact).toSeq, body.toSeq)
      } finally files.close()
    }
  }

  test("persistent native HTTP 503 stops after three attempts and exact bounded delays") {
    withFixture((_, _) => reply(503)) { fixture =>
      var operations = 0
      val waits = scala.collection.mutable.ArrayBuffer.empty[Long]
      val failures = scala.collection.mutable.ArrayBuffer.empty[Incomplete]
      val last = intercept[Incomplete] {
        ClassifierDownloads.run(() => { operations += 1; fixture.fetch("/artifact.jar") },
          (_, failure) => failures += failure, milliseconds => waits += milliseconds)
      }
      assertEquals(operations, 3)
      assertEquals(fixture.count("/artifact.jar"), 3)
      assertEquals(waits.toList, List(1000L, 2000L))
      assertEquals(failures.size, 2)
      assert(ClassifierDownloads.retryable(last))
      assert(!failures.exists(_ eq last), "exhaustion propagates the actual final failure")
    }
  }

  test("a mixed native 503 and unauthorized artifact failure does not retry either download") {
    withFixture((path, _) => reply(if (path == "/transient.jar") 503 else 401)) { fixture =>
      var retries = 0
      val failed = intercept[Incomplete] {
        ClassifierDownloads.run(() => fixture.fetch("/transient.jar", "/unauthorized.jar"),
          (_, _) => retries += 1, _ => fail("mixed failure must not wait"))
      }
      assert(!ClassifierDownloads.retryable(failed))
      assertEquals(retries, 0)
      assertEquals(fixture.count("/transient.jar"), 1)
      assertEquals(fixture.count("/unauthorized.jar"), 1)
    }
  }

  for (status <- List(401, 403, 404, 410)) test(s"native HTTP $status remains a hard classifier failure") {
    withFixture((_, _) => reply(status)) { fixture =>
      val failed = intercept[Incomplete] {
        ClassifierDownloads.run(() => fixture.fetch("/artifact.jar"), (_, _) => fail("must not retry"), _ => fail("must not wait"))
      }
      assert(!ClassifierDownloads.retryable(failed))
      assertEquals(fixture.count("/artifact.jar"), 1)
    }
  }

  test("TLS, checksum, truncation, wrong exception class and spoofed reason fail closed") {
    val errors: List[ArtifactError] = List(
      download(new SSLException(message(503))),
      new ArtifactError.WrongChecksum("SHA-1", "actual", "expected", "artifact", "artifact.sha1"),
      new ArtifactError.WrongLength(3L, 50L, "artifact"),
      download(new IOException(message(503)) {}),
      download(new IOException("unrelated failure"), message(503)),
      download(new IOException(message(503, "https://elsewhere.invalid/redirect.jar"))),
      download(new IOException(message(503) + " extra text"))
    )
    errors.foreach(error => assert(!ClassifierDownloads.retryable(failure(Artifact(url) -> error))))
    val wrongNativeType = Incomplete(None, directCause = Some(new IOException(message(503))))
    assert(!ClassifierDownloads.retryable(wrongNativeType))
  }

  test("a native checksum mismatch never retries the classifier task") {
    withFixture((path, _) => {
      val bytes = if (path.endsWith(".sha1")) ("0" * 40).getBytes("UTF-8") else body
      (200, bytes, bytes.length.toLong)
    }) { fixture =>
      var calls = 0
      val failed = intercept[Incomplete] {
        ClassifierDownloads.run(() => { calls += 1; fixture.fetchChecked("/artifact.jar") },
          (_, _) => fail("checksum failure must not retry"), _ => fail("must not wait"))
      }
      assert(!ClassifierDownloads.retryable(failed))
      assertEquals(calls, 1, "native checksum retries do not cause a classifier task retry")
      assert(fixture.count("/artifact.jar.sha1") >= 1)
    }
  }

  test("a truncated native artifact with its published checksum never retries the classifier task") {
    val checksum = java.security.MessageDigest.getInstance("SHA-1").digest(body).map(byte => f"${byte & 0xff}%02x").mkString.getBytes("UTF-8")
    withFixture((path, _) =>
      if (path.endsWith(".sha1")) (200, checksum, checksum.length.toLong)
      else (200, body.take(3), body.length.toLong)
    ) { fixture =>
      var calls = 0
      val failed = intercept[Incomplete] {
        ClassifierDownloads.run(() => { calls += 1; fixture.fetchChecked("/artifact.jar") },
          (_, _) => fail("truncation must not retry"), _ => fail("must not wait"))
      }
      assert(!ClassifierDownloads.retryable(failed))
      assertEquals(calls, 1)
      assert(fixture.count("/artifact.jar") >= 1)
    }
  }

  test("every Incomplete branch and suppressed exception must pass the native guard") {
    val eligible = transient()
    assert(ClassifierDownloads.retryable(Incomplete(None, causes = Seq(eligible))))
    assert(!ClassifierDownloads.retryable(Incomplete(None, causes = Seq(eligible, Incomplete(None)))))
    assert(!ClassifierDownloads.retryable(Incomplete(None, causes = Seq(eligible), directCause = Some(new IllegalStateException("other task")))))
    val suppressed = transient()
    suppressed.addSuppressed(new IllegalStateException("other failure"))
    assert(!ClassifierDownloads.retryable(suppressed))
    val io = new IOException(message(503))
    io.addSuppressed(new IOException("other transport error"))
    assert(!ClassifierDownloads.retryable(failure(Artifact(url) -> download(io))))
    val mutableErrors = scala.collection.mutable.ArrayBuffer[(Artifact, ArtifactError)](
      Artifact(url) -> download(new IOException("unrecognized original cause")))
    val changed = new FetchError.DownloadingArtifacts(mutableErrors)
    mutableErrors.update(0, Artifact(url) -> download(new IOException(message(503))))
    assert(!ClassifierDownloads.retryable(Incomplete(None, directCause = Some(changed))),
      "the native exception cause must still be the enumerated first artifact failure")
    val cyclic = new IOException(message(503))
    val next = new IOException(message(503), cyclic)
    cyclic.initCause(next)
    assert(!ClassifierDownloads.retryable(failure(Artifact(url) -> download(cyclic))))
  }

  test("interrupted backoff restores interruption and cannot start another task") {
    var calls = 0
    var interrupted = false
    try {
      try {
        ClassifierDownloads.run(() => { calls += 1; throw transient() }, (_, _) => (),
          _ => throw new InterruptedException("owned interrupt"))
      } catch { case _: InterruptedException => interrupted = true }
      assert(interrupted)
      assert(Thread.currentThread.isInterrupted)
      assertEquals(calls, 1)
    } finally Thread.interrupted()
  }

  test("fatal and unrelated task failures propagate without retry") {
    val fatal = new LinkageError("owned fatal failure")
    var captured: LinkageError = null
    try {
      ClassifierDownloads.run(() => throw fatal, (_, _) => fail("must not retry"), _ => fail("must not wait"))
    } catch { case error: LinkageError => captured = error }
    assert(captured eq fatal)
    val failed = Incomplete(None, directCause = Some(new IllegalStateException("project task failed")))
    val propagated = intercept[Incomplete] {
      ClassifierDownloads.run(() => throw failed, (_, _) => fail("must not retry"), _ => fail("must not wait"))
    }
    assert(propagated eq failed)
  }
}
