"""Threaded HTTP upload server with a streaming multipart/form-data parser.

Files are written to disk in 64 KiB chunks as they arrive, so uploads of any
size work without exhausting memory on the rescue machine. Each file lands as
"<name>.part" and is renamed only when fully received, so a dropped
connection never leaves a file that looks complete.
"""

import itertools
import json
import os
import re
import socket
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from .access import AccessControl
from .authcode import AuthCodeManager
from .config import APP_DIR
from .quarantine import harden_destination, protect_file
from .scanner import ClientScanner
from .web import PAGE_HTML

CHUNK_SIZE = 64 * 1024
PROGRESS_EVERY_BYTES = 4 * 1024 * 1024

_transfer_ids = itertools.count(1)


class MultipartError(Exception):
    pass


class _BoundedReader:
    """Never reads past Content-Length of the request body."""

    def __init__(self, raw, length):
        self.raw = raw
        self.remaining = length

    def read(self, size):
        if self.remaining <= 0:
            return b""
        data = self.raw.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


def _parse_part_headers(blob):
    headers = {}
    for line in blob.split(b"\r\n"):
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return headers


def _disposition_params(headers):
    disp = headers.get("content-disposition", "")
    params = {}
    for match in re.finditer(r'(\w+)="((?:[^"\\]|\\.)*)"', disp):
        params[match.group(1)] = match.group(2).replace('\\"', '"')
    return params


def iter_multipart(reader, boundary):
    """Yield (headers, body_chunks_iterator) per part. Bodies must be consumed
    in order; the caller of this generator drains any leftovers itself."""
    first_delim = b"--" + boundary
    delim = b"\r\n--" + boundary
    buf = b""

    def read_more():
        nonlocal buf
        chunk = reader.read(CHUNK_SIZE)
        if not chunk:
            raise MultipartError("connection closed mid-upload")
        buf += chunk

    # Skip preamble through the first boundary line.
    while True:
        idx = buf.find(first_delim)
        if idx != -1:
            buf = buf[idx + len(first_delim):]
            break
        if len(buf) > len(first_delim):
            buf = buf[-len(first_delim):]
        read_more()

    while True:
        while len(buf) < 2:
            read_more()
        if buf[:2] == b"--":
            return  # closing boundary
        if buf[:2] != b"\r\n":
            raise MultipartError("malformed multipart boundary")
        buf = buf[2:]

        while True:
            header_end = buf.find(b"\r\n\r\n")
            if header_end != -1:
                break
            if len(buf) > 64 * 1024:
                raise MultipartError("part headers too large")
            read_more()
        headers = _parse_part_headers(buf[:header_end])
        buf = buf[header_end + 4:]

        def body():
            nonlocal buf
            keep = len(delim) - 1
            while True:
                idx = buf.find(delim)
                if idx != -1:
                    data = buf[:idx]
                    buf = buf[idx + len(delim):]
                    if data:
                        yield data
                    return
                if len(buf) > keep:
                    emit, buf = buf[:-keep], buf[-keep:]
                    if emit:
                        yield emit
                read_more()

        body_iter = body()
        yield headers, body_iter
        for _ in body_iter:  # drain if handler stopped early
            pass


class ZipVerifyError(Exception):
    pass


def compress_and_verify(original, zip_part_path, arcname):
    """Write `original` into a zip at `zip_part_path` and prove the archive
    holds a byte-exact copy. Raises ZipVerifyError if anything is off."""
    with zipfile.ZipFile(zip_part_path, "w", zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        zf.write(original, arcname=arcname)
    with zipfile.ZipFile(zip_part_path, "r") as zf:
        if zf.testzip() is not None:
            raise ZipVerifyError("archive failed CRC check")
        try:
            info = zf.getinfo(arcname)
        except KeyError:
            raise ZipVerifyError("file missing from archive") from None
        original_size = os.path.getsize(original)
        if info.file_size != original_size:
            raise ZipVerifyError(
                f"size mismatch (zip {info.file_size} vs original {original_size})")
        with zf.open(arcname) as archived, open(original, "rb") as source:
            while True:
                a = archived.read(CHUNK_SIZE)
                b = source.read(CHUNK_SIZE)
                if a != b:
                    raise ZipVerifyError("archive content differs from original")
                if not a:
                    break
    return os.path.getsize(zip_part_path)


_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name, fallback):
    name = _SAFE_NAME.sub("_", os.path.basename(name.replace("\\", "/"))).strip(" .")
    return name or fallback


