#!/usr/bin/env python3
"""Reject effective sbt client configuration before a pinned foreground run."""
import argparse
import os
from pathlib import Path
import re
import sys

CLIENT = {"-client", "--client", "--jvm-client"}


def client_sources(arguments, environment, project, sbt_home, system_options=Path("/etc/sbt/sbtopts")):
    found = []
    def inspect(text, origin):
        if any(token.strip("\"'") in CLIENT for token in text.split()):
            found.append(origin)
    for argument in arguments:
        if argument in CLIENT:
            found.append("command arguments")
    inspect(environment.get("SBT_OPTS", ""), "SBT_OPTS")
    if environment.get("SBT_NATIVE_CLIENT") == "true":
        found.append("SBT_NATIVE_CLIENT")
    machine = system_options
    configured = Path((environment.get("XDG_CONFIG_HOME") or str(Path(environment["HOME"]) / ".config"))) / "sbt/sbtopts"
    if configured.is_file():
        machine = configured
    override = environment.get("SBT_ETC_FILE")
    if override and Path(override).is_file():
        machine = Path(override)
    if not machine.is_file():
        machine = sbt_home / "conf/sbtopts"
    for file in (machine, project / ".sbtopts"):
        if file.is_file():
            # Match the packaged runner's whitespace tokenization and comment
            # handling, without executing or expanding any configuration text.
            text = "\n".join(re.sub(r"(^#.*|\s+#.*)$", "", line.rstrip("\r"))
                             for line in file.read_text().splitlines())
            inspect(text, str(file))
    return list(dict.fromkeys(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbt-home", type=Path, required=True)
    args, arguments = parser.parse_known_args()
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    sources = client_sources(arguments, os.environ, Path.cwd(), args.sbt_home)
    if sources:
        print("sbt-purifier: pinned projects require foreground mode; remove client mode from "
              + ", ".join(sources) + ".", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
