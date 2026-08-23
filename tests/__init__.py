"""Offline test suite for the NSW ED ingest pipeline.

Stdlib `unittest`, deliberately: the pipeline's only runtime dependencies are
requests and psycopg2, and a test framework that has to be installed before
the tests can run is a test framework that does not get run. Everything here
is offline -- no network, no Postgres, no psycopg2 driver required.

From the repo root:

    python -m unittest discover -s tests -v

Fixtures are the real captured payloads in `fixtures/`, so the parser tests
assert against data NSW Health actually served rather than something written
to make the code look right.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
FIXTURES = os.path.join(REPO_ROOT, "fixtures")

if SRC not in sys.path:
    sys.path.insert(0, SRC)
# The launcher scripts are importable too: poll_loop.py owns the backoff
# policy, which is decision logic worth testing rather than only running.
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def fixture(name: str):
    """Load a JSON fixture by filename."""
    import json

    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


# The two captured wait payloads, by what they exercise.
WAITS_FULL = "wait_payload_20260719T094206Z.json"      # anchor reporting
WAITS_NOT_REPORTING = "wait_payload_20260719T094109Z.json"  # anchor silent
CKAN_SAMPLE = "ckan_datastore_search_sample.json"