class _NameReservation:
    """Serializes filename allocation so concurrent uploads never collide."""

    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight = set()

    def claim(self, directory, name):
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        with self._lock:
            candidate, counter = name, 1
            while (directory / candidate).exists() or \
                  (directory / (candidate + ".part")).exists() or \
                  str(directory / candidate) in self._in_flight:
                candidate = f"{stem or name} ({counter}){'.' + ext if ext else ''}"
                counter += 1
            self._in_flight.add(str(directory / candidate))
            return directory / candidate

    def release(self, path):
        with self._lock:
            self._in_flight.discard(str(path))


class RefugeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Refuge"

    # -- admission control ---------------------------------------------------

    def _client_hostname(self, ip):
        """Best-effort reverse-DNS name for the connecting client."""
        try:
            return socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            return ""

    def _enter_request(self):
        """Admit this request through single-client + GUI-approval gating.
        Denied clients get a 404 (as if the site did not exist)."""
        ip = self.client_address[0]
        # Fingerprinting happens at the connection level (server.verify_request),
        # so it already ran for this client — including ones about to be denied.
        status = self.server.access.enter(
            ip,
            require_approval=getattr(self.server, "require_client_approval", False),
            single_client=getattr(self.server, "single_client_only", False),
            provider=lambda: self._client_hostname(ip))
        if status == "deny":
            self._send(404, "text/plain", b"Not found")
            return False
        return True

    def _leave_request(self):
        self.server.access.leave(self.client_address[0])

    # -- routing -------------------------------------------------------------

    def do_GET(self):
        if not self._enter_request():
            return
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
            elif self.path == "/files":
                self._send(200, "application/json",
                           json.dumps(self._list_received()).encode("utf-8"))
            elif self.path.startswith("/download/"):
                self._handle_download(self.path[len("/download/"):])
            else:
                self._send(404, "text/plain", b"Not found")
        finally:
            self._leave_request()

    def do_POST(self):
        if not self._enter_request():
            return
        try:
            if self.path == "/delete":
                self._handle_delete()
            elif self.path != "/upload":
                self._send(404, "text/plain", b"Not found")
            else:
                try:
                    saved = self._handle_upload()
                    self._send(200, "application/json",
                               json.dumps({"saved": saved}).encode())
                except MultipartError as exc:
                    self.server.bus.error(
                        f"Upload from {self.client_address[0]} failed: {exc}")
                    self._send(400, "text/plain", str(exc).encode())
                except OSError as exc:
                    self.server.bus.error(f"Disk error while receiving upload: {exc}")
                    self._send(500, "text/plain", b"Server storage error")
        finally:
            self._leave_request()

    # -- upload handling -----------------------------------------------------

    def _handle_upload(self):
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=([^;]+)", content_type)
        if "multipart/form-data" not in content_type or not match:
            raise MultipartError("expected multipart/form-data")
        boundary = match.group(1).strip('"').encode("latin-1")
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise MultipartError("missing Content-Length")

        reader = _BoundedReader(self.rfile, length)
        client = self.client_address[0]
        machine = ""
        saved = []
        # Overwrite of an existing file requires the GUI authorization code.
        # `ctx` carries the operator's intent and caches the one code check
        # per request so multiple files can be replaced with a single code.
        ctx = {"overwrite": False, "code": "", "decision": None}

        for headers, body in iter_multipart(reader, boundary):
            params = _disposition_params(headers)
            if "filename" not in params:
                value = b"".join(body).decode("utf-8", "replace").strip()
                field = params.get("name")
                if field == "machine":
                    machine = sanitize_name(value, "") if value else ""
                elif field == "overwrite":
                    ctx["overwrite"] = value in ("1", "true", "on", "yes")
                elif field == "code":
                    ctx["code"] = value
                continue
            saved.append(self._save_file(params["filename"], body, client,
                                         machine, length, ctx))
        return saved

    def _authorize_overwrite(self, target, ctx, client):
        """Decide whether `target` may be overwritten. The code is verified at
        most once per request and the result cached in ctx."""
        if not (self.server.allow_web_delete and ctx["overwrite"]
                and target.exists()):
            return False
        if ctx["decision"] is None:
            ctx["decision"] = self.server.authcodes.verify(
                ctx["code"], actor=client)
        return ctx["decision"] == "ok"

    def _save_file(self, raw_name, body, client, machine, request_size, ctx):
        transfer_id = next(_transfer_ids)
        bus = self.server.bus
        dest_root = Path(self.server.dest_dir)
        directory = dest_root / machine if machine else dest_root
        directory.mkdir(parents=True, exist_ok=True)

        name = sanitize_name(raw_name, f"unnamed-{transfer_id}")
        overwrite = self._authorize_overwrite(directory / name, ctx, client)
        if overwrite:
            # Replace the existing file in place (authorized). Write to a unique
            # temp part first, then atomically swap, so a failed transfer never
            # damages the file it is replacing.
            final_path = directory / name
            part_path = directory / f"{name}.{transfer_id}.part"
        else:
            final_path = self.server.names.claim(directory, name)
            part_path = final_path.with_name(final_path.name + ".part")

        bus.emit("transfer_start", id=transfer_id, client=client,
                 name=str(final_path.relative_to(dest_root)), total=request_size)
        written = 0
        last_report = 0
        try:
            with open(part_path, "wb") as fh:
                for chunk in body:
                    fh.write(chunk)
                    written += len(chunk)
                    if written - last_report >= PROGRESS_EVERY_BYTES:
                        last_report = written
                        bus.emit("transfer_progress", id=transfer_id, written=written)
            os.replace(part_path, final_path)
        except BaseException:
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            bus.emit("transfer_error", id=transfer_id, written=written)
            raise
        finally:
            if not overwrite:
                self.server.names.release(final_path)

        stored_path = final_path
        if self.server.compress_to_zip:
            stored_path = self._zip_rescued_file(final_path, directory)
        if self.server.block_execution:
            protect_file(stored_path, bus)

        bus.emit("transfer_done", id=transfer_id, written=written,
                 name=str(stored_path.relative_to(dest_root)))
        action = "Overwrote (authorized)" if overwrite else "Rescued"
        bus.success(f"{action} '{stored_path.name}' "
                    f"({written:,} bytes) from {client}"
                    f"{' [' + machine + ']' if machine else ''}")
        return stored_path.name

    def _zip_rescued_file(self, final_path, directory):
        """Compress a saved file into a verified zip, then remove the original.
        On any failure the original is kept - rescue data is never lost."""
        bus = self.server.bus
        zip_path = self.server.names.claim(directory, final_path.name + ".zip")
        zip_part = zip_path.with_name(zip_path.name + ".part")
        try:
            zip_size = compress_and_verify(final_path, zip_part, final_path.name)
            os.replace(zip_part, zip_path)
            final_path.unlink()
        except (ZipVerifyError, OSError, zipfile.BadZipFile) as exc:
            try:
                zip_part.unlink(missing_ok=True)
            except OSError:
                pass
            bus.error(f"Compression of '{final_path.name}' failed ({exc}) - "
                      "keeping the uncompressed original.")
            return final_path
        finally:
            self.server.names.release(zip_path)
        bus.info(f"Compressed '{final_path.name}' -> '{zip_path.name}' "
                 f"(verified byte-exact, {zip_size:,} bytes on disk); "
                 "original removed.")
        return zip_path

    # -- authorized delete ---------------------------------------------------

    def _resolve_within_dest(self, name):
        """Resolve a client-supplied relative name to a real path, refusing
        anything that escapes the rescue folder (path traversal defence)."""
        if not name:
            return None
        root = Path(self.server.dest_dir).resolve()
        try:
            candidate = (root / name).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None
        if candidate == root:
            return None
        return candidate

    def _handle_delete(self):
        bus = self.server.bus
        client = self.client_address[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if 0 < length <= 64 * 1024 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            name = str(payload.get("name", ""))
            code = str(payload.get("code", ""))
        except (ValueError, UnicodeDecodeError):
            self._send(400, "application/json",
                       json.dumps({"error": "Bad request."}).encode())
            return

        if not self.server.allow_web_delete:
            bus.warn(f"Delete of '{name}' from {client} refused: web "
                     "delete/overwrite is disabled in Settings.")
            self._send(403, "application/json",
                       json.dumps({"error": "Delete is disabled on this server."}).encode())
            return

        target = self._resolve_within_dest(name)
        if target is None or not target.is_file() or target.name.endswith(".part"):
            self._send(404, "application/json",
                       json.dumps({"error": "File not found."}).encode())
            return

        result = self.server.authcodes.verify(code, actor=client)
        if result == "locked":
            self._send(423, "application/json", json.dumps(
                {"error": "Locked: too many invalid codes. Wait, then use the "
                          "new code shown on the rescue machine."}).encode())
            return
        if result != "ok":
            self._send(403, "application/json", json.dumps(
                {"error": "Invalid or expired authorization code. Read the "
                          "current code from the rescue machine's screen."}).encode())
            return

        try:
            target.unlink()
        except OSError as exc:
            bus.error(f"Delete of '{name}' failed: {exc}")
            self._send(500, "application/json",
                       json.dumps({"error": "Delete failed on the server."}).encode())
            return

        bus.success(f"Deleted '{name}' by GUI authorization (requested from "
                    f"{client}). A new authorization code is now shown.")
        bus.emit("file_deleted", name=name, client=client)
        self._send(200, "application/json", json.dumps({"deleted": name}).encode())

    def _list_received(self):
        root = Path(self.server.dest_dir)
        files = []
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and not path.name.endswith(".part"):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue  # removed/renamed since rglob listed it
                    name = str(path.relative_to(root)).replace(os.sep, "/")
                    files.append({"name": name, "size": stat.st_size,
                                  "mtime": stat.st_mtime})
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return files[:200]

    def _resolve_rescued_file(self, raw_rel_path):
        """Resolve a /download/<path> URL segment to a Path inside dest_dir.
        Returns None if it's missing, still uploading (.part), or would
        escape the rescue folder."""
        root = Path(self.server.dest_dir).resolve()
        rel_path = unquote(raw_rel_path)
        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.name.endswith(".part"):
            return None
        return candidate

    def _handle_download(self, raw_rel_path):
        candidate = self._resolve_rescued_file(raw_rel_path)
        if candidate is None:
            self._send(404, "text/plain", b"Not found")
            return
        self._send_file(candidate)

    def _send_file(self, path):
        size = path.stat().st_size
        ascii_name = path.name.encode("ascii", "replace").decode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(path.name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- plumbing ------------------------------------------------------------

    def _send(self, status, content_type, body):
        if status >= 400:
            self.close_connection = True  # request body may be partially unread
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # route access log away from stderr
        pass


class _RefugeHTTPServer(ThreadingHTTPServer):
    """Routes handler-thread errors to the dashboard instead of stderr
    (stderr is invisible when launched with pythonw)."""

    daemon_threads = True

    def verify_request(self, request, client_address):
        # Runs for every accepted connection, before any HTTP parsing.
        ip = client_address[0]
        # Connection-level blocklist: drop the TCP connection outright so a
        # blocked host can't even reach the HTTP layer (no 404 — nothing).
        access = getattr(self, "access", None)
        if access is not None and access.connection_refused(ip):
            return False
        # Fingerprint every other connection attempt at the socket level —
        # probes that never send a valid HTTP request are caught here, not in
        # the request handlers. Always returns True for these; HTTP-level
        # admission/404 still applies to well-formed requests.
        scanner = getattr(self, "scanner", None)
        if scanner is not None:
            try:
                scanner.observe(ip)
            except Exception:
                pass
        return True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            self.bus.warn(f"Connection from {client_address[0]} dropped "
                          "mid-request (client machine may have gone down).")
        else:
            self.bus.error(f"Server error handling request from "
                           f"{client_address[0]}: {type(exc).__name__}: {exc}")


class UploadServer:
    """Owns the ThreadingHTTPServer and its serve loop thread."""

    def __init__(self, bus, config):
        self.bus = bus
        self.config = config
        self._httpd = None
        self._thread = None
        # The authorization code and client-approval decisions live on the
        # long-lived UploadServer (not the per-start httpd) so they survive a
        # settings-driven server restart.
        self.authcodes = AuthCodeManager(bus)
        self.access = AccessControl(bus)
        self.scanner = ClientScanner(bus, APP_DIR / "client_scans")

    @property
    def running(self):
        return self._httpd is not None

    def start(self):
        if self.running:
            return True
        Path(self.config.dest_dir).mkdir(parents=True, exist_ok=True)
        if self.config.block_execution:
            harden_destination(self.config.dest_dir, self.bus)
        try:
            httpd = _RefugeHTTPServer(
                (self.config.bind_address, int(self.config.port)), RefugeHandler)
        except OSError as exc:
            self.bus.error(f"Could not start server on port {self.config.port}: {exc}")
            return False
        httpd.bus = self.bus
        httpd.dest_dir = self.config.dest_dir
        httpd.block_execution = self.config.block_execution
        httpd.compress_to_zip = self.config.compress_to_zip
        httpd.allow_web_delete = self.config.allow_web_delete
        httpd.authcodes = self.authcodes
        httpd.require_client_approval = self.config.require_client_approval
        httpd.single_client_only = self.config.single_client_only
        httpd.access = self.access
        self.scanner.enabled = self.config.scan_clients
        if self.scanner.enabled:
            self.scanner.announce()  # confirm nmap presence up front
        httpd.scanner = self.scanner
        httpd.names = _NameReservation()
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, daemon=True, name="refuge-http")
        self._thread.start()
        self.bus.success(f"Upload server listening on port {self.config.port}. "
                         f"Saving to: {self.config.dest_dir}")
        self.bus.emit("server_state", running=True, port=self.config.port)
        return True

    def stop(self):
        if not self.running:
            return
        # Release any worker threads parked waiting for an approval answer.
        self.access.cancel_all()
        httpd, self._httpd = self._httpd, None
        httpd.shutdown()
        httpd.server_close()
        self.bus.info("Upload server stopped.")
        self.bus.emit("server_state", running=False, port=self.config.port)
