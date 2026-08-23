"""Waits leg: the NA rule, type coercion, and parsing the real payloads."""

from __future__ import annotations

import unittest

from tests import WAITS_FULL, WAITS_NOT_REPORTING, fixture

from ed_waits.waits import (
    _apply_na_rule, _as_int, _clean_postcode, parse_wait_payload,
    validate_wait_payload,
)

LOGGER = "ingest_ed_waits"


class AsIntTests(unittest.TestCase):
    """bool is a subclass of int; JSON true must not become a patient count."""

    def test_int_passes_through(self):
        self.assertEqual(_as_int(4), 4)

    def test_bool_is_rejected(self):
        self.assertIsNone(_as_int(True))
        self.assertIsNone(_as_int(False))

    def test_integral_float_is_accepted(self):
        self.assertEqual(_as_int(4.0), 4)

    def test_fractional_float_is_rejected(self):
        self.assertIsNone(_as_int(4.5))

    def test_string_and_none_rejected(self):
        self.assertIsNone(_as_int("4"))
        self.assertIsNone(_as_int(None))


class NaRuleTests(unittest.TestCase):
    """Mirrors the site's getNSWHealthInformation.js display rule."""

    def test_ordinary_count_survives(self):
        self.assertEqual(_apply_na_rule(4, 100, 5), 4)

    def test_negative_count_is_na(self):
        self.assertIsNone(_apply_na_rule(-1, 100, 5))

    def test_count_over_threshold_is_na(self):
        self.assertIsNone(_apply_na_rule(150, 100, 5))

    def test_count_at_threshold_is_kept(self):
        self.assertEqual(_apply_na_rule(100, 100, 5), 100)

    def test_stale_count_is_na(self):
        self.assertIsNone(_apply_na_rule(4, 100, 121))

    def test_count_exactly_at_staleness_limit_is_kept(self):
        self.assertEqual(_apply_na_rule(4, 100, 120), 4)

    def test_absent_threshold_does_not_na_the_count(self):
        self.assertEqual(_apply_na_rule(4, None, 5), 4)

    def test_boolean_staleness_is_ignored_not_compared(self):
        self.assertEqual(_apply_na_rule(4, 100, True), 4)

    def test_unparseable_count_is_na(self):
        self.assertIsNone(_apply_na_rule("four", 100, 5))


class CleanPostcodeTests(unittest.TestCase):

    def test_rted_prefix_is_stripped(self):
        self.assertEqual(_clean_postcode("NSW 2145"), "2145")

    def test_bare_postcode_is_unchanged(self):
        self.assertEqual(_clean_postcode("2145"), "2145")

    def test_null_sentinel_becomes_none(self):
        self.assertIsNone(_clean_postcode("NULL"))
        self.assertIsNone(_clean_postcode(""))


class ValidateWaitPayloadTests(unittest.TestCase):

    def test_real_payload_passes(self):
        validate_wait_payload(fixture(WAITS_FULL))

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            validate_wait_payload([])

    def test_missing_hospital_details_raises(self):
        with self.assertRaises(ValueError):
            validate_wait_payload({"reportingHospitalDetails": []})

    def test_missing_expected_field_raises(self):
        payload = {"hospitalDetails": [{}],
                   "reportingHospitalDetails": [{"hospitalID": 1}]}
        with self.assertRaises(ValueError):
            validate_wait_payload(payload)


