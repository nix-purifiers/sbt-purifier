package sbtnixbackend

import java.io.File
import java.nio.charset.StandardCharsets
import java.nio.file.Files

/** The stable implementation envelope, separate from dependency lock versions. */
private[sbtnixbackend] object ImplementationPin {
  import Json._

  def validate(pin: Json): Json = {
    val p = pin.asObject
    val source = p("source").asObject
    def requirePin(ok: Boolean): Unit =
      if (!ok) throw new JsonError("invalid purifier implementation pin; restore the lock or explicitly update the purifier")
    requirePin(p.fields.map(_._1).toSet == Set("version", "tool", "source"))
    requirePin(p("version").asInt == 1 && p("tool").asString == "sbt-purifier")
    source("type").asString match {
      case "github" =>
        requirePin(source.fields.map(_._1).toSet == Set("type", "owner", "repo", "rev", "narHash"))
        Seq("owner", "repo").foreach(key => requirePin(source(key).asString.matches("[A-Za-z0-9][A-Za-z0-9_.-]*")))
      case "git" =>
        requirePin(source.fields.map(_._1).toSet == Set("type", "url", "rev", "narHash"))
        val url = new java.net.URI(source("url").asString)
        requirePin(Set("https", "ssh", "file")(url.getScheme) && url.getQuery == null && url.getFragment == null)
        requirePin(Option(url.getPath).exists(_.startsWith("/")) && (url.getScheme == "file" || url.getHost != null))
      case _ => requirePin(false)
    }
    requirePin(source("rev").asString.matches("[0-9a-f]{40}"))
    requirePin(source("narHash").asString.matches("sha256-[A-Za-z0-9+/]{43}="))
    pin
  }

  def installed: Option[Json] = sys.env.get("SBT_PURIFIER_IMPLEMENTATION_CONFIG").filter(_.nonEmpty).flatMap { path =>
    val config = Json.parse(new String(Files.readAllBytes(new File(path).toPath), StandardCharsets.UTF_8)).asObject
    config("source") match {
      case Null => None
      case source => Some(validate(obj("version" -> num(1), "tool" -> str("sbt-purifier"), "source" -> source)))
    }
  }

  def requireMatch(pin: Json, active: Option[Json]): Unit = {
    validate(pin)
    def identity(p: Json): Vector[Json] = {
      val source = validate(p).asObject("source").asObject
      Vector(source("rev"), source("narHash"))
    }
    val expected = identity(pin)
    val actual = active.map(identity)
    if (!actual.exists(_ == expected))
      sys.error("sbt-purifier: this project pins another purifier implementation; use its sbt wrapper to select the pinned plugin")
  }
}
