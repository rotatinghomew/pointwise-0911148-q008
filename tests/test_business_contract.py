import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.contracts import visible
from app.domain import OPERATIONS, seed_dataset
from app.repository import digest
from app.service import Service


class BusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.operation = next(iter(OPERATIONS))

    def run_check(self):
        return OPERATIONS[self.operation](self.data, {})

    def test_mapping_change_splits_area_identity(self):
        rows = [row for row in self.run_check()['series'] if row['region_code'] == 'r7']
        self.assertEqual({row['area_id'] for row in rows}, {'r7', 'r7-realigned'})
        self.assertEqual(len(rows), len({row['month'] for row in rows}))
        for row in rows:
            datetime.strptime(row['month'], '%Y-%m')

    def test_latest_revision_and_missing_values(self):
        result = self.run_check()
        self.assertEqual(len(result['series']), 240)
        self.assertGreater(result['missing_values'], 0)
        self.assertTrue(any(row['rate'] is None for row in result['series']))


    def test_fixture_timestamps_do_not_arrive_before_events(self):
        from app.contracts import instant
        for records in self.data.values():
            for row in records:
                if 'received_at' not in row:
                    continue
                for key in ('event_at', 'assigned_at', 'signed_at', 'paid_at', 'issued_at'):
                    if row.get(key):
                        self.assertLessEqual(instant(row[key]), instant(row['received_at']))

    def test_seed_and_live_source_have_one_contract(self):
        from app.contracts import load
        self.assertEqual(self.data, load('data/source.json'))
