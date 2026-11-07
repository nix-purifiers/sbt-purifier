"""Actual FOD realization must precede a cached mutable-URL manifest read."""
import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import threading

parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--template-lock", type=Path, required=True)
parser.add_argument("--state", type=Path, required=True)
args = parser.parse_args()
state = args.state.resolve()
state.mkdir(parents=True, exist_ok=False)
env = dict(os.environ, HOME=str(state / "home"), XDG_CACHE_HOME=str(state / "cache"), TMPDIR=str(state / "tmp"))
for key in ("HOME", "XDG_CACHE_HOME", "TMPDIR"):
    Path(env[key]).mkdir()
requests = []
payload = b""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_GET(self):
        requests.append({"path": self.path, "payloadSha256": hashlib.sha256(payload).hexdigest()})
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}/ivy-realize-mutable-metadata.xml"
cache_path = f"http/127.0.0.1%3A{server.server_port}/ivy-realize-mutable-metadata.xml"
lock = state / "lock.json"
library = args.source.resolve() / "nix/lib/realize.nix"
common = [str(library), "--arg", "lockFile", str(lock)]


def run(command, label):
    log = state / (label + ".log")
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=state, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=60)
        except BaseException:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
    assert code == 0, log.read_text()
    return log.read_text()


def replace_lock(body):
    data = json.loads(args.template_lock.read_text())
    data["artifacts"] = [{"path": cache_path, "url": url, "mirrors": [], "hash": "sha256-" + base64.b64encode(hashlib.sha256(body).digest()).decode()}]
    version = data.pop("version")
    data.pop("contentHash")
    rest = json.dumps(data, sort_keys=True, separators=(",", ":"))[1:]
    head = '{\n  "version": ' + str(version) + ',\n'
    content_hash = hashlib.sha256((head + rest).encode()).hexdigest()
    lock.write_text(head + '  "contentHash": "' + content_hash + '",\n' + rest)


try:
    payload = f"<metadata><version>before-{server.server_port}</version></metadata>".encode()
    replace_lock(payload)
    # Deliberately populate the ordinary evaluator URL cache with old bytes.
    first = json.loads(run(["nix-instantiate", "--eval", "--strict", "--json", "--read-write-mode", *common, "-A", "manifest"], "seed-old-manifest"))
    assert Path(json.loads(Path(first).read_text())[cache_path]).read_bytes() == payload
    payload = f"<metadata><version>after-{server.server_port}</version></metadata>".encode()
    replace_lock(payload)
    built = run(["nix-build", "--no-out-link", "--quiet", *common, "-A", "fetches"], "realize-new-fetches").splitlines()
    outputs = [Path(line) for line in built if line.startswith("/nix/store/")]
    assert len(outputs) == 1 and outputs[0].read_bytes() == payload, "fetches must actually realize the new bytes before manifest evaluation"
    second = json.loads(run(["nix-instantiate", "--eval", "--strict", "--json", "--read-write-mode", *common, "-A", "manifest"], "new-manifest"))
    actual = Path(json.loads(Path(second).read_text())[cache_path])
    assert actual == outputs[0] and actual.read_bytes() == payload
    (state / "receipt.json").write_text(json.dumps({"status": "PASS", "scope": "host Nix evaluator/FOD regression; not native Ivy capability", "librarySha256": hashlib.sha256(library.read_bytes()).hexdigest(), "newFod": str(actual), "newPayloadSha256": hashlib.sha256(payload).hexdigest(), "manifest": second}, indent=2) + "\n")
finally:
    server.shutdown()
    server.server_close()
    (state / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")
