import java.nio.file.Files
import java.security.MessageDigest

val pinExpectedMarker = settingKey[String]("Expected loaded marker for this transition")
val pinObserve = taskKey[Unit]("Join native coordinates, runtime classpath and loaded origin")
pinObserve := {
  def hash(f: File): String = MessageDigest.getInstance("SHA-256")
    .digest(Files.readAllBytes(f.toPath)).map(b => f"${b & 255}%02x").mkString
  val report = update.value
  val cp = (Runtime / managedClasspath).value
  val backend = ivySbt.value.getClass
  val backendOrigin = new File(backend.getProtectionDomain.getCodeSource.getLocation.toURI)
  println("PIN_SDK\t" + Seq(sbtVersion.value, System.getProperty("java.runtime.version"),
    useCoursier.value.toString, updateOptions.value.cachedResolution.toString,
    backend.getName, backendOrigin.getCanonicalPath, hash(backendOrigin)).mkString("\t"))
  require(!useCoursier.value, "PIN_WRONG_RESOLVER_BACKEND")
  cp.foreach { entry =>
    val coordinate = entry.get(moduleID.key).map(m => Seq(m.organization, m.name, m.revision).mkString(":"))
    println("PIN_CLASSPATH\t" + coordinate.getOrElse("NO_MODULE") + "\t" + entry.data.getCanonicalPath + "\t" + hash(entry.data))
  }
  report.configurations.foreach { config =>
    (config.modules ++ config.details.flatMap(_.modules)).distinct.foreach { m =>
      println("PIN_MODULE\t" + Seq(config.configuration.toString, m.module.organization,
        m.module.name, m.module.revision, m.evicted.toString,
        m.callers.map(_.caller.toString).mkString("|")).mkString("\t"))
    }
  }
  val loader = new java.net.URLClassLoader(cp.map(_.data.toURI.toURL).toArray, null)
  try {
    val cls = loader.loadClass("proof.Marker")
    val origin = new File(cls.getProtectionDomain.getCodeSource.getLocation.toURI).getCanonicalFile
    val joins = cp.filter(_.data.getCanonicalFile == origin)
    require(joins.size == 1, "PIN_ORIGIN_JOIN must identify exactly one native classpath entry")
    val module = joins.head.get(moduleID.key).getOrElse(sys.error("PIN_ORIGIN_JOIN missing module coordinate"))
    val marker = cls.getMethod("value").invoke(null).toString
    println("PIN_LOADED\t" + Seq(module.organization, module.name, module.revision,
      marker, origin.toString, hash(origin)).mkString("\t"))
    require(module.organization == "proof" && module.name == "leaf", "PIN_WRONG_COORDINATE")
    require(marker == pinExpectedMarker.value, "PIN_WRONG_MARKER")
  } finally loader.close()
}
Test / test := pinObserve.value
