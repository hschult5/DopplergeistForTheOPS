"""Whole-recording, disk-backed decoding with explicit recording-clock provenance.

The first decoded sample is audio time zero. ffmpeg applies codec skip samples;
container PTS, file mtime, upload time and generic creation tags are not UTC clocks.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np


class AudioIntakeError(ValueError):
    """Predictable intake failure; ``code`` is suitable for a job reason_code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def source_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_media(source):
    """Probe the actual first audio stream, independent of filename extension."""
    path = Path(source)
    if not path.is_file():
        raise AudioIntakeError("unreadable_audio", "Audio source is not a readable file")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, check=True, text=True)
        probe = json.loads(result.stdout)
    except OSError as exc:
        raise AudioIntakeError("decoder_unavailable", "ffprobe could not start; check executable and runtime dependencies") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.returncode in (126, 127):
            raise AudioIntakeError("decoder_unavailable", "ffprobe could not start; check runtime dependencies") from exc
        raise AudioIntakeError("invalid_media", "Media probe failed; file is unsupported or corrupt") from exc
    audio = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise AudioIntakeError("no_audio_stream", "Media contains no audio stream")
    if int(audio[0].get("sample_rate", 0)) <= 0 or int(audio[0].get("channels", 0)) <= 0:
        raise AudioIntakeError("invalid_media", "Audio stream has invalid sample rate or channels")
    return probe


def _time_hints(probe):
    hints = []
    sources = [("format", probe.get("format", {}))]
    sources += [(f"stream:{i}", stream) for i, stream in enumerate(probe.get("streams", []))]
    for scope, info in sources:
        for key, value in info.get("tags", {}).items():
            if any(token in key.lower() for token in ("time", "date", "creation", "origination")):
                hints.append({"tag": key, "value": str(value), "scope": scope,
                              "usable_as_recording_clock": False})
    return hints


@dataclass
class IntakeRecording:
    """Decoded cache handle. Metadata is JSON; samples stay on disk."""

    samples_path: Path
    metadata: dict
    diagnostics: dict
    source_path: Path | None = None

    @property
    def time_status(self):
        return "ready" if self.metadata.get("time_window") is not None else "needs_input"

    def iter_chunks(self, chunk_s=120.0, overlap_s=60.0):
        sr = self.metadata["sample_rate_hz"]
        if not np.isfinite(chunk_s) or not np.isfinite(overlap_s) or not 0 <= overlap_s < chunk_s:
            raise ValueError("Require finite 0 <= overlap_s < chunk_s")
        size, overlap = int(round(chunk_s * sr)), int(round(overlap_s * sr))
        if size <= overlap:
            raise ValueError("Chunk step must be at least one sample")
        count = self.samples_path.stat().st_size // 4
        with self.samples_path.open("rb", buffering=0) as data:
            for first in range(0, count, size - overlap):
                data.seek(first * 4)
                yield first / sr, np.fromfile(data, dtype="<f4", count=min(size, count - first))
                if first + size >= count:
                    break


