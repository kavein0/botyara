import json
import unittest
from urllib.parse import parse_qs, urlsplit
import requests
from client import build_request
from profiles import MIXED_PROFILE, STANDARD_PROFILE


class ProfileTests(unittest.TestCase):
    def test_prepared_requests(self):
        result = {"callback_uuid": "demo", "task_id": 7, "result": "тест & +"}
        operations = [("checkin", "/checkin", "payload & +"),
                      ("get_task", "/poll", "callback & +"),
                      ("post_task", "/results", result)]
        for profile in (STANDARD_PROFILE, MIXED_PROFILE):
            for operation, endpoint, data in operations:
                rule = profile[operation]
                with self.subTest(operation=operation, transport=rule["transport"]):
                    options = build_request(endpoint, rule, data)
                    self.assertEqual(options.pop("timeout"), 5)
                    request = requests.Request(**options).prepare()
                    self.assertEqual(request.method, rule["method"])
                    self.assertEqual(urlsplit(request.url).path, endpoint)
                    if rule["transport"] == "json":
                        self.assertEqual(request.headers["Content-Type"], "application/json")
                        value = json.loads(request.body)[rule["field"]]
                    else:
                        encoded = urlsplit(request.url).query if rule["transport"] == "query" else request.body
                        value = parse_qs(encoded)[rule["field"]][0]
                        if rule["transport"] == "query":
                            self.assertIsNone(request.body)
                        else:
                            self.assertEqual(request.headers["Content-Type"], "application/x-www-form-urlencoded")
                        if isinstance(data, dict):
                            value = json.loads(value)
                    self.assertEqual(value, data)


if __name__ == "__main__":
    unittest.main()
