"""
Unit tests for moderate_image.py — verdict scoring logic.
Run with: pytest tests/test_moderate_image.py
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

os.environ.setdefault("CONFIDENCE_THRESHOLD", "80")
os.environ.setdefault("MIN_LABEL_CONFIDENCE", "60")

import moderate_image  # noqa: E402


def test_build_verdict_flags_high_confidence_label():
    labels = [
        {"Name": "Explicit Nudity", "ParentName": "", "Confidence": 92.5},
        {"Name": "Graphic Violence", "ParentName": "Violence", "Confidence": 65.0},
    ]
    verdict = moderate_image.build_verdict("test-bucket", "test.jpg", labels)

    assert verdict["flagged"] is True
    assert verdict["top_confidence"] == 92.5
    assert verdict["status"] == "flagged"
    assert verdict["labels"][0]["name"] == "Explicit Nudity"


def test_build_verdict_clean_image_below_threshold():
    labels = [{"Name": "Rude Gestures", "ParentName": "", "Confidence": 45.0}]
    verdict = moderate_image.build_verdict("test-bucket", "clean.jpg", labels)

    assert verdict["flagged"] is False
    assert verdict["status"] == "clean"


def test_build_verdict_no_labels():
    verdict = moderate_image.build_verdict("test-bucket", "blank.jpg", [])

    assert verdict["flagged"] is False
    assert verdict["top_confidence"] == 0.0
    assert verdict["labels"] == []


def test_labels_sorted_descending_by_confidence():
    labels = [
        {"Name": "A", "ParentName": "", "Confidence": 55.0},
        {"Name": "B", "ParentName": "", "Confidence": 95.0},
        {"Name": "C", "ParentName": "", "Confidence": 70.0},
    ]
    verdict = moderate_image.build_verdict("bucket", "key.jpg", labels)
    confidences = [l["confidence"] for l in verdict["labels"]]

    assert confidences == sorted(confidences, reverse=True)


@pytest.mark.parametrize("filename,expected", [
    ("photo.jpg", True),
    ("photo.JPEG", True),
    ("photo.png", True),
    ("document.pdf", False),
    ("video.mp4", False),
])
def test_supported_extensions(filename, expected):
    assert (filename.lower().endswith(moderate_image.SUPPORTED_EXTENSIONS)) == expected
