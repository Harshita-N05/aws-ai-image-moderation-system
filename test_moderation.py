"""
Unit tests for AWS Image Moderation System
Run: pytest tests/ -v
"""

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from lambda_handler import (
    _parse_labels,
    _response,
    _error_result,
    CONFIDENCE_THRESHOLD,
    FLAGGED_CATEGORIES,
)


class TestParseLabels(unittest.TestCase):

    def test_empty_labels_returns_empty(self):
        self.assertEqual(_parse_labels([]), [])

    def test_safe_labels_not_flagged(self):
        labels = [
            {"Name": "People",   "ParentName": "",       "Confidence": 99.0},
            {"Name": "Outdoors", "ParentName": "",       "Confidence": 95.0},
        ]
        result = _parse_labels(labels)
        self.assertEqual(result, [])

    def test_flagged_label_detected(self):
        labels = [
            {"Name": "Explicit Nudity", "ParentName": "Explicit Nudity", "Confidence": 96.5},
        ]
        result = _parse_labels(labels)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "Explicit Nudity")
        self.assertEqual(result[0]["confidence"], 96.5)

    def test_below_threshold_ignored(self):
        labels = [
            {"Name": "Alcohol", "ParentName": "Alcohol", "Confidence": 50.0},
        ]
        result = _parse_labels(labels)
        self.assertEqual(result, [])

    def test_sorted_by_confidence_descending(self):
        labels = [
            {"Name": "Alcohol",  "ParentName": "Alcohol",  "Confidence": 76.0},
            {"Name": "Weapons",  "ParentName": "Weapons",  "Confidence": 91.2},
            {"Name": "Tobacco",  "ParentName": "Tobacco",  "Confidence": 80.5},
        ]
        result = _parse_labels(labels)
        confidences = [l["confidence"] for l in result]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_multiple_flagged_labels(self):
        labels = [
            {"Name": "Weapons",         "ParentName": "Weapons",         "Confidence": 91.2},
            {"Name": "Violence",        "ParentName": "Violence",        "Confidence": 88.0},
            {"Name": "Explicit Nudity", "ParentName": "Explicit Nudity", "Confidence": 96.5},
        ]
        result = _parse_labels(labels)
        self.assertEqual(len(result), 3)

    def test_mixed_safe_and_flagged(self):
        labels = [
            {"Name": "People",          "ParentName": "",                "Confidence": 99.0},
            {"Name": "Explicit Nudity", "ParentName": "Explicit Nudity", "Confidence": 96.5},
        ]
        result = _parse_labels(labels)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "Explicit Nudity")


class TestResponseHelper(unittest.TestCase):

    def test_200_response_structure(self):
        resp = _response(200, {"key": "value"})
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("Content-Type", resp["headers"])
        body = json.loads(resp["body"])
        self.assertEqual(body["key"], "value")

    def test_400_response(self):
        resp = _response(400, {"error": "bad request"})
        self.assertEqual(resp["statusCode"], 400)

    def test_cors_header_present(self):
        resp = _response(200, {})
        self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")


class TestErrorResult(unittest.TestCase):

    def test_error_result_structure(self):
        result = _error_result("my-bucket", "test.jpg", "InvalidImage")
        self.assertEqual(result["bucket"],     "my-bucket")
        self.assertEqual(result["key"],        "test.jpg")
        self.assertEqual(result["action"],     "ERROR")
        self.assertFalse(result["is_flagged"])
        self.assertIn("timestamp", result)

    def test_error_result_contains_error_message(self):
        result = _error_result("b", "k", "Some error")
        self.assertEqual(result["error"], "Some error")


class TestLambdaHandler(unittest.TestCase):

    def _make_s3_event(self, bucket="test-bucket", key="test.jpg"):
        return {
            "Records": [{
                "eventSource": "aws:s3",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }]
        }

    def _make_api_event(self, bucket="test-bucket", key="test.jpg"):
        return {"body": json.dumps({"bucket": bucket, "key": key})}

    @patch("lambda_handler.rekognition")
    @patch("lambda_handler.sns")
    @patch("lambda_handler.s3")
    def test_s3_trigger_safe_image(self, mock_s3, mock_sns, mock_reko):
        mock_reko.detect_moderation_labels.return_value = {"ModerationLabels": []}
        mock_s3.put_object_tagging.return_value = {}

        from lambda_handler import lambda_handler
        resp = lambda_handler(self._make_s3_event(), {})
        self.assertEqual(resp["statusCode"], 200)
        body = json.loads(resp["body"])
        self.assertEqual(body["processed"], 1)
        result = body["moderation_results"][0]
        self.assertEqual(result["action"], "APPROVED")
        self.assertFalse(result["is_flagged"])
        mock_sns.publish.assert_not_called()

    @patch("lambda_handler.rekognition")
    @patch("lambda_handler.sns")
    @patch("lambda_handler.s3")
    @patch.dict(os.environ, {"SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:test"})
    def test_s3_trigger_flagged_image(self, mock_s3, mock_sns, mock_reko):
        mock_reko.detect_moderation_labels.return_value = {
            "ModerationLabels": [
                {"Name": "Explicit Nudity", "ParentName": "Explicit Nudity", "Confidence": 96.5}
            ]
        }
        mock_s3.put_object_tagging.return_value = {}
        mock_sns.publish.return_value = {"MessageId": "abc"}

        from lambda_handler import lambda_handler
        resp = lambda_handler(self._make_s3_event(), {})
        body = json.loads(resp["body"])
        result = body["moderation_results"][0]
        self.assertEqual(result["action"], "BLOCKED")
        self.assertTrue(result["is_flagged"])
        mock_sns.publish.assert_called_once()

    @patch("lambda_handler.rekognition")
    @patch("lambda_handler.s3")
    def test_api_gateway_trigger(self, mock_s3, mock_reko):
        mock_reko.detect_moderation_labels.return_value = {"ModerationLabels": []}
        mock_s3.put_object_tagging.return_value = {}

        from lambda_handler import lambda_handler
        resp = lambda_handler(self._make_api_event(), {})
        self.assertEqual(resp["statusCode"], 200)

    def test_api_missing_fields_returns_400(self):
        from lambda_handler import lambda_handler
        resp = lambda_handler({"body": json.dumps({})}, {})
        self.assertEqual(resp["statusCode"], 400)

    def test_unknown_event_returns_400(self):
        from lambda_handler import lambda_handler
        resp = lambda_handler({"unknown": "event"}, {})
        self.assertEqual(resp["statusCode"], 400)


class TestConfig(unittest.TestCase):

    def test_confidence_threshold_is_75(self):
        self.assertEqual(CONFIDENCE_THRESHOLD, 75.0)

    def test_flagged_categories_not_empty(self):
        self.assertGreater(len(FLAGGED_CATEGORIES), 0)

    def test_explicit_nudity_in_categories(self):
        self.assertIn("Explicit Nudity", FLAGGED_CATEGORIES)

    def test_weapons_in_categories(self):
        self.assertIn("Weapons", FLAGGED_CATEGORIES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
