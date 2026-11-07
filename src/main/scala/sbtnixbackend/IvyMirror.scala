package sbtnixbackend

import java.io.File
import java.net.{HttpURLConnection, InetSocketAddress, URI}
import java.nio.file.{Files, StandardCopyOption}
import java.util.concurrent.Executors

import com.sun.net.httpserver.{HttpExchange, HttpHandler, HttpServer}
import sbt.librarymanagement.{FileRepository, MavenRepository, Patterns, Resolver, URLRepository}

/** Dependency resolution through Apache Ivy (`useCoursier := false`, sbt 1
  * only) on the locked downloads.
  *
  * The coursier cache of a lock mirrors the URLs it holds: the file of
  * `https://host/path` is at `https/host/path`. A build that resolves with
  * Ivy has every remote resolver rewritten onto a mirror of that layout:
  *
  *  - in a Nix build and interactively, the cache itself, as a `file:` URL,
  *    so Ivy reads exactly the locked files;
  *  - in the lock run, a [[IvyMirror.Server]] in front of the network, which
  *    fetches each file Ivy asks for into the fork's cache at its coursier
  *    path, so the lock collects it with the rest.
  */
object IvyMirror {

  /** The environment variable that names the mirror in the lock run. */
  val Variable = "SBT_NIX_IVY_MIRROR"

  /** How a project that resolves with Ivy reads: through the mirror at
    * `base` when there is one (in a Nix build, the lock run, and
    * interactively with a usable lock), and always with an Ivy home of its
    * own under `homes`.
    *
    * Ivy keeps every module it resolves in the cache of its home, and reads
    * a module from a local repository where it first found it, by version.
    * A rebuilt plugin repository or flake repository is a new Nix store
    * path that holds new files under the same version, and a new lock may
    * hold other files under the same URLs. The home is therefore named by a
    * digest of `key` (the lockfile) and of every local repository the
    * project reads, so each of those has a cache of its own.
    */
  final case class Setup(base: Option[String], homes: File, key: String)

  /** The Ivy home of a project with `setup` that reads `resolvers`. */
  def home(setup: Setup, resolvers: Seq[Resolver]): File = {
    val local = resolvers.flatMap {
      case m: MavenRepository if m.root.startsWith("file:") => Seq(m.root)
      case f: FileRepository                                => f.patterns.ivyPatterns ++ f.patterns.artifactPatterns
      case _                                                => Nil
    }
    val digest = java.security.MessageDigest.getInstance("SHA-256")
    (setup.key +: local.distinct.sorted).foreach(s => digest.update((s + "\n").getBytes("UTF-8")))
    new File(setup.homes, digest.digest().take(10).map(b => f"${b & 0xff}%02x").mkString)
  }

  /** `resolver` with `${ivy.home}` in its patterns replaced by `ivyHome`:
    * sbt's `local` repository (where `publishLocal` writes) stays in the
    * user's Ivy home when the project's Ivy home moves.
    */
  def pinIvyHome(resolver: Resolver, ivyHome: File): Resolver = resolver match {
    case f: FileRepository if (f.patterns.ivyPatterns ++ f.patterns.artifactPatterns).exists(_.contains("${ivy.home}")) =>
      def pin(ps: Seq[String]) = ps.toVector.map(_.replace("${ivy.home}", ivyHome.getAbsolutePath))
      val p = f.patterns
      f.withPatterns(Patterns(pin(p.ivyPatterns), pin(p.artifactPatterns), p.isMavenCompatible, p.descriptorOptional, p.skipConsistencyCheck))
    case other => other
  }

  /** The mirror path of a URL prefix: `https://host:8080/a/` becomes
    * `<base>/https/host%3A8080/a/`. A prefix that is not an absolute
    * http(s) URL is not rewritten.
    */
  def rewritePrefix(prefix: String, base: String): Option[String] =
    if (prefix.startsWith(base.stripSuffix("/") + "/") || !(prefix.startsWith("http://") || prefix.startsWith("https://"))) None
    else {
      val slash = prefix.endsWith("/")
      val rel = CachePaths.relativePath(if (slash) prefix.stripSuffix("/") else prefix)
      // Cache names contain literal percent escapes (including host ports).
      // File URLs must preserve those percent signs through URI decoding;
      // HTTP mirrors route the raw cache key. Keep Ivy pattern tokens intact.
      val path = if (base.startsWith("file:")) rel.replace("%", "%25") else rel
      Some(base.stripSuffix("/") + "/" + path + (if (slash) "/" else ""))
    }

