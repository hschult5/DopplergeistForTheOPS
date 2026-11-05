"""Exercise CLI routing and coverage failures with unmodified recorded PCM.

The two four-second excerpts are separate recordings for this software check.
No samples are generated, padded or joined, and proposal recall is not assessed.
"""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from scipy.io import wavfile


ROOT = Path(__file__).resolve().parents[1]


class RealEpisodeCli(unittest.TestCase):
    def test_explicit_routing_and_source_coverage(self):
        fixture = json.loads((ROOT / "tests/fixtures/real_discovery_normalization.json").read_text())
        self.assertEqual(fixture["sample_format"], "<f4")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, excerpt in enumerate(fixture["excerpts"]):
                raw = base64.b64decode(excerpt["pcm_f32le_base64"], validate=True)
                self.assertEqual(len(raw), excerpt["sample_count"] * 4)
                audio = root / f"recorded-excerpt-{index}.wav"
                wavfile.write(audio, fixture["sample_rate_hz"], np.frombuffer(raw, dtype="<f4"))
                output = root / f"scan-{index}"
                command = [sys.executable, "-m", "dopplergeist.cli", "discover", str(audio),
                           "--output", str(output)]
                process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(process.returncode, 0, process.stderr)
                summary = json.loads(process.stdout)
                artifact = json.loads((output / "discovery/discovery.json").read_text())
                intake = json.loads((output / "intake/intake.json").read_text())
                self.assertEqual(intake["sample_count"], excerpt["sample_count"])
                self.assertEqual(summary["events_found"], len(artifact["events"]))
                self.assertEqual(summary["groups_found"], len(artifact["proposals"]))
                self.assertEqual((output / "intake/recording.f32").read_bytes(), raw)
                self.assertEqual(json.loads((output / "discovery/intake_high/recording.json").read_text())["sample_rate_hz"], 24000)

            refused = root / "removed-experimental-option"
            process = subprocess.run(
                [sys.executable, "-m", "dopplergeist.cli", "discover", str(audio),
                 "--output", str(refused), "--experimental-episodes"],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, 2)
            self.assertFalse(refused.exists())


if __name__ == "__main__":
    unittest.main()
