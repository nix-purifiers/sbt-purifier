#!/usr/bin/env python3
"""Source identity and immutable assets alongside the existing Maven release.

The shared publisher at fcd45819eb3027859492c392b292856608e8e90d uses
tracked path/mode/blob identities and exact git archives. Here the complete
tree is deliberately retained: sbt exports development inputs under tests/.
This conservative policy may publish documentation-only changes too.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess


def git(*args):
    return subprocess.check_output(["git", *args])


def fingerprint(revision):
    return hashlib.sha256(git("ls-tree", "-r", "-z", revision)).hexdigest()


def api(repository, suffix):
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/{suffix}"]))


def guard(repository, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or git("rev-parse", "HEAD").decode().strip() != sha:
        raise RuntimeError("Release checkout does not match the requested exact commit")
    if api(repository, "git/ref/heads/main")["object"]["sha"] != sha:
        raise RuntimeError("Main advanced; refusing to publish the stale commit")
    runs = api(repository, f"actions/workflows/check.yml/runs?branch=main&head_sha={sha}&per_page=100")
    if not any(run["head_sha"] == sha and run["head_branch"] == "main"
               and run["status"] == "completed" and run["conclusion"] == "success"
               and run["event"] in ("push", "workflow_dispatch") for run in runs["workflow_runs"]):
        raise RuntimeError("Exact main commit has no completed successful check workflow")


def prepare(repository, sha, version, destination):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid release version")
    if git("rev-parse", "HEAD").decode().strip() != sha:
        raise RuntimeError("Source archive must describe the release checkout")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"version": version, "series": version.rsplit(".", 1)[0],
                "commit": sha, "fingerprint": fingerprint(sha),
                "fingerprintPolicy": "git-full-tree-v1", "kind": "sbt",
                "mavenVersion": version}
    manifest_path = destination / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    archive = git("archive", "--format=tar", "--prefix=" + repository.split("/")[-1] + "/", sha)
    archive_path = destination / "source.tar.gz"
    archive_path.write_bytes(gzip.compress(archive, mtime=0))
    # Keep the legacy Maven-layout SHA256SUMS byte-for-byte compatible.
    (destination / "SOURCE-SHA256SUMS").write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
        for path in (manifest_path, archive_path)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity = commands.add_parser("fingerprint")
    identity.add_argument("revision")
    for name in ("guard", "prepare"):
        command = commands.add_parser(name)
        command.add_argument("repository")
        command.add_argument("sha")
        if name == "prepare":
            command.add_argument("version")
            command.add_argument("destination")
    args = parser.parse_args()
    if args.command == "fingerprint":
        print(fingerprint(args.revision))
    elif args.command == "guard":
        guard(args.repository, args.sha)
    else:
        prepare(args.repository, args.sha, args.version, args.destination)


if __name__ == "__main__":
    main()
