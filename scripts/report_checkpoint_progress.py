#!/usr/bin/env python3
"""Read-only, bounded accounting of saved coarse search checkpoints."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time

MIB = 1024 * 1024
TILE_ID = r"-?\d+_-?\d+(?:~(?:[0-3]|t[01]))*"
RECEIPT_NAME = re.compile(rf"tile-({TILE_ID})\.json\Z")
SPLIT_NAME = re.compile(rf"split-({TILE_ID})\.json\Z")
METRICS = {
    "grid_points": "recorded receiver-point visits, not geographic coverage",
    "tracks": "recorded track-segment visits, not unique aircraft",
    "cpa_rows": "CPA rows",
    "candidates": "association candidates",
    "hypotheses_examined": "association hypotheses examined",
    "work_units": "association work units",
    "unclassified_score_candidates": "unclassified candidate assessments",
    "bounded_score_candidates": "candidate assessments with a score bound",
    "unbounded_score_candidates": "candidate assessments without a score bound",
    "evaluated_rejections": "evaluated candidate rejections",
    "evaluated_trials": "clock trial visits evaluated",
    "rejected_trials": "rejected clock-trial visits",
    "unknown_trials": "unresolved clock trial visits",
    "unreported_trial_envelopes": "candidate assessments without trial accounting",
    "unsupported_cpa_tracks": "unsupported track evaluations",
    "ranked": "locally retained mode appearances, not globally unique modes",
}


def content_hash(value):
    # Exact data_access.content_hash convention, including JSON whitespace.
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def nonnegative_int(value):
    return type(value) is int and value >= 0


def cells_tuple(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(type(n) is int for n in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError("invalid half-open cell rectangle")
    return tuple(value)


def fingerprint(path):
    if path.is_symlink():
        raise ValueError("symlinked input files are not read")
    st = path.stat()
    if not path.is_file():
        raise ValueError("input is not a regular file")
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def strict_json(raw):
    def invalid_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")
    return json.loads(raw, parse_constant=invalid_constant)


def receipt_summary(record):
    payload = record["payload"]
    if not isinstance(record.get("run_hash"), str) or re.fullmatch(r"[0-9a-f]{64}", record["run_hash"]) is None:
        raise ValueError("invalid producer identity")
    if record.get("payload_hash") != content_hash(payload):
        raise ValueError("payload hash mismatch")
    cells = cells_tuple(payload["cells"])
    if (not isinstance(payload.get("counts"), dict) or not isinstance(payload.get("terrain_failures"), list)
            or not isinstance(payload.get("ranked"), list)):
        raise ValueError("unrecognized checkpoint schema")
    metrics = {}
    for name in METRICS:
        value = (payload if name in {"grid_points", "tracks", "ranked"} else payload["counts"]).get(name)
        if name in {"ranked", "unsupported_cpa_tracks"}:
            value = len(value) if isinstance(value, list) else None
        metrics[name] = value if nonnegative_int(value) else None
    return {"run_hash": record.get("run_hash"), "cells": list(cells),
            "retention_checksum": payload.get("retention_checksum"),
            "terrain_failures": len(payload["terrain_failures"]), "metrics": metrics}


class Reader:
    """Reuse reduced summaries while file identity and timestamps match."""

    def __init__(self, remaining, file_limit, entries):
        self.remaining, self.file_limit, self.entries = remaining, file_limit, entries
        self.stats = Counter(bytes_read=0, files_read=0, files_reused=0)

    def read(self, path, reducer=lambda value: value, limit=None):
        key = str(path)
        try:
            before = fingerprint(path)
            prior = self.entries.get(key, {})
            if prior.get("fingerprint") == before and "result" in prior:
                self.stats["files_reused"] += 1
                return prior["result"]
            maximum = min(self.file_limit, limit if limit is not None else self.file_limit)
            if before[2] > maximum:
                return {"status": "file_size_limit"}
            if before[2] > self.remaining:
                return {"status": "refresh_byte_budget"}
            with path.open("rb") as handle:
                raw = handle.read(before[2])
            self.remaining -= len(raw)
            self.stats["bytes_read"] += len(raw)
            self.stats["files_read"] += 1
            if len(raw) != before[2] or fingerprint(path) != before:
                return {"status": "changed_during_read"}
            try:
                result = {"status": "ok", "value": reducer(strict_json(raw))}
            except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
                result = {"status": "invalid_content", "reason": str(error)}
            self.entries[key] = {"fingerprint": before, "result": result}
            return result
        except (OSError, ValueError) as error:
            return {"status": "unreadable", "reason": str(error)}


def inventory(directory):
    result = {}
    for path in directory.iterdir():
        if path.name.startswith(("tile-", "split-", "fine-tile-")) and path.name.endswith(".json"):
            try:
                result[path.name] = fingerprint(path)
            except (OSError, ValueError) as error:
                result[path.name] = str(error)
    return result


def ledger_snapshot(path, run_hash):
    """Read only the indexed stages metadata; never records or bounds."""
    result = {"status": "missing", "stages": {}, "record_and_bound_content_rehashed": False}
    if not path.exists():
        return result
    try:
        fingerprint(path)
        with path.open("rb") as handle:
            header = handle.read(100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
            raise ValueError("invalid SQLite database header")
        if header[18:20] not in (b"\x01\x01", b"\x02\x02"):
            raise ValueError("unsupported SQLite journal format")
        wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
        result["journal_mode_observed"] = "wal" if header[18] == 2 else "rollback"
        if header[18] == 2:
            # mode=ro can still create sidecars for a writable directory. Inspect
            # the database header even when its WAL is missing or empty.
            for sidecar, minimum in ((wal, 32), (shm, 32768)):
                if not sidecar.exists() or fingerprint(sidecar)[2] < minimum:
                    raise ValueError("WAL database needs existing readable nonempty WAL/SHM files; canonical agreement is unavailable")
                with sidecar.open("rb") as handle:
                    if len(handle.read(32)) != 32:
                        raise ValueError("unreadable WAL/SHM header")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA cache_size=-2048")
            db.execute("BEGIN")
            identities = db.execute("SELECT identity,score_kind FROM run LIMIT 2").fetchall()
            if len(identities) != 1 or identities[0][0] != run_hash:
                raise ValueError("ledger identity differs from manifest")
            result["score_kind"] = identities[0][1]
            rows = db.execute("SELECT name,checksum FROM stages "
                              "WHERE name >= 'coarse:' AND name < 'coarse;' LIMIT 100001").fetchall()
            if len(rows) > 100000:
                raise ValueError("stage metadata exceeds the 100000-row report limit")
            result["stages"] = {name.removeprefix("coarse:"): checksum for name, checksum in rows}
        finally:
            db.close()
        result["status"] = "metadata_snapshot"
    except (OSError, ValueError, sqlite3.Error) as error:
        result.update(status="unavailable", reason=str(error), stages={})
    return result


class Grid:
    """Reproduce the integer grid with at most one root's coordinate arrays."""

    def __init__(self, config, cache, max_points, skip=False):
        self.roots, self.reason, self.point_reason = None, None, None
        self.cache, self.runtime = cache, {}
        try:
            if skip:
                raise ValueError("grid reconstruction disabled by --skip-grid")
            import numpy as np
            import pyproj
            self.np = np
            self.runtime = {"numpy": np.__version__, "pyproj": pyproj.__version__, "proj": pyproj.proj_version_str}
            self.bounds, self.spacing = config["bounds"], config["budget"]["spacing_m"]
            size = config["budget"]["tile_cells"]
            if (not isinstance(self.bounds, list) or len(self.bounds) != 4
                    or not all(type(x) in (int, float) and math.isfinite(x) for x in self.bounds)
                    or not -180 <= self.bounds[0] < self.bounds[2] <= 180
                    or not -90 <= self.bounds[1] < self.bounds[3] <= 90
                    or type(self.spacing) not in (int, float) or not math.isfinite(self.spacing)
                    or self.spacing <= 0 or type(size) is not int or not 1 <= size <= 256):
                raise ValueError("invalid saved grid configuration")
            forward = pyproj.Transformer.from_crs("EPSG:4326", config["crs"], always_xy=True)
            bounds = forward.transform_bounds(*self.bounds, densify_pts=101)
            x0, y0 = (math.floor(value / self.spacing) for value in bounds[:2])
            x1, y1 = (math.ceil(value / self.spacing) for value in bounds[2:])
            if (x1 // size - x0 // size + 1) * (y1 // size - y0 // size + 1) > 100000:
                raise ValueError("grid exceeds the 100000-root report limit")
            self.roots = {f"{ix}_{iy}": (max(x0, ix * size), max(y0, iy * size),
                                         min(x1 + 1, (ix + 1) * size), min(y1 + 1, (iy + 1) * size))
                          for iy in range(y0 // size, y1 // size + 1)
                          for ix in range(x0 // size, x1 // size + 1)}
            self.inverse = pyproj.Transformer.from_crs(config["crs"], "EPSG:4326", always_xy=True)
            self.key = content_hash([self.bounds, self.spacing, config["crs"], self.runtime, config.get("receiver_mask")])
            if config.get("receiver_mask") is not None:
                self.point_reason = "receiver mask present; its geometry is not loaded or approximated"
            elif (x1 - x0 + 1) * (y1 - y0 + 1) > max_points:
                self.point_reason = "projected grid exceeds --max-grid-points"
        except ImportError as error:
            self.reason = f"grid dependency unavailable: {error}; install the project or use --skip-grid"
        except (KeyError, TypeError, ValueError, OverflowError, RuntimeError) as error:
            self.roots = None
            self.reason = str(error)

    def count(self, cells):
        if self.roots is None or self.point_reason is not None:
            return None
        key = self.key + ":" + ",".join(map(str, cells))
        area = (cells[2] - cells[0]) * (cells[3] - cells[1])
        saved = self.cache.get(key)
        if nonnegative_int(saved) and saved <= area:
            return saved
        np = self.np
        x, y = np.meshgrid(np.arange(cells[0], cells[2], dtype=np.int64), np.arange(cells[1], cells[3], dtype=np.int64))
        lon, lat = self.inverse.transform(x.ravel() * self.spacing, y.ravel() * self.spacing)
        value = int(np.count_nonzero((lon >= self.bounds[0]) & (lon <= self.bounds[2])
                                    & (lat >= self.bounds[1]) & (lat <= self.bounds[3])))
        self.cache[key] = value
        return value


def children_for(tile_id, cells):
    x0, y0, x1, y1 = cells
    xs = [x0, (x0 + x1) // 2, x1] if x1 - x0 > 1 else [x0, x1]
    ys = [y0, (y0 + y1) // 2, y1] if y1 - y0 > 1 else [y0, y1]
    if len(xs) == len(ys) == 2:
        if sum(part.startswith("t") for part in tile_id.split("~")[1:]) >= 24:
            return [], True
        return [[f"{tile_id}~t{i}", list(cells)] for i in range(2)], True
    parts = [(xs[i], ys[j], xs[i + 1], ys[j + 1])
             for j in range(len(ys) - 1) for i in range(len(xs) - 1)]
    return [[f"{tile_id}~{i}", list(part)] for i, part in enumerate(parts)], False


def sum_known(values):
    values = list(values)
    return sum(values) if all(value is not None for value in values) else None


class Coverage:
    def __init__(self, grid, receipts, splits, run_hash, ledger):
        self.grid, self.receipts, self.splits, self.run_hash = grid, receipts, splits, run_hash
        self.stages = ledger["stages"] if ledger["status"] == "metadata_snapshot" else None
        self.active, self.agreed, self.visited, self.superseded = set(), set(), set(), set()
        self.reasons, self.clock_splits = Counter(), 0

    def empty(self, cells, reason):
        self.reasons[reason] += 1
        zero = 0 if self.grid.count(cells) is not None else None
        return {"receipt_full": False, "ledger_full": False, "receipt_any": False, "ledger_any": False,
                "receipt_points": zero, "ledger_points": zero if self.stages is not None else None}

    def walk(self, tile_id, cells):
        self.visited.add(tile_id)
        if tile_id in self.splits:
            if tile_id in self.receipts:
                self.superseded.add(tile_id)
            saved = self.splits[tile_id]
            if saved["status"] != "ok":
                return self.empty(cells, "split_" + saved["status"])
            children, temporal = children_for(tile_id, cells)
            if not children or saved["value"] != {"run_hash": self.run_hash, "cells": list(cells), "children": children}:
                return self.empty(cells, "invalid_or_foreign_split")
            self.clock_splits += int(temporal)
            pieces = [self.walk(child, tuple(child_cells)) for child, child_cells in children]
            result = {}
            for scope in ("receipt", "ledger"):
                full = all(piece[scope + "_full"] for piece in pieces)
                result[scope + "_full"] = full
                result[scope + "_any"] = any(piece[scope + "_any"] for piece in pieces)
                if temporal:
                    points = self.grid.count(cells)
                    result[scope + "_points"] = points if full else 0 if points is not None else None
                else:
                    result[scope + "_points"] = sum_known(piece[scope + "_points"] for piece in pieces)
            if self.stages is None:
                result["ledger_points"] = None
            return result
        saved = self.receipts.get(tile_id, {"status": "missing_receipt"})
        if saved["status"] != "ok":
            return self.empty(cells, saved["status"])
        value = saved["value"]
        if value["run_hash"] != self.run_hash:
            return self.empty(cells, "foreign_producer_identity")
        if tuple(value["cells"]) != cells:
            return self.empty(cells, "cell_rectangle_mismatch")
        if value["terrain_failures"]:
            return self.empty(cells, "recorded_terrain_failure")
        self.active.add(tile_id)
        matched = (self.stages is not None and tile_id in self.stages
                   and isinstance(value["retention_checksum"], str)
                   and value["retention_checksum"] == self.stages[tile_id])
        if matched:
            self.agreed.add(tile_id)
        elif self.stages is not None:
            self.reasons["missing_canonical_stage" if tile_id not in self.stages else "canonical_checksum_mismatch"] += 1
        points = self.grid.count(cells)
        return {"receipt_full": True, "ledger_full": matched, "receipt_any": True, "ledger_any": matched,
                "receipt_points": points,
                "ledger_points": (points if matched else 0 if points is not None else None)
                                 if self.stages is not None else None}

    def report(self, max_tiles):
        if self.grid.roots is None:
            self.active = {name for name, saved in self.receipts.items()
                           if name not in self.splits and saved["status"] == "ok"
                           and saved["value"]["run_hash"] == self.run_hash and not saved["value"]["terrain_failures"]}
            return {"status": "unavailable", "reason": self.grid.reason}
        roots = list(self.grid.roots.items())
        scheduled = roots[:max_tiles] if max_tiles is not None else roots
        requested_points = sum_known(self.grid.count(cells) for _, cells in roots)
        scheduled_points = sum_known(self.grid.count(cells) for _, cells in scheduled)
        states = [self.walk(name, cells) for name, cells in scheduled]
        result = {"status": "recorded_geometry_only", "requested_root_tiles": len(roots),
                  "scheduled_root_tiles": len(scheduled), "max_tiles": max_tiles,
                  "requested_receiver_points": requested_points, "scheduled_receiver_points": scheduled_points,
                  "receiver_count_limitation": self.grid.point_reason, "clock_split_nodes": self.clock_splits,
                  "unresolved_piece_reasons": dict(sorted(self.reasons.items()))}
        for scope, label in (("receipt", "receipt_recorded"), ("ledger", "canonical_stage_agreed")):
            points = sum_known(state[scope + "_points"] for state in states)
            result[label] = {"fully_receipted_roots": sum(state[scope + "_full"] for state in states),
                             "partial_roots": sum(state[scope + "_any"] and not state[scope + "_full"] for state in states),
                             "roots_without_accepted_receipts": sum(not state[scope + "_any"] for state in states),
                             "unique_receiver_points": points,
                             "outstanding_scheduled_receiver_points": scheduled_points - points
                                 if scheduled_points is not None and points is not None else None,
                             "percent_of_scheduled_receiver_points": round(100 * points / scheduled_points, 6)
                                 if points is not None and scheduled_points else None,
                             "percent_of_requested_receiver_points": round(100 * points / requested_points, 6)
                                 if points is not None and requested_points else None}
        if self.stages is None:
            result["canonical_stage_agreed"] = None
        return result


def funnel(receipts, selected):
    result = []
    for name, unit in METRICS.items():
        values = [receipts[tile]["value"]["metrics"][name] for tile in selected]
        known = [value for value in values if value is not None]
        result.append({"metric": name, "unit": unit, "known_sum": sum(known) if known else None,
                       "receipts_with_counter": len(known), "receipts_missing_or_invalid": len(values) - len(known)})
    return {"receipt_count": len(selected), "metrics": result}



def trial_classification(receipts, selected):
    """Rates only over explicitly accounted clock-trial visits in the same receipts.

    A missing counter or unreported trial envelope prevents a defensible
    fraction for the selected receipt population. Envelope counts are candidate
    units, never added to the clock-trial denominator.
    """
    names = ("evaluated_trials", "rejected_trials", "unknown_trials", "unreported_trial_envelopes")
    totals = dict.fromkeys(names, 0)
    covered = 0
    for tile_id in selected:
        counts = receipts[tile_id]["value"]["metrics"]
        if not all(nonnegative_int(counts.get(name)) for name in names):
            continue
        covered += 1
        for name in names:
            totals[name] += counts[name]
    visits = totals["evaluated_trials"] + totals["rejected_trials"] + totals["unknown_trials"]
    complete = covered == len(selected) and totals["unreported_trial_envelopes"] == 0
    status = ("no_telemetry" if not covered else
              "incomplete" if not complete else
              "no_reported_trials" if not visits else "complete")
    return {
        "status": status,
        "basis": "disjoint evaluated, rejected, and unknown clock-trial visits within active coarse receipts",
        "selected_receipts": len(selected),
        "receipts_with_all_trial_counters": covered,
        "receipts_with_missing_trial_counters": len(selected) - covered,
        "known_counters": totals if covered else None,
        "reported_trial_visits": visits if covered else None,
        "percent_rejected": round(100 * totals["rejected_trials"] / visits, 6)
            if complete and visits else None,
        "percent_unknown": round(100 * totals["unknown_trials"] / visits, 6)
            if complete and visits else None,
        "percent_evaluated": round(100 * totals["evaluated_trials"] / visits, 6)
            if complete and visits else None,
    }


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            handle.write(text)
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def markdown(report):
    def display(value):
        return "unknown" if value is None else str(value)
    coverage = report["coverage"]
    lines = ["# Saved checkpoint progress", "", f"Run: `{report['source']['run_hash']}`", "",
             "Recorded coarse work does not certify scientific completeness or a location.", "",
             f"Scheduled roots: {display(coverage.get('scheduled_root_tiles'))}; "
             f"requested roots: {display(coverage.get('requested_root_tiles'))}.", "",
             "| Basis | Fully receipted roots | Partial roots | Unique receiver points | Scheduled percent |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for key in ("receipt_recorded", "canonical_stage_agreed"):
        row = coverage.get(key) or {}
        lines.append("| " + key + " | " + " | ".join(display(row.get(field)) for field in (
            "fully_receipted_roots", "partial_roots", "unique_receiver_points", "percent_of_scheduled_receiver_points")) + " |")
    lines.extend(["", "## Recorded counters", "", f"Scope: {report['funnel_scope']}.", "",
                  "Sums are visits within included receipts. Missing or invalid fields remain unknown.", "",
                  "| Counter | Unit | Known sum | Present receipts | Missing or invalid receipts |",
                  "| --- | --- | ---: | ---: | ---: |"])
    for row in report["receipt_funnel"]["metrics"]:
        lines.append("| " + " | ".join(display(row[key]) for key in (
            "metric", "unit", "known_sum", "receipts_with_counter", "receipts_missing_or_invalid")) + " |")
    trials = report["trial_classification"]
    lines.extend(["", "## Reported clock-trial classifications", "",
                  f"Telemetry status: {trials['status']}; complete-counter receipts: "
                  f"{trials['receipts_with_all_trial_counters']}/{trials['selected_receipts']}.",
                  "Percentages use only disjoint trial visits and are unavailable if any "
                  "selected receipt lacks trial counters or reports an unaccounted envelope.",
                  f"Rejected: {display(trials['percent_rejected'])}%; "
                  f"evaluated: {display(trials['percent_evaluated'])}%; "
                  f"unknown: {display(trials['percent_unknown'])}%."])
    lines.extend(["", "## Limits", ""] + ["- " + item for item in report["limitations"]])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="saved job/search directory or existing real-empty-tile workload JSON")
    parser.add_argument("--json", dest="json_path", help="JSON output outside the input job (default: stdout)")
    parser.add_argument("--markdown", dest="markdown_path", help="Markdown output outside the input job")
    parser.add_argument("--cache", help="incremental summary cache outside the input job")
    parser.add_argument("--max-read-mib", type=float, default=128, help="new JSON bytes per refresh (default: 128 MiB)")
    parser.add_argument("--max-file-mib", type=float, default=64, help="individual JSON size limit (default: 64 MiB)")
    parser.add_argument("--max-grid-points", type=int, default=20000000, help="projected receiver reconstruction limit")
    parser.add_argument("--skip-grid", action="store_true", help="only report recorded counters and inventory")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    observed_at = datetime.now(timezone.utc).isoformat()
    if any(not math.isfinite(v) or v <= 0 for v in (args.max_read_mib, args.max_file_mib)) or args.max_grid_points < 1:
        parser.error("read, file, and grid budgets must be positive and finite")
    source = args.source.expanduser().resolve(strict=True)
    directory = source / "search" if source.is_dir() and (source / "search" / "manifest.json").exists() else source
    protected = (directory.parent if directory.name == "search" else directory) if directory.is_dir() else None
    targets = [Path(value).expanduser().resolve() if value else None
               for value in (args.json_path, args.markdown_path, args.cache)]
    if any(path == source or protected is not None and path.is_relative_to(protected) for path in targets if path):
        raise ValueError("report and cache paths must be outside the input job")
    if len({p for p in targets if p}) != sum(p is not None for p in targets):
        raise ValueError("JSON, Markdown, and cache paths must be distinct")
    json_path, markdown_path, cache_path = targets
    reader_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    cache = {"schema_version": 1, "reader_sha256": reader_hash, "source": str(source), "entries": {}, "grid_counts": {}}
    if cache_path is not None and cache_path.exists():
        if cache_path.stat().st_size > 64 * MIB:
            raise ValueError("summary cache exceeds 64 MiB; choose a fresh cache path")
        old = strict_json(cache_path.read_bytes())
        if not isinstance(old, dict) or old.get("schema_version") != 1 or old.get("source") != str(source):
            raise ValueError("cache belongs to another source or schema; choose a fresh cache path")
        if old.get("reader_sha256") == reader_hash:
            if not isinstance(old.get("entries"), dict) or not isinstance(old.get("grid_counts"), dict):
                raise ValueError("invalid summary cache; choose a fresh cache path")
            cache = old
    reader = Reader(int(args.max_read_mib * MIB), int(args.max_file_mib * MIB), cache["entries"])
    receipts, splits = {}, {}
    if directory.is_dir():
        manifest_path = directory / "manifest.json"
        saved = reader.read(manifest_path, limit=8 * MIB)
        if saved["status"] != "ok":
            raise ValueError(f"cannot read manifest: {saved}")
        config, run_hash = saved["value"]["configuration"], saved["value"]["run_hash"]
        if content_hash(config) != run_hash:
            raise ValueError("manifest configuration hash mismatch")
        before = inventory(directory)
        for name in sorted(before):
            match = SPLIT_NAME.fullmatch(name)
            if match:
                splits[match[1]] = reader.read(directory / name, limit=64 * 1024)
        for name in sorted(before, key=lambda n: (before[n][2] if isinstance(before[n], list) else math.inf, n)):
            match = RECEIPT_NAME.fullmatch(name)
            if match:
                receipts[match[1]] = reader.read(directory / name, receipt_summary)
        ledger = ledger_snapshot(directory / "retention.sqlite", run_hash)
        source_info = {"kind": "search_directory", "path": str(directory), "run_hash": run_hash,
                       "manifest_hash_verified": True,
                       "legacy_upgrade_present": (directory.parent / "checkpoint-upgrade.json").exists()}
    else:
        saved = reader.read(source, limit=8 * MIB)
        if saved["status"] != "ok":
            raise ValueError(f"cannot read recorded workload: {saved}")
        fixture = saved["value"]
        if (fixture.get("schema_version") != 1 or not isinstance(fixture.get("tiles"), list)
                or not isinstance(fixture.get("empty_payload"), dict) or "provenance" not in fixture):
            raise ValueError("expected the existing recorded real-empty-tile workload format")
        config, run_hash = fixture["query"], fixture["run_hash"]
        for tile in fixture["tiles"]:
            tile_id = tile["tile_id"]
            if re.fullmatch(TILE_ID, tile_id) is None or tile_id in receipts:
                raise ValueError("invalid or duplicated recorded tile ID")
            payload = dict(fixture["empty_payload"], cells=tile["cells"])
            receipts[tile_id] = {"status": "ok", "value": receipt_summary({
                "run_hash": run_hash, "payload": payload, "payload_hash": tile["payload_hash"]})}
        before = {}
        ledger = {"status": "unavailable_in_portable_workload", "stages": {}, "record_and_bound_content_rehashed": False}
        source_info = {"kind": "recorded_workload", "path": str(source), "run_hash": run_hash,
                       "manifest_hash_verified": False, "provenance": fixture["provenance"]}
    max_tiles = config["budget"].get("max_tiles")
    if max_tiles is not None and (type(max_tiles) is not int or max_tiles <= 0):
        raise ValueError("invalid saved max_tiles")
    grid = Grid(config, cache["grid_counts"], args.max_grid_points, args.skip_grid)
    saved_packages = config.get("runtime", {}).get("packages", {})
    runtime_mismatches = {name: {"saved": saved_packages[name], "current": grid.runtime[name]}
                          for name in ("numpy", "pyproj") if name in saved_packages and name in grid.runtime
                          and saved_packages[name] != grid.runtime[name]}
    coverage = Coverage(grid, receipts, splits, run_hash, ledger)
    progress = coverage.report(max_tiles)
    producers = Counter(value["value"]["run_hash"] for value in receipts.values() if value["status"] == "ok")
    if directory.is_dir():
        changed = before != inventory(directory) or fingerprint(manifest_path) != cache["entries"][str(manifest_path)]["fingerprint"]
    else:
        changed = fingerprint(source) != cache["entries"][str(source)]["fingerprint"]
    stages = ledger.pop("stages")
    ledger.update(coarse_stage_rows=len(stages), stage_rows_without_reconciled_receipts=len(set(stages) - coverage.agreed))
    limitations = [
        "Payload hashes are checked or reused from an unchanged stat identity. Terrain/source files and retention records/bounds are not rehashed.",
        "Canonical agreement means matching committed stage metadata, not independently verified scientific contents or a resumability guarantee.",
        "SQLite uses one mode=ro WAL-aware transaction. Receipts and split markers are separate files; the combined observation is not atomic.",
        "Pre-open checks cannot atomically prevent sidecar races during producer shutdown. SQLite shared-memory reader bookkeeping is allowed; application rows and checkpoints are not written.",
        "Fully receipted regions record work. Unsupported trajectories, unresolved trials, or missing data can still prevent a scientific conclusion.",
        "Only the exact manifest producer identity is counted. Other producer identities need separate compatibility validation.",
        "Small receipts are read first. Read limits and absent counters make known sums a biased partial inventory, not a national extrapolation.",
        "Clock siblings share cells. All required clock descendants must be present before the receiver counts once; clock/scoring semantics are not replayed.",
        "Fine receipts are inventoried separately. grid_points counts recorded receiver visits and can be zero after an empty-flight shortcut; it never supplies geographic coverage.",
        "Receiver geometry is reconstructed with the current NumPy/pyproj/PROJ runtime, not a reproduced historical runtime.",
        "Stage CPU/wall percentiles, stage throughput, unique aircraft/samples, nonlinear fits, and globally retained modes remain unknown.",
    ]
    if grid.reason or grid.point_reason:
        limitations.append(grid.reason or grid.point_reason)
    if runtime_mismatches:
        limitations.append("Saved and current grid package versions differ: " + ", ".join(sorted(runtime_mismatches)) + ".")
    if changed:
        limitations.append("Input inventory changed during this refresh. Observations are provisional; refresh after it stabilizes.")
    if source_info["kind"] == "recorded_workload":
        limitations.append("This portable workload has no canonical ledger. It is not the original a904 national search or an accuracy result.")
    report = {"schema_version": 1, "observed_at": observed_at, "source": source_info,
              "grid_runtime": grid.runtime, "saved_runtime": config.get("runtime"), "grid_runtime_mismatches": runtime_mismatches,
              "configuration": {key: config.get(key) for key in ("bounds", "crs", "budget", "receiver_mask")},
              "snapshot": {"receipt_inventory_unchanged": not changed, "atomic_across_sqlite_and_files": False, "canonical_ledger": ledger},
              "read_budget": dict(reader.stats, max_read_bytes=int(args.max_read_mib * MIB), max_file_bytes=int(args.max_file_mib * MIB)),
              "inventory": {"coarse_receipt_files": len(receipts), "split_markers": len(splits),
                            "fine_receipt_files": sum(n.startswith("fine-tile-") and not n.endswith(".error.json") for n in before),
                            "error_files": sum(n.endswith(".error.json") for n in before),
                            "receipt_read_statuses": dict(Counter(v["status"] for v in receipts.values())),
                            "payload_verified_producer_counts": dict(sorted(producers.items())),
                            "superseded_parent_receipts": len(coverage.superseded),
                            "receipt_files_outside_reconciled_tree": len(set(receipts) - coverage.visited) if grid.roots is not None else None},
              "coverage": progress,
              "funnel_scope": "active coarse leaves with matching geometry and producer" if grid.roots is not None
                              else "matching-producer receipts without grid/hierarchy reconciliation",
              "receipt_funnel": funnel(receipts, coverage.active),
              "trial_classification": trial_classification(receipts, coverage.active),
              "canonical_stage_agreed_funnel": funnel(receipts, coverage.agreed)
                  if grid.roots is not None and ledger["status"] == "metadata_snapshot" else None,
              "unknown_telemetry": {key: None for key in ("unique_aircraft", "unique_track_samples", "nonlinear_fits",
                  "global_retained_modes", "stage_cpu_p50_p95_max_s", "stage_wall_p50_p95_max_s", "stage_throughput")},
              "limitations": limitations, "analysis_wall_s": time.perf_counter() - started}
    if cache_path is not None:
        atomic_write(cache_path, json.dumps(cache, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    formatted = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if json_path is not None:
        atomic_write(json_path, formatted)
    elif markdown_path is None:
        sys.stdout.write(formatted)
    if markdown_path is not None:
        atomic_write(markdown_path, markdown(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
        print(f"checkpoint report: {error}", file=sys.stderr)
        raise SystemExit(2)
