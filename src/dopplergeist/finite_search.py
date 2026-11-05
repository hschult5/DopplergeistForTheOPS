"""Exact finite-seed identity and disk-backed scored-retention accounting."""
import hashlib
import json
import math
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from threading import local

from .data_access import DataUnavailable, content_hash


def mode_seed(row):
    def number(value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Finite mode seeds require finite coordinates and times")
        return 0.0 if value == 0 else value
    return {"event_indices": [int(i) for i in row.get("event_indices", range(len(row["flight_ids"])))],
            "flight_ids": list(row["flight_ids"]),
            "pass_emission_times_s": [number(t) for t in row.get("pass_emission_times_s", [])],
            "position": [number(row.get(k, 0)) for k in ("x", "y", "lat", "lon", "elev_m")],
            "recording_start_time_s": number(row["recording_start_time_s"])}


def identify_mode(row):
    seed = row.get("mode_seed") or mode_seed(row)
    identifier = "mode-" + content_hash(seed)
    if row.get("mode_id", identifier) != identifier:
        raise DataUnavailable("Mode ID does not match its original seed identity")
    row["mode_seed"], row["mode_id"] = seed, identifier
    return identifier


def classified_assessment(row):
    assessment = row.get("scoring_assessment")
    valid = (isinstance(assessment, dict) and assessment.get("complete") is True
            and all(type(assessment.get(k)) is int and assessment[k] >= 0
                    for k in ("scheduled_trials", "evaluated_trials", "rejected_trials", "unknown_trials"))
            and assessment["scheduled_trials"] > 0
            and assessment["scheduled_trials"] == sum(assessment[k] for k in
                    ("evaluated_trials", "rejected_trials", "unknown_trials"))
            and assessment.get("trial_count_semantics") == "disjoint_evaluated_rejected_unknown"
            and assessment.get("empty_clock_domain") is False
            and assessment["unknown_trials"] == 0)
    if not valid:
        return False
    # No trials is unknown work. A finite best needs an evaluated trial; a
    # wholly rejected envelope cannot have discarded an evaluated finite best.
    score = row.get("score", math.nan)
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return False
    return (assessment["evaluated_trials"] > 0 if math.isfinite(score) else
            score == -math.inf and assessment["evaluated_trials"] == 0 and assessment["rejected_trials"] > 0)


def bounded_assessment(row):
    """Validate a score ceiling without calling missing support a core fit."""
    assessment = row.get("scoring_assessment")
    kind = "matched_group_support_minus_normalized_residual"
    indices = row.get("event_indices")
    if (not isinstance(assessment, dict) or assessment.get("complete") is not False
            or assessment.get("unknown_score_bound_kind") != kind
            or row.get("score_kind") != kind
            or assessment.get("unknown_score_bound_method") not in
               ("nonnegative_loss_and_validated_event_fits", "group_count_ceiling",
                "nonnegative_loss_validated_fits_and_acoustic_dual",
                "nonnegative_loss_validated_fits_and_acoustic_floor_dual")
            or not isinstance(indices, list) or not indices
            or any(type(i) is not int or i < 0 for i in indices) or indices != sorted(set(indices))
            or len(row.get("flight_ids", [])) != len(indices)):
        return False
    fields = ("evaluated_trials", "rejected_trials", "unknown_trials",
              "bounded_unknown_trials", "bounded_unknown_rejections")
    if not all(type(assessment.get(k)) is int and assessment[k] >= 0 for k in fields):
        return False
    scheduled = assessment.get("scheduled_trials")
    if (type(scheduled) is not int or scheduled <= 0
            or assessment.get("trial_count_semantics") != "disjoint_evaluated_rejected_unknown"
            or assessment.get("empty_clock_domain") is not False
            or scheduled != sum(assessment[k] for k in fields[:3])):
        return False
    unknown, bounded, rejected = (assessment[k] for k in fields[2:])
    if not 0 < unknown == bounded or rejected > bounded:
        return False
    method = assessment["unknown_score_bound_method"]
    dual = assessment.get("acoustic_bound_rejected_trials", 0)
    floor_dual = assessment.get("acoustic_floor_pruned_trials", 0)
    if type(dual) is not int or type(floor_dual) is not int:
        return False
    if method == "nonnegative_loss_validated_fits_and_acoustic_floor_dual":
        if not (0 < floor_dual <= bounded-rejected and 0 <= dual <= rejected):
            return False
    elif floor_dual != 0:
        return False
    elif method == "nonnegative_loss_validated_fits_and_acoustic_dual":
        if not 0 < dual <= rejected:
            return False
    elif dual != 0:
        return False
    upper = assessment.get("unknown_score_upper_bound")
    if method == "group_count_ceiling":
        # This method skips every fit, so only the trivial per-group ceiling is valid.
        return (row.get("score") is None
                and assessment["evaluated_trials"] == 0 and assessment["rejected_trials"] == 0
                and unknown == scheduled and assessment["bounded_unknown_rejections"] == 0
                and isinstance(upper, (int, float)) and not isinstance(upper, bool)
                and math.isfinite(upper) and upper == len(indices))
    if rejected == bounded:
        return upper is None
    return (isinstance(upper, (int, float)) and not isinstance(upper, bool)
            and math.isfinite(upper) and 0 <= upper <= len(indices))


class RetentionLedger:
    """One immutable-input run; bounded per-stage writes and streaming audits.

    Store every finite scored mode before top-k pruning. Stage rows are replaced
    on deterministic replay, so resumed work cannot inflate counts. Exact IDs
    appearing at several stages are aggregated by their maximum scored value.
    """
    def __init__(self, path, run_hash, score_kind):
        self.path, self.run_hash, self.score_kind = Path(path), run_hash, score_kind
        self._batch_state = local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connect() as db:
                db.executescript("""
                    PRAGMA cache_size=-8192; PRAGMA temp_store=FILE;
                    CREATE TABLE IF NOT EXISTS run(identity TEXT, score_kind TEXT);
                    CREATE TABLE IF NOT EXISTS stages(name TEXT PRIMARY KEY, checksum TEXT,
                        occurrences INTEGER, duplicates INTEGER, kept TEXT);
                    CREATE TABLE IF NOT EXISTS records(stage TEXT, mode_id TEXT, score REAL,
                        groups_count INTEGER, kind TEXT, seed TEXT, PRIMARY KEY(stage,mode_id));
                    CREATE TABLE IF NOT EXISTS bounds(stage TEXT, bound_id TEXT, upper_score REAL,
                        groups_count INTEGER, detail TEXT, PRIMARY KEY(stage,bound_id));
                    DROP INDEX IF EXISTS mode_index;
                    DROP INDEX IF EXISTS bound_index;
                """)
                # Saved stages are checksum-verified before reuse and evidence.
                # Opening a national ledger must not scan its entire database.
                identity = db.execute("SELECT identity,score_kind FROM run").fetchone()
                if identity is None:
                    db.execute("INSERT INTO run VALUES(?,?)", (run_hash, score_kind))
                elif identity != (run_hash, score_kind):
                    raise DataUnavailable("Finite ledger input/score identity mismatch")
        except sqlite3.DatabaseError as error:
            raise DataUnavailable("Cannot read finite retention ledger") from error

    def _open_connection(self):
        # Match the worker-stage import's bounded wait so a short-lived reader
        # cannot fail an otherwise valid search at the default five seconds.
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA cache_size=-8192")
            db.execute("PRAGMA temp_store=FILE")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def batch(self):
        """Commit a caller-bounded group of stages in one transaction.

        Nested ``connect()`` calls on this thread share the connection. Other
        threads and ledger instances see only committed stages. Checkpoints
        written before a rollback therefore fail the ordinary resume check.
        Nested batches are rejected; callers must bound their work per batch.
        """
        if getattr(self._batch_state, "active", None) is not None:
            raise RuntimeError("Nested retention batches are not supported")
        db = self._open_connection()
        active = {"db": db, "failed": False}
        self._batch_state.active = active
        try:
            with db:
                yield self
                if active["failed"]:
                    raise DataUnavailable("Retention batch failed; its stages were not committed")
        finally:
            del self._batch_state.active
            db.close()

    @contextmanager
    def connect(self):
        active = getattr(self._batch_state, "active", None)
        if active is not None:
            try:
                yield active["db"]
            except BaseException:
                active["failed"] = True
                raise
            return
        db = self._open_connection()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _checksum(records, kept, occurrences, duplicates, bounds=()):
        digest = hashlib.sha256()
        for record in records:
            digest.update(json.dumps(record, separators=(",", ":"), allow_nan=False).encode())
            digest.update(b"\n")
        digest.update(json.dumps(sorted(kept), separators=(",", ":")).encode())
        digest.update(json.dumps([occurrences, duplicates]).encode())
        for bound in bounds:
            digest.update(json.dumps(bound, separators=(",", ":"), allow_nan=False).encode())
            digest.update(b"\n")
        return digest.hexdigest()

    def record(self, stage, rows, kept, occurrences, *, bounds=()):
        records = []
        for row in rows:
            identifier = identify_mode(row)
            kind = row.get("score_kind", self.score_kind)
            if kind != self.score_kind:
                raise DataUnavailable("Finite retention mixed incompatible score units")
            records.append((identifier, float(row["score"]), len(row["mode_seed"]["event_indices"]), kind,
                            json.dumps(row["mode_seed"], sort_keys=True, separators=(",", ":"))))
        records.sort()
        bound_records = []
        for bound in bounds:
            detail = bound["detail"]
            if (self.score_kind != "matched_group_support_minus_normalized_residual"
                    or not bounded_assessment(detail)
                    or bound["groups_count"] != len(detail["event_indices"])
                    or bound["upper_score"] != detail["scoring_assessment"]["unknown_score_upper_bound"]
                    or bound["bound_id"] != "bound-" + content_hash(detail)):
                raise DataUnavailable("Invalid finite association-score bound")
            bound_records.append((bound["bound_id"], bound["upper_score"], bound["groups_count"],
                                  json.dumps(detail, sort_keys=True, separators=(",", ":"))))
        bound_records.sort()
        kept_ids = [identify_mode(row) for row in kept]
        checksum = self._checksum(records, kept_ids, occurrences, occurrences-len(records), bound_records)
        with self.connect() as db:
            db.execute("DELETE FROM records WHERE stage=?", (stage,))
            db.execute("DELETE FROM bounds WHERE stage=?", (stage,))
            db.executemany("INSERT INTO records VALUES(?,?,?,?,?,?)", [(stage, *r) for r in records])
            db.executemany("INSERT INTO bounds VALUES(?,?,?,?,?)", [(stage, *r) for r in bound_records])
            db.execute("INSERT OR REPLACE INTO stages VALUES(?,?,?,?,?)",
                       (stage, checksum, occurrences, occurrences-len(records), json.dumps(sorted(kept_ids))))
        return checksum

    def checksum(self, stage):
        with self.connect() as db:
            row = db.execute("SELECT checksum FROM stages WHERE name=?", (stage,)).fetchone()
            return row[0] if row else None

    def stage_valid(self, stage, expected):
        try:
            with self.connect() as db:
                record = db.execute("SELECT checksum,kept,occurrences,duplicates FROM stages WHERE name=?", (stage,)).fetchone()
                if record is None or record[0] != expected:
                    return False
                rows = db.execute("SELECT mode_id,score,groups_count,kind,seed FROM records WHERE stage=? ORDER BY mode_id", (stage,))
                bounds = db.execute("SELECT bound_id,upper_score,groups_count,detail FROM bounds WHERE stage=? ORDER BY bound_id", (stage,))
                return self._checksum(rows, json.loads(record[1]), record[2], record[3], bounds) == expected
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise DataUnavailable("Corrupt finite retention stage") from error

    def evidence(self, retained, *, complete, spacing_m, refine_spacing_m, max_event_groups, expected_stages=None):
        ids = [identify_mode(row) for row in retained]
        accounted = True
        totals = {}
        with self.connect() as db:
            stages = db.execute("SELECT name,checksum,occurrences,duplicates,kept FROM stages ORDER BY name")
            for name, checksum, occurrences, duplicates, kept in stages:
                if not self.stage_valid(name, checksum):
                    accounted = False
                phase = name.split(":", 1)[0]
                total = totals.setdefault(phase, {"stages": 0, "input_occurrences": 0, "exact_duplicates": 0,
                                                  "retained": 0, "discarded": 0})
                retained_count = len(json.loads(kept))
                for key, value in (("stages", 1), ("input_occurrences", occurrences), ("exact_duplicates", duplicates),
                                   ("retained", retained_count), ("discarded", occurrences-duplicates-retained_count)):
                    total[key] += value
            db.execute("CREATE TEMP TABLE retained_ids(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO retained_ids VALUES(?)", [(i,) for i in ids])
            # Stage checksums validate records, not the secondary mode_index.
            # Read the table so index damage cannot hide a competing mode.
            distinct = "SELECT mode_id,MAX(score) AS score FROM records NOT INDEXED GROUP BY mode_id"
            # Group once instead of sorting the national ledger three times.
            # Only retained modes stay in Python; SQLite can spill to disk.
            retained_ids = set(ids)
            discarded, bound, evaluated, original = 0, None, 0, {}
            for mode_id, score in db.execute(distinct):
                evaluated += 1
                if mode_id in retained_ids:
                    original[mode_id] = score
                else:
                    discarded += 1
                    bound = score if bound is None else max(bound, score)
            bounded_count, bounded_rejections, bounded_max = db.execute(
                "SELECT count(*),coalesce(sum(upper_score IS NULL),0),max(upper_score) FROM bounds NOT INDEXED").fetchone()
            discarded += bounded_count - bounded_rejections
            if bounded_max is not None:
                bound = bounded_max if bound is None else max(bound, bounded_max)
        if set(original) != set(ids):
            accounted = False
        if expected_stages is not None:
            actual = {phase: totals.get(phase, {}).get("stages", 0) for phase in expected_stages}
            if actual != expected_stages or set(totals)-set(expected_stages):
                accounted = False
        if not accounted:
            discarded, bound, evaluated, original = None, None, None, {}
            bounded_count, bounded_rejections, bounded_max = None, None, None
        return {"schema_version": 1, "scope": "evaluated_finite_grid", "complete": bool(complete and accounted),
                "score_kind": self.score_kind, "spacing_m": float(spacing_m), "refine_spacing_m": float(refine_spacing_m),
                "max_event_groups": max_event_groups,
                "retained_modes": [{"mode_id": row["mode_id"], "score": original.get(row["mode_id"], row["score"]),
                                    "event_groups": len(row["mode_seed"]["event_indices"])} for row in retained],
                "discarded_count": discarded, "best_discarded_score": bound, "pruning_accounted_for": accounted,
                "evaluated_modes": evaluated,
                "bounded_unknown_envelopes": bounded_count,
                "bounded_unknown_rejections": bounded_rejections,
                "best_unknown_score_upper_bound": bounded_max,
                "stages": totals,
                "limitations": ["Evaluated grids and recorded refinements only; continuous-space uniqueness is not established.",
                                "Unknown association trials remain unknown; their persisted score ceilings join the discarded-alternative bound.",
                                "Unvisited positions and unperformed refinements have no inferred score bound.",
                                "Proposal envelopes, CPA tolerances, grouping, clock bounds and physical model are declared assumptions."]}
