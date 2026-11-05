"""Local upload/job API, sharing the automatic CLI's persistent job manager."""
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .geography import public_profiles
from .jobs import JobManager

_EXCLUDED_ARTIFACTS = {"operator.json", "cancel", ".control.lock"}


class JobServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager):
        if address[0] not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            raise ValueError("Bind host must be 127.0.0.1, localhost or 0.0.0.0")
        self.manager = manager
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _reply(self, value, code=200):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _check_origin(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed:
            raise PermissionError("Invalid local Host header")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed}:
            raise PermissionError("Requests must originate from this local interface")

    def _route(self):
        self._check_origin()
        parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
        manager = self.server.manager
        if self.command == "GET" and parts == ["api", "capabilities"]:
            return self._reply({"schema_version": 1, "coverage": "conus", "max_upload_bytes": manager.max_upload_bytes,
                                "default_country": "US", "countries": public_profiles(),
                                "max_runtime_s": manager.max_runtime_s, "max_pending": manager.max_pending,
                                "provider_configured": bool(manager.config.get("cache_root") or manager.config.get("flight_indexes")),
                                "online_enabled": not bool(manager.config.get("offline", True))})
        if self.command == "POST" and parts == ["api", "jobs"]:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Use a Content-Length upload")
            if self.headers.get_content_type() != "application/octet-stream":
                raise ValueError("Upload raw audio as application/octet-stream")
            metadata = json.loads(self.headers.get("X-Dopplergeist-Request", "{}"))
            if not isinstance(metadata, dict) or "audio_path" in metadata:
                raise ValueError("Upload metadata must be a request object without audio_path")
            size = int(self.headers.get("Content-Length", "0"))
            suffix = Path(self.headers.get("X-Filename", "input.mp3")).suffix
            self.connection.settimeout(30)
            return self._reply(manager.submit(metadata, self.rfile, size, suffix=suffix), 202)
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            if self.command == "GET" and len(parts) == 3:
                from .jobs import status_for_display
                directory = manager.directory(job_id)
                return self._reply(status_for_display(directory, manager.get(job_id)))
            if self.command == "GET" and parts[3:] == ["guidance"]:
                from .guidance import explain_job
                return self._reply(explain_job(manager.directory(job_id)))
            if self.command == "POST" and parts[3:] == ["cancel"]:
                return self._reply(manager.cancel(job_id))
            if self.command == "POST" and parts[3:] == ["resume"]:
                return self._reply(manager.resume(job_id), 202)
            if self.command == "GET" and parts[3:] == ["artifacts"]:
                directory = manager.directory(job_id)
                artifacts = []
                for path in sorted(directory.rglob("*")):
                    if path.is_file() and not path.is_symlink() and path.name not in _EXCLUDED_ARTIFACTS and not path.name.endswith(".tmp"):
                        artifacts.append({"path": path.relative_to(directory).as_posix(), "bytes": path.stat().st_size})
                return self._reply({"artifacts": artifacts})
            if self.command == "GET" and len(parts) > 4 and parts[3] == "artifacts":
                directory = manager.directory(job_id)
                relative = Path(*parts[4:])
                path = (directory / relative).resolve()
                if (path == directory or directory not in path.parents or not path.is_file()
                        or any(part in _EXCLUDED_ARTIFACTS for part in relative.parts)
                        or relative.name.endswith(".tmp")):
                    raise KeyError("Unknown artifact")
                return self._file(path, download=path.suffix not in {".png", ".svg"})
        if self.command == "GET":
            asset = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                     "/leaflet.js": "leaflet.js", "/leaflet.css": "leaflet.css",
                     "/leaflet-license.txt": "leaflet-license.txt"}.get(urlsplit(self.path).path)
            if asset:
                return self._file(files("dopplergeist.web").joinpath(asset))
        raise KeyError("Unknown endpoint")

    def _file(self, path, download=False):
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name.replace(chr(34), "")}"')
        self.end_headers()
        with path.open("rb") as source:
            while block := source.read(1024*1024):
                self.wfile.write(block)

    def _handle(self):
        try:
            self._route()
        except PermissionError as exc:
            self._reply({"error": str(exc)}, 403)
        except KeyError as exc:
            self._reply({"error": str(exc)}, 404)
        except OverflowError as exc:
            self._reply({"error": str(exc)}, 429)
        except (ValueError, TimeoutError) as exc:
            self._reply({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _handle
    do_POST = _handle


def serve(root, config=None, *, host="127.0.0.1", port=8765, max_runtime_s=3600, max_upload_bytes=100*1024*1024):
    manager = JobManager(root, config, max_runtime_s=max_runtime_s, max_upload_bytes=max_upload_bytes)
    try:
        with JobServer((host, port), manager) as server:
            print(f"DOPPLERGEIST: http://127.0.0.1:{server.server_port} (bind: {host}; jobs: {manager.root})", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        manager.close()