  /** The URL a mirror URL below `base` mirrors; any other string as it is. */
  def unrewritePrefix(s: String, base: String): String = {
    val root = base.stripSuffix("/") + "/"
    if (!s.startsWith(root)) s
    else {
      val raw = s.substring(root.length)
      val rel = if (base.startsWith("file:")) raw.replace("%25", "%") else raw
      val slash = rel.endsWith("/")
      CachePaths.url(rel.stripSuffix("/")) + (if (slash) "/" else "")
    }
  }

  /** `resolver` reading from the mirror at `base` instead of the network;
    * a resolver that is not a remote Maven or URL repository is left alone.
    */
  def rewrite(resolver: Resolver, base: String): Resolver = resolver match {
    case m: MavenRepository if !m.root.startsWith("file:") =>
      rewritePrefix(m.root, base).fold[Resolver](m) { root =>
        MavenRepository(m.name, root).withAllowInsecureProtocol(true)
      }
    case u: URLRepository =>
      def patterns(ps: Seq[String]) = ps.toVector.map(p => rewritePrefix(p, base).getOrElse(p))
      val p = u.patterns
      URLRepository(
        u.name,
        Patterns(patterns(p.ivyPatterns), patterns(p.artifactPatterns), p.isMavenCompatible, p.descriptorOptional, p.skipConsistencyCheck),
        true
      )
    case other => other
  }

  /** Resolver URLs before an enclosing launcher's mirror rewrite. */
  def unrewrite(resolver: Resolver, base: String): Resolver = resolver match {
    case m: MavenRepository => MavenRepository(m.name, unrewritePrefix(m.root,base))
    case u: URLRepository =>
      val p = u.patterns
      URLRepository(u.name, Patterns(p.ivyPatterns.map(unrewritePrefix(_,base)).toVector,
        p.artifactPatterns.map(unrewritePrefix(_,base)).toVector,
        p.isMavenCompatible,p.descriptorOptional,p.skipConsistencyCheck), true)
    case other => other
  }

  /** A local HTTP server in front of the network for the lock run: a GET or
    * HEAD of `/https/host/path` answers with the file of
    * `https://host/path`, downloaded once into `cache` at its coursier
    * path. An upstream 404 is a 404, and nothing is stored. A file of Maven
    * Central comes from `centralMirror` when it is given (see
    * [[CentralMirror]]); it is stored at the coursier path of its Central
    * URL all the same.
    */
  final class Server(cache: File, centralMirror: Option[String] = None) {
    private val server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 64)
    server.setExecutor(Executors.newFixedThreadPool(8))
    server.createContext(
      "/",
      new HttpHandler {
        def handle(exchange: HttpExchange): Unit =
          try serve(exchange)
          catch { case e: Exception => respond(exchange, 502, None) }
          finally exchange.close()
      }
    )
    server.start()

    val base: String = s"http://127.0.0.1:${server.getAddress.getPort}"

    def stop(): Unit = server.stop(0)

    private val lock = new Object

    private def serve(exchange: HttpExchange): Unit = {
      val rawPath = exchange.getRequestURI.getRawPath.stripPrefix("/")
      // The request path is the coursier path; coursier made its percent
      // escapes, so they stay as they are.
      val rel = if (rawPath.endsWith("/")) rawPath + ".directory" else rawPath
      val file = new File(cache, rel)
      val found = lock.synchronized {
        file.isFile || fetch(CentralMirror.downloadUrl(CachePaths.url(rel), centralMirror), file)
      }
      if (!found) respond(exchange, 404, None)
      else respond(exchange, 200, if (exchange.getRequestMethod == "HEAD") None else Some(file))
    }

    private def fetch(url: String, target: File): Boolean = {
      val conn = URI.create(url).toURL.openConnection().asInstanceOf[HttpURLConnection]
      conn.setInstanceFollowRedirects(true)
      conn.setRequestProperty("User-Agent", "sbt-nix-backend")
      conn.getResponseCode match {
        case 200 =>
          target.getParentFile.mkdirs()
          val tmp = new File(target.getPath + ".part")
          val in = conn.getInputStream
          try Files.copy(in, tmp.toPath, StandardCopyOption.REPLACE_EXISTING)
          finally in.close()
          Files.move(tmp.toPath, target.toPath, StandardCopyOption.REPLACE_EXISTING)
          true
        case 404 | 410 => false
        case code      => throw new IllegalStateException(s"$url: HTTP $code")
      }
    }

    private def respond(exchange: HttpExchange, code: Int, body: Option[File]): Unit = {
      val length = body.map(_.length).getOrElse(-1L)
      if (code == 200 && body.isEmpty) exchange.sendResponseHeaders(200, -1)
      else exchange.sendResponseHeaders(code, if (code == 200) length else -1)
      body.foreach(f => Files.copy(f.toPath, exchange.getResponseBody))
    }
  }
}
