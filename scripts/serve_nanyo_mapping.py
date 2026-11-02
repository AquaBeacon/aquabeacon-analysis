"""Serve local results with byte ranges required for synchronized video seeking."""

import argparse
import functools
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        self.byte_range = None
        path = Path(self.translate_path(self.path))
        request = self.headers.get("Range")
        if not request or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", request)
        if not match:
            self.send_error(416)
            return None
        a, b = match.groups()
        start = int(a) if a else max(0, size - int(b))
        end = min(size - 1, int(b) if b and a else size - 1)
        if start > end or start >= size:
            self.send_error(416)
            return None
        stream = path.open("rb")
        stream.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.byte_range = end - start + 1
        return stream

    def end_headers(self):
        if self.byte_range is None:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def copyfile(self, source, outputfile):
        try:
            if self.byte_range is None:
                return super().copyfile(source, outputfile)
            remaining = self.byte_range
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--port", type=int, default=8872)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "results"
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), functools.partial(RangeHandler, directory=str(root))
    )
    print(
        f"Local mapping: http://127.0.0.1:{args.port}/20261102_nanyo_combined_000/mapping_replay/viewer.html",
        flush=True,
    )
    server.serve_forever()
