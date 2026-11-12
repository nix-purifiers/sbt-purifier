package sbtnixbackend

class JvmOptionsSuite extends munit.FunSuite {
  private val defaults = Vector("-Xss4m", "-Xmx2g")

  test("large upstream heaps survive the capture fork without copying private properties") {
    val options = Vector("-Xms2g", "-Xmx8g", "-Xss2m", "-XX:ReservedCodeCacheSize=512m",
      "-Duser.home=/private/home", "-Dhttps.proxyPassword=secret", "-javaagent:/private/agent.jar")
    assertEquals(JvmOptions.inheritResources(defaults, options), options.take(4))
  }

  test("last resource option wins, including equivalent long JVM spellings") {
    assertEquals(
      JvmOptions.inheritResources(defaults, Vector("-Xmx4g", "-XX:MaxHeapSize=8589934592")),
      Vector("-Xss4m", "-XX:MaxHeapSize=8589934592")
    )
  }

  test("an initial heap without an explicit maximum does not inherit an incompatible fallback maximum") {
    assertEquals(JvmOptions.inheritResources(defaults, Vector("-Xms4g")), Vector("-Xss4m", "-Xms4g"))
  }

  test("unrelated options retain the existing fallback and explicit CPU limits are carried") {
    assertEquals(JvmOptions.inheritResources(defaults, Vector("-Dfile.encoding=UTF-8")), defaults)
    assertEquals(JvmOptions.inheritResources(defaults, Vector("-XX:ActiveProcessorCount=2")),
      defaults :+ "-XX:ActiveProcessorCount=2")
  }
}
