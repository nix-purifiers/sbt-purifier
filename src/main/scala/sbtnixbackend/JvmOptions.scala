// SPDX-License-Identifier: MIT
package sbtnixbackend

import java.lang.management.ManagementFactory
import scala.collection.JavaConverters._

/** Carry explicit startup resource limits into capture and sandbox JVMs. */
private[sbtnixbackend] object JvmOptions {
  private val shortMemory = "(-Xms|-Xmx|-Xss)[0-9]+[kKmMgG]?".r
  private val longMemory =
    "-XX:(InitialHeapSize|MaxHeapSize|ThreadStackSize|InitialCodeCacheSize|ReservedCodeCacheSize|MetaspaceSize|MaxMetaspaceSize)=[0-9]+[kKmMgG]?".r
  private val processors = "-XX:ActiveProcessorCount=-?[0-9]+".r

  private def key(option: String): Option[String] = option match {
    case shortMemory(name) => Some(name)
    case longMemory("InitialHeapSize") => Some("-Xms")
    case longMemory("MaxHeapSize") => Some("-Xmx")
    case longMemory("ThreadStackSize") => Some("-Xss")
    case longMemory(name) => Some(name)
    case processors() => Some("ActiveProcessorCount")
    case _ => None
  }

  def inheritResources(defaults: Vector[String], arguments: Vector[String]): Vector[String] = {
    // Keep only numeric resource options. In particular, never copy -D
    // properties, proxy credentials, agents or host-specific file paths.
    val inherited = arguments.foldLeft(Vector.empty[(String, String)]) { (selected, option) =>
      key(option) match {
        case Some(name) => selected.filterNot(_._1 == name) :+ (name -> option)
        case None => selected
      }
    }
    val keys = inherited.map(_._1).toSet
    val heapSpecified = keys.contains("-Xms") || keys.contains("-Xmx")
    defaults.filterNot { option =>
      key(option).exists { name =>
        keys.contains(name) || (heapSpecified && (name == "-Xms" || name == "-Xmx"))
      }
    } ++ inherited.map(_._2)
  }

  def captureDefaults: Vector[String] = inheritResources(
    Vector("-Xss4m", "-Xmx2g"),
    ManagementFactory.getRuntimeMXBean.getInputArguments.asScala.toVector
  )
}
