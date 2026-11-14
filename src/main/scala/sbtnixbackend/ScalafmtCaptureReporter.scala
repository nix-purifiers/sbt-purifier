package sbtnixbackend

import java.io.{OutputStream, OutputStreamWriter, PrintWriter}
import java.lang.reflect.{InvocationHandler, Method, Proxy}

/** The native default reporter prints resolver exceptions and progress URLs.
  * Capture needs only the failure type; authenticated details must stay private.
  */
private[sbtnixbackend] final class ScalafmtCaptureReporter(loader: ClassLoader) {
  val api: Class[_] = loader.loadClass("org.scalafmt.interfaces.ScalafmtReporter")
  @volatile var failure: Option[String] = None
  private val output = new OutputStreamWriter(new OutputStream {
    override def write(value: Int): Unit = ()
  })
  private val writer = new PrintWriter(output)
  val value: AnyRef = Proxy.newProxyInstance(loader, Array(api), new InvocationHandler {
    override def invoke(proxy: AnyRef, method: Method, arguments: Array[AnyRef]): AnyRef =
      method.getName match {
        case "error" =>
          failure = Some(Option(arguments).toVector.flatMap(_.toVector).collectFirst {
            case error: Throwable => error.getClass.getName
          }.getOrElse("native configuration error"))
          null
        case "missingVersion" => failure = Some("missing configured formatter version"); null
        case "excluded" | "parsedConfig" => null
        case "downloadWriter" => writer
        case "downloadOutputStreamWriter" => output
        case "toString" => "Scalafmt capture reporter"
        case "hashCode" => java.lang.Integer.valueOf(System.identityHashCode(proxy))
        case "equals" => java.lang.Boolean.valueOf(arguments != null && arguments.length == 1 && (proxy eq arguments(0)))
        case _ => sys.error("unsupported native Scalafmt reporter capability: " + method.getName)
      }
  })

  def close(): Unit = writer.close()
}
