import copy
import json
import unittest
from pathlib import Path
from hitch_inference.content import ContractError
from hitch_inference.request_validation import validate_request, engine_request


class RequestContractTest(unittest.TestCase):
    def test_shared_request_contract(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/request-contract.json").read_text())
        responses = {**fixture["lock"], "protocol": {**fixture["lock"]["protocol"], "api": "responses"}}
        # Legal JSON exponents can overflow float parsing; reject them as contract errors.
        for number in ("1e309", "-1e309"):
            with self.subTest(number), self.assertRaises(ContractError):
                validate_request(json.loads('{"max_output_tokens":' + number + '}'), responses)
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