def intake_recording(source, output_dir, *, time_window=None, sample_rate_hz=4000,
                     resample_filter_size=None):
    """Decode mono f32 into a new cache without holding the whole input in RAM.

    ``time_window`` follows the shared RecordingTimeWindow contract and bounds
    the UTC time of sample zero. Metadata hints require human confirmation via
    that explicit window: tags alone do not provide clock error or edit history.
    A missing window permits acoustic analysis and yields ``needs_input`` for
    historical matching. Existing cache files are never overwritten.
    ``resample_filter_size`` opts into the explicit swresample filter used by
    experimental episode discovery; omission preserves the standard decoder.
    """
    from .contracts import validate_recording, validate_request

    if type(sample_rate_hz) is not int or not 1000 <= sample_rate_hz <= 192000:
        raise ValueError("sample_rate_hz must be an integer between 1000 and 192000")
    if resample_filter_size is not None and (type(resample_filter_size) is not int or resample_filter_size <= 0):
        raise ValueError("resample_filter_size must be a positive integer")
    source = Path(source).resolve()
    request = validate_request({"schema_version": 1, "audio_path": str(source), "time_window": time_window})
    probe = probe_media(source)
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    before = source.stat()
    digest = source_hash(source)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    samples_path, metadata_path = output / "recording.f32", output / "recording.json"
    if samples_path.exists() or metadata_path.exists():
        raise FileExistsError("Intake output already exists; use a new output directory")
    channels = int(audio["channels"])
    downmix = "pan=mono|c0=" + "+".join(f"{1 / channels:.17g}*c{i}" for i in range(channels))
    if resample_filter_size is not None:
        downmix += f",aresample=resampler=swr:filter_size={resample_filter_size}:cutoff=0.95"
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(source),
               "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", downmix, "-ac", "1", "-ar", str(sample_rate_hz),
               "-acodec", "pcm_f32le", "-f", "f32le", "pipe:1"]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output, prefix=".decode-", delete=False) as cache, \
                tempfile.TemporaryFile() as errors:
            temporary = Path(cache.name)
            try:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            except OSError as exc:
                raise AudioIntakeError("decoder_unavailable", "ffmpeg could not start; check executable and runtime dependencies") from exc
            count, peak, remainder = 0, 0.0, b""
            try:
                while block := process.stdout.read(256 * 1024):
                    block = remainder + block
                    usable = len(block) - len(block) % 4
                    values = np.frombuffer(block[:usable], dtype="<f4")
                    if not np.all(np.isfinite(values)):
                        raise AudioIntakeError("invalid_samples", "Decoded audio contains nonfinite samples")
                    if len(values):
                        peak = max(peak, float(np.max(np.abs(values))))
                    count += len(values)
                    cache.write(block[:usable])
                    remainder = block[usable:]
                code = process.wait()
                if code in (126, 127):
                    raise AudioIntakeError("decoder_unavailable", "ffmpeg could not start; check runtime dependencies")
                if code or remainder:
                    raise AudioIntakeError("decode_failed", "Audio decode failed; media may be truncated or corrupt")
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()
            if not count:
                raise AudioIntakeError("empty_audio", "Decoded audio contains no samples")
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise AudioIntakeError("source_changed", "Audio source changed during intake")
        metadata = validate_recording({
            "source_sha256": digest, "duration_s": count / sample_rate_hz,
            "sample_rate_hz": sample_rate_hz, "channels": 1,
            "source_format": probe["format"].get("format_name", audio.get("codec_name", "unknown")),
            "time_window": request.get("time_window"),
        })
        diagnostics = {
            "sample_count": count, "peak_amplitude": peak,
            "time_status": "ready" if time_window is not None else "needs_input",
            "time_hints": _time_hints(probe),
            "decode": {"decoder": "ffmpeg", "sample_format": "f32le", "channel_policy": "arithmetic channel mean",
                       "source_sample_rate_hz": int(audio["sample_rate"]), "source_channels": int(audio["channels"]),
                       "source_codec": audio.get("codec_name", "unknown"), "stream_index": int(audio["index"]),
                       "audio_time_zero": "first decoded sample after codec skip/discard", "command": command},
        }
        if resample_filter_size is not None:
            diagnostics["decode"].update(
                resampling=f"ffmpeg swresample, filter_size {resample_filter_size}, cutoff 0.95; timestamps start at decoded sample zero",
                usable_frequency_hz=[0.0, .45 * min(sample_rate_hz, int(audio["sample_rate"]))])
        # Exclusive creation also prevents accidental replacement on concurrent intake.
        os.link(temporary, samples_path)
        with metadata_path.open("x") as target:
            target.write(json.dumps(metadata, indent=2, allow_nan=False) + "\n")
        with (output / "intake.json").open("x") as target:
            target.write(json.dumps(diagnostics, indent=2, allow_nan=False) + "\n")
        return IntakeRecording(samples_path, metadata, diagnostics, source)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
