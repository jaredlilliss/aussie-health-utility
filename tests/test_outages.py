"""Outages leg: LHD label parsing, normalization and the NA-marking rule."""

from __future__ import annotations

import unittest

from tests import fixture  # noqa: F401  (bootstraps sys.path for src/)

from ed_waits.common import FacilityRow
from ed_waits.outages import (
    Outage, _norm_lhd, _parse_lhd_labels, apply_outages,
    validate_outages_payload,
)

LOGGER = "ingest_ed_waits"


def _facility(source_id="1", lhd="Western Sydney Local Health District"):
    return FacilityRow(source_id=source_id, name="Test Hospital",
                       facility_type="ed_reporting", address=None,
                       suburb=None, postcode=None, lhd=lhd, phone=None)


def _snapshot(source_id="1", waiting=7):
    return {"source_id": source_id, "patients_waiting": waiting,
            "outage": False, "outage_text": None}


class ParseLhdLabelsTests(unittest.TestCase):
    """[] means statewide, None means drift. The distinction is load-bearing."""

    def test_absent_field_is_drift_not_statewide(self):
        """Regression: an absent field used to mean statewide.

        Statewide forces patients_waiting to NULL for every hospital in NSW.
        Verbose OData omits or defers unset multi-value fields, so "absent"
        is at least as likely to mean the shape changed. Reading it as
        statewide blanked the state silently; treating it as drift loses one
        item's outage marking loudly.
        """
        self.assertIsNone(_parse_lhd_labels({}))

    def test_null_field_is_drift(self):
        self.assertIsNone(_parse_lhd_labels({"nswLocalHealthDistrict": None}))

    def test_explicitly_empty_results_is_statewide(self):
        self.assertEqual(
            _parse_lhd_labels({"nswLocalHealthDistrict": {"results": []}}), [])

    def test_labels_are_extracted(self):
        item = {"nswLocalHealthDistrict": {"results": [
            {"Label": "Western Sydney"}, {"Label": "South Western Sydney"}]}}
        self.assertEqual(_parse_lhd_labels(item),
                         ["Western Sydney", "South Western Sydney"])

    def test_deferred_navigation_property_is_drift(self):
        item = {"nswLocalHealthDistrict": {"__deferred": {"uri": "..."}}}
        self.assertIsNone(_parse_lhd_labels(item))

    def test_renamed_entry_key_is_drift(self):
        item = {"nswLocalHealthDistrict": {"results": [{"Name": "Western Sydney"}]}}
        self.assertIsNone(_parse_lhd_labels(item))

    def test_non_list_results_is_drift(self):
        item = {"nswLocalHealthDistrict": {"results": "Western Sydney"}}
        self.assertIsNone(_parse_lhd_labels(item))

    def test_blank_labels_are_dropped_not_fatal(self):
        item = {"nswLocalHealthDistrict": {"results": [
            {"Label": "  "}, {"Label": "Hunter New England"}]}}
        self.assertEqual(_parse_lhd_labels(item), ["Hunter New England"])


class NormLhdTests(unittest.TestCase):
    """RTED and SharePoint spell the same district differently."""

    def test_suffix_is_dropped(self):
        self.assertEqual(_norm_lhd("Western Sydney Local Health District"),
                         "western sydney")

    def test_bare_name_matches_suffixed_name(self):
        self.assertEqual(_norm_lhd("Western Sydney"),
                         _norm_lhd("Western Sydney Local Health District"))

    def test_case_and_whitespace_are_normalized(self):
        self.assertEqual(_norm_lhd("  WESTERN   SYDNEY  "), "western sydney")

    def test_empty_and_none(self):
        self.assertIsNone(_norm_lhd(None))
        self.assertIsNone(_norm_lhd(""))

    def test_suffix_strip_needs_a_preceding_space(self):
        """A name that is only the suffix must not normalize to nothing.

        The stripped suffix carries a leading space, so a district called
        literally "Local Health District" survives intact rather than
        collapsing to None and silently matching every other empty LHD.
        """
        self.assertEqual(_norm_lhd("Local Health District"),
                         "local health district")


