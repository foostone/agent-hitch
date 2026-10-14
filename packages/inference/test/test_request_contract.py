import copy
import json
import unittest
from pathlib import Path
from hitch_inference.request_validation import validate_request, engine_request


class RequestContractTest(unittest.TestCase):
    def test_shared_request_contract(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/request-contract.json").read_text())
        for row in fixture["cases"]:
            with self.subTest(row["name"]):
                frozen = {**fixture["lock"], "protocol": {**fixture["lock"]["protocol"], "api": row["api"]}}
                if not row["valid"]:
                    with self.assertRaises(ValueError):
                        validate_request(copy.deepcopy(row["request"]), frozen)
                    continue
                canonical = validate_request(copy.deepcopy(row["request"]), frozen)
                self.assertEqual(canonical, row["canonical"])
                self.assertEqual(validate_request(canonical, frozen), canonical)
                self.assertEqual(engine_request(canonical, frozen), row["engine"])
