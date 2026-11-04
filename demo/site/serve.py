"""Serve demo/site on http://localhost:8090 (an allowed origin of the demo tenant).

python demo/site/serve.py [--port 8090]
"""

import argparse
import functools
import http.server
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8090)
args = parser.parse_args()
handler = functools.partial(
    http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parent)
)
with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
    print(f"Saffron & Sage demo site: http://localhost:{args.port}")
    server.serve_forever()
