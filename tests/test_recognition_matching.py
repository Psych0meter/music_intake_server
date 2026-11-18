
"""Regression tests for recognition candidate matching and tie-breaking.

External recognition/network libraries are stubbed so these tests are
fast, deterministic, and do not require API keys, fpcalc, or Whisper.
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Stub AcoustID so importing the recognition pipeline does not require
# the external package or access to the network.
acoustid = types.ModuleType("acoustid")

for _name in (
    "NoBackendError",
    "FingerprintGenerationError",
    "WebServiceError",
):
    setattr(acoustid, _name, type(_name, (Exception,), {}))

acoustid.match = lambda *args, **kwargs: []
sys.modules.setdefault("acoustid", acoustid)


# Stub Mutagen. These tests mock the functions that inspect and write
# audio metadata, so the real package is not needed here.
mutagen = types.ModuleType("mutagen")
mutagen.File = lambda *args, **kwargs: None
sys.modules.setdefault("mutagen", mutagen)


# Stub requests without allowing accidental network access.
#
# recognize.py creates a Session and mounts an HTTP adapter during import,
# so the mock must implement both interfaces.
requests = types.ModuleType("requests")


class _Session:
    def mount(self, *args, **kwargs):
        """Accept adapter configuration without creating a real connection."""
        pass

    def get(self, *args, **kwargs):
        raise AssertionError(
            "Network access is not expected in recognition unit tests"
        )


class _HTTPAdapter:
    def __init__(self, *args, **kwargs):
        pass


requests.Session = _Session
requests.adapters = types.SimpleNamespace(HTTPAdapter=_HTTPAdapter)
sys.modules.setdefault("requests", requests)


# Import recognize.py with a temporary settings stub.
#
# recognize.py imports get_settings directly, so it retains the stub
# function reference after import. Restoring sys.modules immediately
# afterwards allows test_settings_validation.py to import the real
# app/settings.py during the same unittest discovery run.
_settings_before = sys.modules.get("settings")

settings_stub = types.ModuleType("settings")
settings_stub.get_settings = lambda *args, **kwargs: {}

sys.modules["settings"] = settings_stub

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"

if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

try:
    recognize = importlib.import_module("recognize")
finally:
    if _settings_before is None:
        sys.modules.pop("settings", None)
    else:
        sys.modules["settings"] = _settings_before


class SimilarityTests(unittest.TestCase):
    def test_similarity_is_case_and_whitespace_insensitive(self):
        self.assertEqual(
            recognize.similarity("  Daft Punk ", "daft punk"),
            1.0,
        )

    def test_similarity_returns_none_for_missing_values(self):
        self.assertIsNone(recognize.similarity(None, "Track"))
        self.assertIsNone(recognize.similarity("Artist", ""))


class CandidateAgreementTests(unittest.TestCase):
    def test_matching_pair_is_found_despite_different_source_names(self):
        candidates = [
            ("songrec", "Daft Punk", "One More Time"),
            ("acoustid", "Daft Punk", "One More Time"),
            ("genius", "Other Artist", "Other Song"),
        ]

        match = recognize.best_pair_match(candidates)

        self.assertIsNotNone(match)
        self.assertEqual(match[0], 1.0)
        self.assertEqual(
            {match[1][0], match[2][0]},
            {"songrec", "acoustid"},
        )

    def test_conflicting_candidates_do_not_form_consensus(self):
        candidates = [
            ("songrec", "Artist A", "Song A"),
            ("acoustid", "Artist B", "Song B"),
        ]

        self.assertIsNone(recognize.best_pair_match(candidates))

    def test_partial_metadata_can_agree_on_available_field(self):
        candidates = [
            ("songrec", "Daft Punk", None),
            ("acoustid", "Daft Punk", "One More Time"),
        ]

        match = recognize.best_pair_match(candidates)

        self.assertIsNotNone(match)
        self.assertEqual(match[0], 1.0)

    def test_no_comparable_fields_is_not_a_match(self):
        candidates = [
            ("songrec", None, None),
            ("acoustid", None, None),
        ]

        self.assertIsNone(recognize.best_pair_match(candidates))


class PipelineSelectionTests(unittest.TestCase):
    def _run(
        self,
        sr,
        ac,
        genius=None,
        sr_album=None,
        sr_valid=False,
        ac_valid=False,
    ):
        with (
            patch.object(
                recognize,
                "probe_duration",
                return_value=180,
            ),
            patch.object(
                recognize,
                "songrec_identify",
                return_value=(*sr, sr_album),
            ),
            patch.object(
                recognize,
                "acoustid_lookup",
                return_value=(*ac, 0.9),
            ),
            patch.object(
                recognize,
                "lyrics_identify",
                return_value=genius or (None, None),
            ),
            patch.object(
                recognize,
                "itunes_verify",
                side_effect=[sr_valid, ac_valid],
            ),
            patch.object(recognize, "write_tags"),
            patch.object(
                recognize,
                "compute_filehash",
                return_value="hash",
            ),
            patch.object(recognize.Path, "stat") as stat,
        ):
            stat.return_value.st_size = 100
            stat.return_value.st_mtime = 123

            return recognize.identify_and_tag(
                recognize.Path("sample.mp3"),
                {
                    "songrec_enabled": True,
                    "acoustid_enabled": True,
                    "genius_enabled": bool(genius),
                },
            )

    def test_consensus_wins_without_itunes_tiebreak(self):
        result = self._run(
            ("Artist", "Track"),
            ("Artist", "Track"),
        )

        self.assertEqual(
            (result["artist"], result["title"]),
            ("Artist", "Track"),
        )

    def test_itunes_can_select_acoustid_when_sources_disagree(self):
        result = self._run(
            ("Artist A", "Track A"),
            ("Artist B", "Track B"),
            sr_valid=False,
            ac_valid=True,
        )

        self.assertEqual(
            (result["artist"], result["title"]),
            ("Artist B", "Track B"),
        )

    def test_songrec_fallback_is_used_when_neither_disagreement_verifies(self):
        result = self._run(
            ("Artist A", "Track A"),
            ("Artist B", "Track B"),
        )

        self.assertEqual(
            (result["artist"], result["title"]),
            ("Artist A", "Track A"),
        )

    def test_genius_runs_only_when_both_primary_sources_miss(self):
        with (
            patch.object(
                recognize,
                "probe_duration",
                return_value=180,
            ),
            patch.object(
                recognize,
                "songrec_identify",
                return_value=(None, None, None),
            ),
            patch.object(
                recognize,
                "acoustid_lookup",
                return_value=(None, None, 0.0),
            ),
            patch.object(
                recognize,
                "lyrics_identify",
                return_value=("Genius Artist", "Genius Track"),
            ) as lyrics,
            patch.object(recognize, "write_tags"),
            patch.object(
                recognize,
                "compute_filehash",
                return_value="hash",
            ),
            patch.object(recognize.Path, "stat") as stat,
        ):
            stat.return_value.st_size = 100
            stat.return_value.st_mtime = 123

            result = recognize.identify_and_tag(
                recognize.Path("sample.mp3"),
                {
                    "songrec_enabled": True,
                    "acoustid_enabled": True,
                    "genius_enabled": True,
                },
            )

        lyrics.assert_called_once()
        self.assertEqual(
            (result["artist"], result["title"]),
            ("Genius Artist", "Genius Track"),
        )

    def test_genius_is_not_called_when_primary_detector_has_a_match(self):
        with (
            patch.object(
                recognize,
                "probe_duration",
                return_value=180,
            ),
            patch.object(
                recognize,
                "songrec_identify",
                return_value=("Artist", "Track", None),
            ),
            patch.object(
                recognize,
                "acoustid_lookup",
                return_value=(None, None, 0.0),
            ),
            patch.object(recognize, "lyrics_identify") as lyrics,
            patch.object(
                recognize,
                "itunes_verify",
                return_value=True,
            ),
            patch.object(recognize, "write_tags"),
            patch.object(
                recognize,
                "compute_filehash",
                return_value="hash",
            ),
            patch.object(recognize.Path, "stat") as stat,
        ):
            stat.return_value.st_size = 100
            stat.return_value.st_mtime = 123

            recognize.identify_and_tag(
                recognize.Path("sample.mp3"),
                {
                    "songrec_enabled": True,
                    "acoustid_enabled": True,
                    "genius_enabled": True,
                },
            )

        lyrics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
