name := "mima-session-fixture"
scalaVersion := "2.13.18"
mimaPreviousArtifacts := Set("com.typesafe" % "config" % "1.4.10")
val verifyPrevious = taskKey[Unit]("Load the actual previous-release JAR")
verifyPrevious := {
  val jars = mimaPreviousClassfiles.value.values.toArray
  val loader = new java.net.URLClassLoader(jars.map(_.toURI.toURL), null)
  try {
    val factory = loader.loadClass("com.typesafe.config.ConfigFactory")
    val config = factory.getMethod("parseString", classOf[String]).invoke(null, "answer=42")
    val answer = loader.loadClass("com.typesafe.config.Config").getMethod("getInt", classOf[String]).invoke(config, "answer")
    assert(answer == Int.box(42))
    println("MIMA_PREVIOUS_API_42")
  } finally loader.close()
}
