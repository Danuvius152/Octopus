"""Controlled local App servers; real processes, ports and HTTP transports."""

import shlex
import sys
from pathlib import Path


def write_backend(directory: Path, source: str) -> None:
    script = directory / "start.sh"
    script.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-u", "-c", source]) + "\n")
    script.chmod(0o700)


def application(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    write_backend(directory, """
import http.server, os
from pathlib import Path
class Handler(http.server.BaseHTTPRequestHandler):
 def do_POST(self):
  body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
  Path('received').write_bytes(body)
  self.send_response(200)
  self.send_header('Content-Length', '2')
  self.end_headers()
  self.wfile.write(b'ok')
 def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
""")
    return directory
