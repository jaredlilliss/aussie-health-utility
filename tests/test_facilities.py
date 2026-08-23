"""Facilities leg: CKAN directory feed -> FacilityRow, and its drift alarms."""

from __future__ import annotations

import unittest

from tests import CKAN_SAMPLE, fixture

from ed_waits.facilities import transform, validate_payload

LOGGER = "ingest_ed_waits"


def _envelope(records, total=None):
    result = {"records": records}
    if total is not None:
        result["total"] = total
    return {"success": True, "result": result}


class ValidatePayloadTests(unittest.TestCase):

    def test_real_sample_passes(self):
        validate_payload(fixture(CKAN_SAMPLE))

    def test_success_false_raises(self):
        with self.assertRaises(ValueError):
            validate_payload({"success": False, "result": {"records": [{}]}})

    def test_missing_records_raises(self):
        with self.assertRaises(ValueError):
            validate_payload({"success": True, "result": {}})

    def test_empty_records_raises(self):
        with self.assertRaises(ValueError):
            validate_payload(_envelope([]))

    def test_missing_mapped_field_raises(self):
        with self.assertRaises(ValueError):
            validate_payload(_envelope([{"Name": "X"}]))  # no ED key


class TransformTests(unittest.TestCase):

    def test_real_sample_maps_every_record(self):
        rows = transform(fixture(CKAN_SAMPLE))
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            sorted({r.facility_type for r in rows}),
            ["ed_not_reporting", "ed_reporting", "no_ed"])

    def test_name_is_the_key_because_ckan_has_no_id(self):
        rows = transform(fixture(CKAN_SAMPLE))
        self.assertTrue(all(r.source_id == r.name for r in rows))

    def test_partial_page_warns_so_paging_is_not_missed_silently(self):
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            transform(fixture(CKAN_SAMPLE))
        self.assertTrue([m for m in caught.output
                         if "of 266 records" in m])

    def test_unknown_ed_flag_is_kept_raw_and_alarms(self):
        """A new feed value must not be silently bucketed as 'no ED'."""
        payload = _envelope([{"Name": "New Hospital", "ED": "Sometimes"}])
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            rows = transform(payload)
        self.assertEqual(rows[0].facility_type, "unmapped:Sometimes")
        self.assertTrue([m for m in caught.output if "unknown ED flag" in m])

    def test_record_without_a_name_is_skipped(self):
        payload = _envelope([{"Name": "  ", "ED": "Reporting wait times"},
                             {"Name": "Real", "ED": "Reporting wait times"}])
        with self.assertLogs(LOGGER, level="WARNING") as caught:
            rows = transform(payload)
        self.assertEqual([r.name for r in rows], ["Real"])
        self.assertTrue([m for m in caught.output if "no Name" in m])

    def test_null_sentinels_become_none(self):
        payload = _envelope([{"Name": "H", "ED": "Reporting wait times",
                              "Suburb": "NULL", "Phone": "", "LHD": "null"}])
        rows = transform(payload)
        self.assertIsNone(rows[0].suburb)
        self.assertIsNone(rows[0].phone)
        self.assertIsNone(rows[0].lhd)

    def test_known_flags_map_to_facility_types(self):
        payload = _envelope([
            {"Name": "A", "ED": "Reporting wait times"},
            {"Name": "B", "ED": "Not reporting wait times"},
            {"Name": "C", "ED": "No emergency department"},
        ])
        rows = transform(payload)
        self.assertEqual([r.facility_type for r in rows],
                         ["ed_reporting", "ed_not_reporting", "no_ed"])


if __name__ == "__main__":
    unittest.main()