class ParseRealPayloadTests(unittest.TestCase):
    """Against the payload NSW Health actually served on 19/07/2026."""

    def setUp(self):
        self.payload = fixture(WAITS_FULL)

    def test_parses_the_anchor_plus_every_reporting_hospital(self):
        facilities, snapshots = parse_wait_payload(self.payload)
        # 58 in reportingHospitalDetails + the queried anchor, which the
        # API returns separately and omits from that array.
        self.assertEqual(len(snapshots), 59)
        self.assertEqual(len(facilities), len(snapshots))

    def test_the_documented_upstream_gap_is_still_the_gap(self):
        """60 advertised, 59 parseable -- The New Maitland Hospital.

        Documented in NSW_Health_JSON_Engine.md. The parser is correct; the
        hospital is counted in totalHospitalsCount but appears nowhere in
        the payload. If this ever stops failing, the upstream gap closed and
        the doc needs updating.
        """
        advertised = self.payload["reportingHospitals"][0]["totalHospitalsCount"]
        _, snapshots = parse_wait_payload(self.payload)
        self.assertEqual(advertised, 60)
        self.assertEqual(len(snapshots), 59)

    def test_the_known_gap_logs_info_not_warning(self):
        """The fixture's 60-vs-59 shortfall IS the known Maitland gap.

        This replaced test_drift_alarm_fires_on_the_count_mismatch when the
        known-gap allowance landed: the same payload that used to WARN every
        cycle (~96 identical lines a day, unread for eleven days) now logs
        INFO. Only a CHANGE in the shortfall warns -- the four tests below.
        """
        with self.assertNoLogs(LOGGER, level="WARNING"):
            with self.assertLogs(LOGGER, level="INFO") as caught:
                parse_wait_payload(self.payload)
        self.assertTrue([m for m in caught.output
                         if "known upstream gap" in m])

    def test_a_widened_gap_warns(self):
        """A second hospital dropping out must be heard, loudly."""
        self.payload["reportingHospitalDetails"].pop()
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            parse_wait_payload(self.payload)
        self.assertTrue([m for m in caught.output if "CHANGED" in m])

    def test_a_closed_gap_is_silent(self):
        """Advertised matches delivered: nothing to say either way.

        The silence is also the documented cancel-out trap: once NSW Health
        fills the hole, KNOWN_MISSING_HOSPITALS must go back to 0 or a fresh
        single omission would read as the old known gap.
        """
        self.payload["reportingHospitals"][0]["totalHospitalsCount"] = 59
        with self.assertNoLogs(LOGGER, level="INFO"):
            parse_wait_payload(self.payload)

    def test_a_negative_shortfall_warns(self):
        """More hospitals delivered than advertised is drift too."""
        self.payload["reportingHospitals"][0]["totalHospitalsCount"] = 58
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            parse_wait_payload(self.payload)
        self.assertTrue([m for m in caught.output if "CHANGED" in m])

    def test_a_bool_count_is_ignored_not_a_shortfall(self):
        """JSON true must not read as advertised=1 and fake a 58-gap.

        bool is a subclass of int; _as_int() rejects it, so the check is
        skipped entirely rather than comparing 59 rows against "1".
        """
        self.payload["reportingHospitals"][0]["totalHospitalsCount"] = True
        with self.assertNoLogs(LOGGER, level="INFO"):
            parse_wait_payload(self.payload)

    def test_no_duplicate_facilities(self):
        _, snapshots = parse_wait_payload(self.payload)
        ids = [s["source_id"] for s in snapshots]
        self.assertEqual(len(ids), len(set(ids)))

    def test_anchor_hospital_carries_bed_capacity(self):
        """Only the queried hospital has bedDetails; the others must be None."""
        _, snapshots = parse_wait_payload(self.payload)
        anchor = snapshots[0]
        self.assertEqual(anchor["source_id"], "209")
        self.assertEqual(anchor["name"], "Westmead Hospital")
        self.assertEqual(anchor["patients_waiting"], 4)
        self.assertEqual(anchor["treatment_spaces"], 29)
        self.assertTrue(all(s["treatment_spaces"] is None
                            for s in snapshots[1:]))

    def test_snapshots_start_unflagged_for_outages(self):
        _, snapshots = parse_wait_payload(self.payload)
        self.assertTrue(all(s["outage"] is False for s in snapshots))
        self.assertTrue(all(s["outage_text"] is None for s in snapshots))


class ParseNonReportingAnchorTests(unittest.TestCase):
    """The other captured payload: the anchor was not reporting."""

    def test_silent_anchor_yields_no_rows(self):
        payload = fixture(WAITS_NOT_REPORTING)
        facilities, snapshots = parse_wait_payload(payload)
        self.assertEqual(snapshots, [])
        self.assertEqual(facilities, [])


class ParseDriftGuardTests(unittest.TestCase):
    """Synthetic payloads for the paths the fixtures do not exercise."""

    def _payload(self, reporting):
        return {"hospitalDetails": [{"hospitalID": 209,
                                     "facilityIdentifier": "F209"}],
                "waitingDetails": [],
                "reportingHospitalDetails": reporting,
                "reportingHospitals": [{"totalHospitalsCount": len(reporting)}]}

    def test_duplicate_hospital_id_is_skipped_once(self):
        payload = self._payload([
            {"hospitalID": 1, "hospitalName": "A", "waitCount": 3},
            {"hospitalID": 1, "hospitalName": "A again", "waitCount": 9},
        ])
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            _, snapshots = parse_wait_payload(payload)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["patients_waiting"], 3)  # first wins
        self.assertTrue([m for m in caught.output if "duplicate hospitalID" in m])

    def test_entry_without_hospital_id_is_skipped(self):
        payload = self._payload([
            {"hospitalName": "No ID", "waitCount": 3},
            {"hospitalID": 2, "hospitalName": "B", "waitCount": 5},
        ])
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            _, snapshots = parse_wait_payload(payload)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["source_id"], "2")
        self.assertTrue([m for m in caught.output
                         if "no hospitalID" in m])

    def test_anchor_with_unmatched_facility_identifier_is_skipped(self):
        """Never attribute another facility's queue to the queried hospital."""
        payload = {"hospitalDetails": [{"hospitalID": 209,
                                        "facilityIdentifier": "F209"}],
                   "waitingDetails": [{"facilityIdentifier": "SOMETHING_ELSE",
                                       "waitCount": 99}],
                   "reportingHospitalDetails": [],
                   "reportingHospitals": [{"totalHospitalsCount": 1}]}
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            _, snapshots = parse_wait_payload(payload)
        self.assertEqual(snapshots, [])
        self.assertTrue([m for m in caught.output
                         if "no waitingDetails entry matches" in m])


if __name__ == "__main__":
    unittest.main()
