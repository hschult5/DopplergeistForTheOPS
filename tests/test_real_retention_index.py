"""Keep archived real alternatives visible when a secondary index is damaged."""
import json
from pathlib import Path
import sqlite3

from dopplergeist.finite_search import RetentionLedger
from dopplergeist.tiled_search import merge_candidates


def damage_mode_index(path, missing_id, replacement_id):
    """Replace only the tiny index leaf; leave the real records/checksums intact."""
    with sqlite3.connect(path) as db:
        db.execute("CREATE INDEX IF NOT EXISTS mode_index ON records(mode_id)")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name='mode_index'").fetchone()[0]
        entries = db.execute("SELECT rowid,mode_id FROM records NOT INDEXED").fetchall()
    # An index entry contains the indexed value and table rowid. Build a valid
    # leaf with the same rowids/cardinality, but a real alternative's key now
    # names the retained mode. This models index damage, not new observations.
    with sqlite3.connect(":memory:") as source:
        source.execute(f"PRAGMA page_size={page_size}")
        source.execute("CREATE TABLE modes(mode_id TEXT)")
        source.executemany("INSERT INTO modes(rowid,mode_id) VALUES(?,?)",
                           [(rowid, replacement_id if mode_id == missing_id else mode_id)
                            for rowid, mode_id in entries])
        source.execute("CREATE INDEX damaged_modes ON modes(mode_id)")
        source_root = source.execute(
            "SELECT rootpage FROM sqlite_master WHERE name='damaged_modes'").fetchone()[0]
        raw = source.serialize()
    leaf = raw[(source_root - 1) * page_size:source_root * page_size]
    original = bytearray(path.read_bytes())
    start = (root - 1) * page_size
    # Six archived modes fit in one leaf: do not generalize this helper to
    # ledgers with internal index pages, overflow pages or unrelated content.
    assert leaf[0] == original[start] == 0x0A
    original[start:start + page_size] = leaf
    path.write_bytes(original)


def test_secondary_index_damage_cannot_hide_real_discarded_mode(tmp_path):
    fixture = json.loads((Path(__file__).parent / "data/reference-retention-real.json").read_text())
    rows = fixture["rows"]
    ledger = RetentionLedger(tmp_path / "retention.sqlite", "recorded-reference-modes", rows[0]["score_kind"])
    retained = merge_candidates(rows, 1, ledger=ledger, stage="coarse:recorded")
    checksum = ledger.checksum("coarse:recorded")
    options = dict(complete=True, spacing_m=1000, refine_spacing_m=100,
                   max_event_groups=3, expected_stages={"coarse": 1})
    expected = ledger.evidence(retained, **options)
    ranked = sorted(rows, key=lambda row: row["score"], reverse=True)
    assert expected["complete"] and expected["pruning_accounted_for"]
    assert expected["evaluated_modes"] == len(rows) == 6
    assert expected["discarded_count"] == 5
    assert expected["best_discarded_score"] == ranked[1]["score"]
    with sqlite3.connect(ledger.path) as db:
        records = db.execute("SELECT * FROM records NOT INDEXED ORDER BY stage,mode_id").fetchall()

    damage_mode_index(ledger.path, ranked[1]["mode_id"], retained[0]["mode_id"])

    with sqlite3.connect(ledger.path) as db:
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert db.execute("SELECT * FROM records NOT INDEXED ORDER BY stage,mode_id").fetchall() == records
        unchecked = db.execute(
            "SELECT mode_id,MAX(score) FROM records INDEXED BY mode_index GROUP BY mode_id").fetchall()
        assert len(unchecked) == 5
        assert max(score for mode_id, score in unchecked if mode_id != retained[0]["mode_id"]) < ranked[1]["score"]
    assert ledger.stage_valid("coarse:recorded", checksum)
    assert ledger.evidence(retained, **options) == expected
