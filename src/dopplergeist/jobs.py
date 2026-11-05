"""Persistent local jobs; each analysis has its own cancellable process group."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

from .contracts import validate_request, validate_result

TERMINAL = {"needs_input", "succeeded", "inconclusive", "failed", "cancelled"}
PROGRESS_STALE_AFTER_S = 60.0


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def status_for_display(job_dir, value=None, *, stale_after_s=PROGRESS_STALE_AFTER_S):
    """Hide active-job progress percentages when the persisted status is stale."""
    path = Path(job_dir) / "result.json"
    shown = dict(read_json(path) if value is None else value)
    shown["progress_stale"] = False
    if shown.get("status") in {"queued", "running"}:
        age = max(0.0, time.time() - path.stat().st_mtime)
        shown["progress_age_seconds"] = age
        shown["progress_stale"] = age > stale_after_s
        if shown["progress_stale"]:
            shown["progress"] = None
            shown["message"] = (
                f"Job remains {shown['status']}; saved progress is stale and its percentage is hidden."
            )
    return shown


def result(job_dir, status, **fields):
    value = validate_result(dict(schema_version=1, job_id=Path(job_dir).name, status=status, **fields))
    write_json(Path(job_dir) / "result.json", value)
    return value


def _evidence(value):
    return {key: value[key] for key in ("recording", "events", "candidates", "coverage", "artifacts") if key in value}


@contextmanager
def _control_lock(directory):
    """Serialize a local cancel request with the manager's final result."""
    with (directory / ".control.lock").open("a") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def request_local_cancel(root, job_id):
    """Request cancellation through an active manager's cooperative marker."""
    root = Path(root).resolve()
    try:
        if uuid.UUID(job_id).hex != job_id:
            raise ValueError
    except (AttributeError, ValueError):
        raise KeyError("Unknown job") from None
    directory = root / job_id
    result_path = directory / "result.json"
    if not result_path.is_file():
        raise KeyError("Unknown job")
    with _control_lock(directory):
        current = read_json(result_path)
        if current["status"] in TERMINAL:
            return current
        lock_path = root / ".manager.lock"
        try:
            with lock_path.open("rb") as lockfile:
                try:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    manager_active = True
                else:
                    fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
                    manager_active = False
        except FileNotFoundError:
            manager_active = False
        if not manager_active:
            raise ValueError("No active job manager owns this job directory")
        (directory / "cancel").touch(mode=0o600, exist_ok=True)
        return {"job_id": job_id, "status": current["status"], "stage": current.get("stage"),
                "cancellation_requested": True,
                "message": "The job will stop at its next cancellation check."}


