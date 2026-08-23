"""MBS release parser: the entity-expansion guard, and the drift alarms.

The rest of this suite covers the ED waits pipeline, so before this file the
MBS parser had no coverage at all -- including the hardening in
`src/ingest_mbs_xml.py` that made `defusedxml` a hard dependency. A security
property with no test is one refactor away from being silently reverted, so
the first test here asserts the parser's *provenance*, not just its output.

`fixtures/mbs_release_sample.xml` is a verbatim two-record excerpt from the
real July 2026 release (items 3 and 4), consistent with this suite's rule
that fixtures are captured payloads rather than hand-written approximations.
The malicious inputs are built at run time instead of committed, so the repo
never carries a file whose only purpose is to blow something up.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from decimal import Decimal

from defusedxml.common import EntitiesForbidden

from tests import FIXTURES  # also bootstraps sys.path for src/

import ingest_mbs_xml as mbs

SAMPLE = os.path.join(FIXTURES, "mbs_release_sample.xml")

# Ten entities per level: shallow enough to stay a text file, deep enough that
# expanding it would be fatal (10**8 copies) if the guard ever came off.
BILLION_LAUGHS = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE MBS_XML [\n"
    '  <!ENTITY a0 "dos">\n'
    + "".join(
        '  <!ENTITY a{} "{}">\n'.format(i, "".join("&a{};".format(i - 1) for _ in range(10)))
        for i in range(1, 9)
    )
    + "]>\n<MBS_XML><Data><ItemNum>&a8;</ItemNum></Data></MBS_XML>\n"
)

EXTERNAL_ENTITY = (
    '<?xml version="1.0"?>\n'
    '<!DOCTYPE MBS_XML [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]>\n'
    "<MBS_XML><Data><ItemNum>&xxe;</ItemNum></Data></MBS_XML>\n"
)


def write_temp(text: str) -> str:
    handle, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class TestParserProvenance(unittest.TestCase):
    def test_iterparse_comes_from_defusedxml(self):
        """Guards the actual fix: a revert to stdlib ET fails here first."""
        self.assertTrue(
            mbs.iterparse.__module__.startswith("defusedxml"),
            "parser is {!r}; the stdlib parser expands entities".format(
                mbs.iterparse.__module__
            ),
        )


class TestMaliciousInput(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for path in self.paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write(self, text):
        path = write_temp(text)
        self.paths.append(path)
        return path

    def test_entity_expansion_is_refused(self):
        path = self._write(BILLION_LAUGHS)
        with self.assertRaises(EntitiesForbidden):
            list(mbs.stream_records(path))

    def test_external_entity_is_refused(self):
        """The same guard also blocks file disclosure, not just the DoS."""
        path = self._write(EXTERNAL_ENTITY)
        with self.assertRaises(EntitiesForbidden):
            list(mbs.stream_records(path))


class TestDriftAlarms(unittest.TestCase):
    def setUp(self):
        self.paths = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for path in self.paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write(self, text):
        path = write_temp(text)
        self.paths.append(path)
        return path

    def test_unexpected_root_raises(self):
        path = self._write("<Wrong><Data><ItemNum>1</ItemNum></Data></Wrong>")
        with self.assertRaises(ValueError) as ctx:
            list(mbs.stream_records(path))
        self.assertIn("MBS_XML", str(ctx.exception))

    def test_zero_records_raises(self):
        """An empty release must fail loudly, not ingest nothing quietly."""
        path = self._write("<MBS_XML></MBS_XML>")
        with self.assertRaises(ValueError) as ctx:
            list(mbs.stream_records(path))
        self.assertIn("no <Data> records", str(ctx.exception))


class TestRealReleaseExcerpt(unittest.TestCase):
    def setUp(self):
        self.records = list(mbs.stream_records(SAMPLE))

    def test_yields_every_record(self):
        self.assertEqual(len(self.records), 2)
        self.assertEqual([r["ItemNum"] for r in self.records], ["3", "4"])

    def test_fields_survive_the_defused_parser(self):
        """Hardening must not have changed what comes out of a real file."""
        first = self.records[0]
        self.assertEqual(first["ScheduleFee"], "20.55")
        self.assertEqual(first["Benefit100"], "20.55")
        self.assertEqual(first["FeeType"], "N")
        self.assertTrue(first["Description"].startswith("Professional attendance"))

    def test_empty_elements_become_empty_strings(self):
        """<SubItemNum></SubItemNum> must not surface as None."""
        self.assertEqual(self.records[0]["SubItemNum"], "")

    def test_to_row_maps_a_real_record(self):
        row = mbs.to_row(self.records[0])
        self.assertIsNotNone(row)
        self.assertEqual(row.item_num, 3)
        self.assertEqual(row.category, "1/A1")
        self.assertEqual(row.schedule_fee, Decimal("20.55"))
        self.assertEqual(row.benefit_100, Decimal("20.55"))
        self.assertIsNone(row.benefit_75)
        self.assertFalse(row.is_derived)


class TestToRowGuards(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_non_numeric_item_num_is_skipped(self):
        self.assertIsNone(mbs.to_row({"ItemNum": "A1", "Description": "x"}))

    def test_missing_description_is_skipped(self):
        self.assertIsNone(mbs.to_row({"ItemNum": "3", "Description": ""}))

    def test_derived_fee_item_is_flagged(self):
        """FeeType D items carry no ScheduleFee by design (see the MCF doc)."""
        row = mbs.to_row(
            {"ItemNum": "104", "Description": "derived", "FeeType": "D",
             "DerivedFee": "See item 104"}
        )
        self.assertIsNotNone(row)
        self.assertTrue(row.is_derived)
        self.assertIsNone(row.schedule_fee)


if __name__ == "__main__":
    unittest.main()
