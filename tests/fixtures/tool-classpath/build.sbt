name := "tool-classpath-fixture"
organization := "example.toolclasspath"
version := "1.0"
scalaVersion := "2.13.18"

val runtimeTool = taskKey[Unit]("A tool whose dependency is loaded only at runtime")
runtimeTool := ()

// Like a plugin's linker, this dependency is not in the project's ordinary
// compile/test classpaths. Test/compile alone never resolves it.
runtimeTool / fullClasspath := {
  dependencyResolution.value.retrieve(
    "com.typesafe" % "config" % "1.4.10",
    scalaModuleInfo = None,
    streams.value.cacheDirectory / "runtime-tool",
    streams.value.log
  ).fold(problem => throw problem.resolveException,
    files => files.toSeq.map(file => Attributed.blank(file)))
}

Test / test := {
  val classpath = (runtimeTool / fullClasspath).value
  val loader = new java.net.URLClassLoader(classpath.map(_.data.toURI.toURL).toArray, null)
  try {
    val factory = loader.loadClass("com.typesafe.config.ConfigFactory")
    val config = factory.getMethod("parseString", classOf[String]).invoke(null, "answer = 42")
    val api = loader.loadClass("com.typesafe.config.Config")
    val answer = api.getMethod("getInt", classOf[String]).invoke(config, "answer")
      .asInstanceOf[java.lang.Integer].intValue
    assert(answer == 42, s"unexpected runtime tool result: $answer")
    streams.value.log.info("Runtime tool ConfigFactory smoke passed")
  } finally loader.close()
}