class JobManager:
    def __init__(self, root, config=None, *, max_pending=4, max_runtime_s=3600, max_upload_bytes=100*1024*1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = config or {}
        if not isinstance(self.config, dict):
            raise ValueError("Operator configuration must be a JSON object")
        self.max_pending = max_pending
        self.max_runtime_s = max_runtime_s
        self.max_upload_bytes = max_upload_bytes
        if any(not math.isfinite(value) or value <= 0 for value in (max_pending, max_runtime_s, max_upload_bytes)):
            raise ValueError("Job limits must be positive")
        self._lockfile = (self.root / ".manager.lock").open("a")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lockfile.close()
            raise ValueError("Another job manager is using this directory") from None
        self._mutex = threading.RLock()
        self._uploads_changed = threading.Condition(self._mutex)
        self._uploads = 0
        self._serial = threading.Semaphore(1)
        self._threads = {}
        self._processes = {}
        self._closed = False
        # Preserve terminal outputs. An interrupted process is never silently replayed.
        for path in self.root.glob("*/result.json"):
            if read_json(path)["status"] in TERMINAL:
                continue
            with _control_lock(path.parent):
                old = read_json(path)
                if old["status"] not in TERMINAL:
                    if (path.parent / "cancel").exists():
                        result(path.parent, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(old))
                    else:
                        result(path.parent, "failed", reason_code="interrupted", message="Service stopped before completion; submit a new job to retry.", **_evidence(old))

    def directory(self, job_id):
        try:
            if uuid.UUID(job_id).hex != job_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise KeyError("Unknown job") from None
        path = self.root / job_id
        if not (path / "result.json").is_file():
            raise KeyError("Unknown job")
        return path

    def get(self, job_id):
        return read_json(self.directory(job_id) / "result.json")

    def submit(self, metadata, source, size, *, suffix=".mp3"):
        """Copy exactly size bytes from a binary stream before launching analysis."""
        if size <= 0 or size > self.max_upload_bytes:
            raise ValueError(f"Audio size must be 1..{self.max_upload_bytes} bytes")
        request = validate_request(dict(metadata, audio_path="pending"))
        with self._mutex:
            if self._closed:
                raise ValueError("Service is shutting down")
            if self._uploads + sum(thread.is_alive() for thread in self._threads.values()) >= self.max_pending:
                raise OverflowError("Job queue is full; try again after a job finishes")
            job_id = uuid.uuid4().hex
            directory = self.root / job_id
            directory.mkdir(mode=0o700)
            self._uploads += 1
        reserved = True
        try:
            suffix = suffix.lower() if suffix.lower() in {".mp3", ".wav", ".flac", ".m4a", ".ogg"} else ".mp3"
            audio = directory / ("input" + suffix)
            digest = hashlib.sha256()
            with audio.open("xb") as target:
                remaining = size
                while remaining:
                    block = source.read(min(1024*1024, remaining))
                    if not block:
                        result(directory, "failed", reason_code="incomplete_upload", message="Upload ended before its declared length.")
                        raise ValueError("Incomplete upload")
                    target.write(block)
                    digest.update(block)
                    remaining -= len(block)
            with self._mutex:
                if self._closed:
                    result(directory, "failed", reason_code="interrupted", message="Service stopped during upload.")
                    raise ValueError("Service is shutting down")
                request["audio_path"] = str(audio)
                write_json(directory / "request.json", request)
                write_json(directory / "operator.json", self.config)
                write_json(directory / "input.json", {"sha256": digest.hexdigest(), "bytes": size, "path": audio.name})
                queued = result(directory, "queued", stage="queued", progress=0)
                thread = threading.Thread(target=self._run, args=(directory,), daemon=True)
                self._threads[job_id] = thread
                thread.start()
                self._uploads -= 1
                reserved = False
                self._uploads_changed.notify_all()
                return queued
        finally:
            if reserved:
                with self._uploads_changed:
                    self._uploads -= 1
                    self._uploads_changed.notify_all()

    def _run(self, directory, resume=False):
        try:
            with self._serial:
                with self._mutex:
                    if self.get(directory.name)["status"] in TERMINAL:
                        return
                    env = dict(os.environ)
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
                    with (directory / "worker.log").open("ab") as log:
                        command = [sys.executable, "-m", "dopplergeist.jobs", "--execute", str(directory),
                                   "--parent-pid", str(os.getpid())]
                        if resume:
                            command.append("--resume")
                        process = subprocess.Popen(command,
                                                   stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
                    self._processes[directory.name] = process
                deadline = time.monotonic() + self.max_runtime_s
                while True:
                    try:
                        process.wait(timeout=max(0, min(0.5, deadline - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        if (directory / "cancel").exists():
                            with self._mutex:
                                self._stop(process)
                                current = self.get(directory.name)
                                if current["status"] not in TERMINAL:
                                    result(directory, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(current))
                            break
                        if time.monotonic() >= deadline:
                            self._stop(process)
                            with self._mutex, _control_lock(directory):
                                current = self.get(directory.name)
                                if current["status"] not in TERMINAL:
                                    if (directory / "cancel").exists():
                                        result(directory, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(current))
                                    else:
                                        result(directory, "inconclusive", reason_code="work_limit", message="Analysis exceeded the configured wall-time budget; inspect partial artifacts.", **_evidence(current))
                            break
                with self._mutex, _control_lock(directory):
                    current = self.get(directory.name)
                    if current["status"] not in TERMINAL:
                        if (directory / "cancel").exists():
                            result(directory, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(current))
                        else:
                            result(directory, "failed", reason_code="worker_exit", message=f"Analysis process exited ({process.returncode}); inspect worker.log.", **_evidence(current))
        except Exception as exc:
            with self._mutex, _control_lock(directory):
                current = self.get(directory.name)
                if current["status"] not in TERMINAL:
                    if (directory / "cancel").exists():
                        result(directory, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(current))
                    else:
                        result(directory, "failed", reason_code="worker_launch", message=f"Could not run analysis: {exc}", **_evidence(current))
        finally:
            with self._mutex:
                self._processes.pop(directory.name, None)
                self._threads.pop(directory.name, None)

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def cancel(self, job_id):
        with self._mutex:
            directory = self.directory(job_id)
            current = self.get(job_id)
            if current["status"] in TERMINAL:
                return current
            (directory / "cancel").touch()
            process = self._processes.get(job_id)
            if process:
                self._stop(process)
            current = self.get(job_id)
            if current["status"] in TERMINAL:
                return current
            return result(directory, "cancelled", message="Cancelled by the user; partial artifacts are preserved.", **_evidence(current))

    def resume(self, job_id):
        """Explicitly continue a stopped job after its complete audio checkpoint."""
        with self._mutex:
            directory = self.directory(job_id)
            current = self.get(job_id)
            if self._closed:
                raise ValueError("Service is shutting down")
            if current["status"] not in {"cancelled", "failed", "inconclusive"}:
                raise ValueError("Only stopped, failed, or inconclusive jobs can be resumed")
            if job_id in self._threads:
                raise ValueError("The previous worker is still stopping; try again")
            if sum(thread.is_alive() for thread in self._threads.values()) >= self.max_pending:
                raise OverflowError("Job queue is full; try again after a job finishes")
            from .checkpoint_resume import upgrade_legacy_job
            upgrade_legacy_job(directory)
            from .automatic import _load_audio_checkpoint
            _load_audio_checkpoint(directory, read_json(directory / "request.json"), read_json(directory / "operator.json"))
            write_json(directory / f"result-before-resume-{uuid.uuid4().hex}.json", current)
            (directory / "cancel").unlink(missing_ok=True)
            saved_audio = {key: current[key] for key in ("recording", "events") if key in current}
            queued = result(directory, "queued", stage="queued", progress=0, **saved_audio)
            thread = threading.Thread(target=self._run, args=(directory, True), daemon=True)
            self._threads[job_id] = thread
            thread.start()
            return queued

    def wait(self, job_id):
        thread = self._threads.get(job_id)
        if thread:
            thread.join()
        return self.get(job_id)

    def close(self):
        with self._mutex:
            self._closed = True
            self._uploads_changed.notify_all()
            for job_id, thread in list(self._threads.items()):
                if thread.is_alive():
                    self.cancel(job_id)
            while self._uploads:
                self._uploads_changed.wait()
        for thread in list(self._threads.values()):
            thread.join()
        self._lockfile.close()


def execute(directory, parent_pid=None, resume=False):
    # On Linux, an abruptly killed server must not leave an orphan analysis or
    # decoder consuming resources and later overwriting an interrupted result.
    if parent_pid is not None and sys.platform == "linux":
        import ctypes
        def terminate_group(signum, frame):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.killpg(os.getpgrp(), signal.SIGTERM)
        signal.signal(signal.SIGTERM, terminate_group)
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent_pid:
            terminate_group(signal.SIGTERM, None)
    from .automatic import analyze
    directory = Path(directory)
    request = read_json(directory / "request.json")
    config = read_json(directory / "operator.json")

    def progress(stage, fraction, **fields):
        if (directory / "cancel").exists():
            raise InterruptedError("Job cancelled")
        return result(directory, "running", stage=stage, progress=fraction, **fields)

    try:
        progress("intake", 0.02)
        final = analyze(request, directory, config, progress, resume=resume)
        if (directory / "cancel").exists():
            result(directory, "cancelled", message="Cancelled by the user.", **_evidence(read_json(directory / "result.json")))
        else:
            result(directory, **final)
    except InterruptedError:
        result(directory, "cancelled", message="Cancelled by the user.", **_evidence(read_json(directory / "result.json")))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        result(directory, "failed", reason_code="processing_error", message=f"{type(exc).__name__}: {exc}", **_evidence(read_json(directory / "result.json")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", required=True)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    execute(args.execute, args.parent_pid, resume=args.resume)