class ApplyOutagesTests(unittest.TestCase):

    def test_no_outages_leaves_snapshots_untouched(self):
        snaps = [_snapshot()]
        apply_outages([_facility()], snaps, [])
        self.assertEqual(snaps[0]["patients_waiting"], 7)
        self.assertFalse(snaps[0]["outage"])

    def test_statewide_outage_marks_every_snapshot(self):
        facs = [_facility("1", "Western Sydney Local Health District"),
                _facility("2", "Hunter New England Local Health District")]
        snaps = [_snapshot("1"), _snapshot("2")]
        with self.assertLogs(LOGGER, level="WARNING"):
            apply_outages(facs, snaps, [Outage(lhds=[], text="statewide",
                                               starts_at="", ends_at="")])
        for snap in snaps:
            self.assertTrue(snap["outage"])
            self.assertIsNone(snap["patients_waiting"])
            self.assertEqual(snap["outage_text"], "statewide")

    def test_targeted_outage_marks_only_its_district(self):
        facs = [_facility("1", "Western Sydney Local Health District"),
                _facility("2", "Hunter New England Local Health District")]
        snaps = [_snapshot("1"), _snapshot("2")]
        with self.assertLogs(LOGGER, level="WARNING"):
            apply_outages(facs, snaps, [Outage(lhds=["Western Sydney"],
                                               text="wslhd", starts_at="",
                                               ends_at="")])
        self.assertIsNone(snaps[0]["patients_waiting"])
        self.assertEqual(snaps[1]["patients_waiting"], 7)
        self.assertFalse(snaps[1]["outage"])

    def test_overlapping_outages_do_not_raise_a_false_drift_alarm(self):
        """Regression: hits[] was counted inside a loop that breaks.

        Two outages covering the same district meant the second recorded
        zero hits and tripped the "matched NO facilities" alarm on a
        perfectly good payload. A drift alarm that cries wolf is how the
        totalHospitalsCount gap went unread for eleven days.
        """
        facs = [_facility("1", "Western Sydney Local Health District")]
        snaps = [_snapshot("1")]
        outages = [Outage(lhds=["Western Sydney"], text="first",
                          starts_at="", ends_at=""),
                   Outage(lhds=["Western Sydney"], text="second",
                          starts_at="", ends_at="")]
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            apply_outages(facs, snaps, outages)
        self.assertFalse([m for m in caught.output if "matched NO facilities" in m],
                         "second overlapping outage raised a spurious alarm")
        self.assertEqual(snaps[0]["outage_text"], "first")  # first still wins
        self.assertIsNone(snaps[0]["patients_waiting"])

    def test_genuinely_unmatched_outage_still_alarms(self):
        """The fix must not have silenced the alarm it made precise."""
        facs = [_facility("1", "Western Sydney Local Health District")]
        snaps = [_snapshot("1")]
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            apply_outages(facs, snaps, [Outage(lhds=["Nowhere District"],
                                               text="x", starts_at="",
                                               ends_at="")])
        self.assertTrue([m for m in caught.output if "matched NO facilities" in m])
        self.assertEqual(snaps[0]["patients_waiting"], 7)  # count untouched

    def test_label_spelling_difference_still_matches(self):
        """SharePoint 'Western Sydney' vs RTED '... Local Health District'."""
        facs = [_facility("1", "Western Sydney Local Health District")]
        snaps = [_snapshot("1")]
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            apply_outages(facs, snaps, [Outage(lhds=["Western Sydney"],
                                               text="x", starts_at="",
                                               ends_at="")])
        self.assertFalse([m for m in caught.output if "matched NO facilities" in m])
        self.assertIsNone(snaps[0]["patients_waiting"])

    def test_facility_with_no_lhd_is_not_hit_by_a_targeted_outage(self):
        facs = [_facility("1", None)]
        snaps = [_snapshot("1")]
        with self.assertLogs(LOGGER, level="WARNING"):
            apply_outages(facs, snaps, [Outage(lhds=["Western Sydney"],
                                               text="x", starts_at="",
                                               ends_at="")])
        self.assertEqual(snaps[0]["patients_waiting"], 7)

    def test_facility_with_no_lhd_is_still_hit_by_a_statewide_outage(self):
        facs = [_facility("1", None)]
        snaps = [_snapshot("1")]
        with self.assertLogs(LOGGER, level="WARNING"):
            apply_outages(facs, snaps, [Outage(lhds=[], text="x",
                                               starts_at="", ends_at="")])
        self.assertIsNone(snaps[0]["patients_waiting"])


class ValidateOutagesPayloadTests(unittest.TestCase):

    def test_valid_envelope_passes(self):
        validate_outages_payload({"d": {"results": []}})

    def test_missing_envelope_raises(self):
        with self.assertRaises(ValueError):
            validate_outages_payload({"value": []})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            validate_outages_payload([])

    def test_results_not_a_list_raises(self):
        with self.assertRaises(ValueError):
            validate_outages_payload({"d": {"results": {}}})


if __name__ == "__main__":
    unittest.main()
