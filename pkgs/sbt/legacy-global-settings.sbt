// SPDX-License-Identifier: MIT
// Bootstrap the global plugin before its own Ivy settings can take effect.
fullResolvers := {
  val previous = fullResolvers.value
  if (sbtBinaryVersion.value != "0.13") previous
  else {
    val archive = "https://scala.jfrog.io/artifactory/api/download/ivy-releases/"
    val central = "https://repo1.maven.org/maven2/"
    val mirror = sys.env.get("SBT_NIX_IVY_MIRROR").orElse(sys.env.get("SBT_NIX_BOOT_MIRROR")).orElse {
      if (sys.env.get("SBT_NIX_MODE").exists(_ == "sandbox")) sys.env.get("COURSIER_CACHE").map(d => file(d).toURI.toString)
      else None
    }
    def rewrite(value: String): String = mirror.fold(value) { base =>
      if (value.startsWith(base.stripSuffix("/") + "/") || (!value.startsWith("https://") && !value.startsWith("http://"))) value
      else {
        val path = value.replaceFirst("://", "/")
        val escaped = path.map { c =>
          if (c > 128 || " %$&+,:;=?@<>#".contains(c)) "%%%02X".format(c.toInt) else c.toString
        }.mkString
        base.stripSuffix("/") + "/" + escaped
      }
    }
    val retained = previous.filter {
      case u: URLRepository => !(u.patterns.ivyPatterns ++ u.patterns.artifactPatterns).exists(p =>
        p.contains("repo.typesafe.com/") || p.contains("repo.scala-sbt.org/"))
      case _ => true
    }.map {
      case m: MavenRepository => MavenRepository(m.name, rewrite(m.root))
      case u: URLRepository => URLRepository(u.name, Patterns(
        u.patterns.ivyPatterns.map(rewrite), u.patterns.artifactPatterns.map(rewrite),
        u.patterns.isMavenCompatible, u.patterns.descriptorOptional, u.patterns.skipConsistencyCheck))
      case r => r
    }
    retained ++ Seq(
      MavenRepository("purifier-central", rewrite(central)),
      Resolver.url("purifier-sbt-archive", url(rewrite(archive)))(Resolver.ivyStylePatterns)
    )
  }
}
