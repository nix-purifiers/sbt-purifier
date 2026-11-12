package sbtnixbackend

import org.junit.Test
import org.junit.Assert._
import sbt._

class CompatibilityTest {
  @Test def captureMemoryOptions(): Unit = {
    assertEquals(Vector("-Xss4m", "-Xmx8g"), JvmOptions.inheritResources(
      Vector("-Xss4m", "-Xmx2g"), Vector("-Xmx8g", "-Dhttps.proxyPassword=secret")))
  }
  @Test def jsonRoundTrip(): Unit = {
    val value = Json.obj("message" -> Json.str("quote\"\n"), "array" -> Json.Arr(Vector(Json.str("value"))))
    assertEquals(value, Json.parse(value.render))
  }
  @Test def ivyMirrorRoundTrip(): Unit = {
    val url = "https://repo.example.org/maven/"
    val mirror = "file:/tmp/cache/"
    assertEquals(url, IvyMirror.unrewritePrefix(IvyMirror.rewritePrefix(url, mirror).get, mirror))
  }
  @Test def legacyBootRepository(): Unit = {
    assertTrue(BootRepositories.launcher("0.13.18").contains("/ivy-releases/org.scala-sbt/"))
    assertTrue(BootRepositories.contents(Some("file:/cache")).contains("file:/cache/https/scala.jfrog.io/"))
  }
  @Test def pluginVariant(): Unit = {
    assertEquals("scala2_10_sbt0_13", NixArtifacts.variantName(CrossVersion.Disabled, true, "0.13", "2.10.7"))
    assertEquals(Map("e:scalaVersion" -> "2.10", "e:sbtVersion" -> "0.13"), IvyLayout.pluginAttributes("2.10", "0.13"))
  }
}
