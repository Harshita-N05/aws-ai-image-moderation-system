"""
AWS AI Image Moderation System
Lambda Handler — triggered by S3 PutObject events

Flow: S3 Upload → Lambda → Rekognition → SNS Alert → API Gateway Response
"""

import json
import boto3
import logging
import os
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
rekognition = boto3.client("rekognition", region_name=os.environ.get("REGION", "us-east-1"))
sns         = boto3.client("sns",         region_name=os.environ.get("REGION", "us-east-1"))
s3          = boto3.client("s3",          region_name=os.environ.get("REGION", "us-east-1"))

# Config from environment
SNS_TOPIC_ARN       = os.environ.get("SNS_TOPIC_ARN", "")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "75.0"))

# Rekognition moderation label categories to flag
FLAGGED_CATEGORIES = {
    "Explicit Nudity",
    "Suggestive",
    "Violence",
    "Visually Disturbing",
    "Hate Symbols",
    "Tobacco",
    "Alcohol",
    "Gambling",
    "Rude Gestures",
    "Drugs",
    "Weapons",
}


def lambda_handler(event, context):
    """
    Entry point. Handles both:
      1. S3 event trigger (automated pipeline)
      2. API Gateway POST body (manual/test submission)
    """
    results = []

    # ── S3 trigger ──────────────────────────────────────────────────────────
    if "Records" in event:
        for record in event["Records"]:
            if record.get("eventSource") == "aws:s3":
                bucket = record["s3"]["bucket"]["name"]
                key    = record["s3"]["object"]["key"]
                logger.info(f"S3 trigger: s3://{bucket}/{key}")
                result = moderate_image(bucket, key)
                results.append(result)

    # ── API Gateway trigger ──────────────────────────────────────────────────
    elif "body" in event:
        body = json.loads(event.get("body") or "{}")
        bucket = body.get("bucket")
        key    = body.get("key")
        if not bucket or not key:
            return _response(400, {"error": "Missing 'bucket' or 'key' in request body"})
        result = moderate_image(bucket, key)
        results.append(result)

    else:
        return _response(400, {"error": "Unrecognised event format"})

    return _response(200, {"moderation_results": results, "processed": len(results)})


# ── Core moderation logic ────────────────────────────────────────────────────

def moderate_image(bucket: str, key: str) -> dict:
    """
    Calls Rekognition DetectModerationLabels on the S3 object.
    Returns a structured result dict with flagged labels and action taken.
    """
    logger.info(f"Moderating: s3://{bucket}/{key}")

    try:
        response = rekognition.detect_moderation_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MinConfidence=CONFIDENCE_THRESHOLD,
        )
    except rekognition.exceptions.InvalidImageException as e:
        logger.error(f"Invalid image: {e}")
        return _error_result(bucket, key, str(e))
    except Exception as e:
        logger.error(f"Rekognition error: {e}")
        return _error_result(bucket, key, str(e))

    labels          = response.get("ModerationLabels", [])
    flagged_labels  = _parse_labels(labels)
    is_flagged      = len(flagged_labels) > 0
    confidence_max  = max((l["confidence"] for l in flagged_labels), default=0.0)

    result = {
        "bucket":         bucket,
        "key":            key,
        "is_flagged":     is_flagged,
        "flagged_labels": flagged_labels,
        "label_count":    len(flagged_labels),
        "max_confidence": round(confidence_max, 2),
        "timestamp":      datetime.utcnow().isoformat() + "Z",
        "action":         "BLOCKED" if is_flagged else "APPROVED",
    }

    logger.info(f"Result: {result['action']} | labels={len(flagged_labels)} | max_conf={confidence_max:.1f}%")

    # Send SNS alert if flagged (read env var at call time so tests can patch it)
    sns_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if is_flagged and sns_arn:
        _send_alert(result)

    # Tag the S3 object with moderation outcome
    _tag_s3_object(bucket, key, result)

    return result


def _parse_labels(labels: list) -> list:
    """Filter and format Rekognition labels above threshold."""
    flagged = []
    for label in labels:
        category = label.get("ParentName") or label.get("Name", "")
        name     = label.get("Name", "")
        conf     = label.get("Confidence", 0.0)
        if (category in FLAGGED_CATEGORIES or name in FLAGGED_CATEGORIES) and conf >= CONFIDENCE_THRESHOLD:
            flagged.append({
                "category":   category,
                "label":      name,
                "confidence": round(conf, 2),
            })
    # Sort by confidence descending
    return sorted(flagged, key=lambda x: x["confidence"], reverse=True)


def _send_alert(result: dict):
    """Publish SNS notification for flagged content."""
    subject = f"[ALERT] Flagged content detected: {result['key']}"
    message = (
        f"Image Moderation Alert\n"
        f"{'─' * 40}\n"
        f"File     : s3://{result['bucket']}/{result['key']}\n"
        f"Action   : {result['action']}\n"
        f"Labels   : {result['label_count']}\n"
        f"Top Label: {result['flagged_labels'][0]['label']} "
        f"({result['flagged_labels'][0]['confidence']}%)\n"
        f"Time     : {result['timestamp']}\n\n"
        f"Full Labels:\n"
        + "\n".join(
            f"  • {l['label']} [{l['category']}] — {l['confidence']}%"
            for l in result["flagged_labels"]
        )
    )
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        logger.info("SNS alert sent.")
    except Exception as e:
        logger.error(f"SNS publish failed: {e}")


def _tag_s3_object(bucket: str, key: str, result: dict):
    """Tag the S3 object with moderation outcome for downstream filtering."""
    try:
        s3.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={
                "TagSet": [
                    {"Key": "moderation_status", "Value": result["action"]},
                    {"Key": "flagged_labels",    "Value": str(result["label_count"])},
                    {"Key": "max_confidence",    "Value": str(result["max_confidence"])},
                ]
            },
        )
    except Exception as e:
        logger.warning(f"S3 tagging failed (non-critical): {e}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _error_result(bucket: str, key: str, error: str) -> dict:
    return {
        "bucket":    bucket,
        "key":       key,
        "is_flagged": False,
        "error":     error,
        "action":    "ERROR",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
