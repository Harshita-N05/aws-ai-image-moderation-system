"""
AWS AI Image Moderation System
--------------------------------
Lambda function triggered by S3 PutObject events. Calls Amazon Rekognition's
DetectModerationLabels API to identify unsafe/explicit content, scores results
by confidence, tags the S3 object, and publishes alerts via SNS for any image
that exceeds the configured confidence threshold.

Env vars expected:
    SNS_TOPIC_ARN          - ARN of the SNS topic for moderation alerts
    CONFIDENCE_THRESHOLD   - float, e.g. "80" (Rekognition confidence %, default 80)
    MIN_LABEL_CONFIDENCE   - float, e.g. "60" (min confidence for Rekognition to return a label, default 60)
    MODERATION_TABLE       - (optional) DynamoDB table name to persist results
    QUARANTINE_BUCKET      - (optional) bucket to copy flagged images into
"""

import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rekognition = boto3.client("rekognition")
s3 = boto3.client("s3")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "80"))
MIN_LABEL_CONFIDENCE = float(os.environ.get("MIN_LABEL_CONFIDENCE", "60"))
MODERATION_TABLE = os.environ.get("MODERATION_TABLE")
QUARANTINE_BUCKET = os.environ.get("QUARANTINE_BUCKET")

SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png")


def lambda_handler(event, context):
    """Entry point invoked by the S3 -> Lambda event notification."""
    results = []

    for record in event.get("Records", []):
        try:
            result = process_record(record)
            results.append(result)
        except ClientError as e:
            logger.error(f"AWS client error processing record: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error processing record: {e}")
            raise

    return {
        "statusCode": 200,
        "body": json.dumps({"processed": len(results), "results": results}, default=str),
    }


def process_record(record):
    bucket = record["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
    size = record["s3"]["object"].get("size", 0)

    logger.info(f"Processing s3://{bucket}/{key} ({size} bytes)")

    if not key.lower().endswith(SUPPORTED_EXTENSIONS):
        logger.info(f"Skipping unsupported file type: {key}")
        return {"key": key, "status": "skipped", "reason": "unsupported_file_type"}

    moderation_labels = detect_moderation_labels(bucket, key)
    verdict = build_verdict(bucket, key, moderation_labels)

    persist_result(verdict)
    tag_object(bucket, key, verdict)

    if verdict["flagged"]:
        notify(verdict)
        if QUARANTINE_BUCKET:
            quarantine_object(bucket, key)

    return verdict


def detect_moderation_labels(bucket, key):
    """Call Rekognition's DetectModerationLabels API on the S3 object."""
    response = rekognition.detect_moderation_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}},
        MinConfidence=MIN_LABEL_CONFIDENCE,
    )
    return response.get("ModerationLabels", [])


def build_verdict(bucket, key, labels):
    """Score labels and decide whether the image should be flagged."""
    top_confidence = max((l["Confidence"] for l in labels), default=0.0)
    flagged = top_confidence >= CONFIDENCE_THRESHOLD

    categories = sorted(
        [
            {
                "name": l["Name"],
                "parent": l.get("ParentName", ""),
                "confidence": round(l["Confidence"], 2),
            }
            for l in labels
        ],
        key=lambda x: x["confidence"],
        reverse=True,
    )

    return {
        "bucket": bucket,
        "key": key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "flagged": flagged,
        "top_confidence": round(top_confidence, 2),
        "threshold": CONFIDENCE_THRESHOLD,
        "labels": categories,
        "status": "flagged" if flagged else "clean",
    }


def tag_object(bucket, key, verdict):
    """Tag the S3 object so downstream consumers can filter via S3 inventory/queries."""
    try:
        s3.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={
                "TagSet": [
                    {"Key": "moderation-status", "Value": verdict["status"]},
                    {"Key": "moderation-confidence", "Value": str(verdict["top_confidence"])},
                ]
            },
        )
    except ClientError as e:
        logger.warning(f"Failed to tag object {key}: {e}")


def quarantine_object(bucket, key):
    """Copy a flagged image into a separate quarantine bucket for manual review."""
    try:
        s3.copy_object(
            Bucket=QUARANTINE_BUCKET,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key},
        )
        logger.info(f"Quarantined s3://{bucket}/{key} -> s3://{QUARANTINE_BUCKET}/{key}")
    except ClientError as e:
        logger.warning(f"Failed to quarantine object {key}: {e}")


def persist_result(verdict):
    """Optionally store the moderation verdict in DynamoDB for auditing."""
    if not MODERATION_TABLE:
        return
    try:
        table = dynamodb.Table(MODERATION_TABLE)
        item = json.loads(json.dumps(verdict), parse_float=Decimal)
        item["id"] = f"{verdict['bucket']}/{verdict['key']}#{verdict['timestamp']}"
        table.put_item(Item=item)
    except ClientError as e:
        logger.warning(f"Failed to persist verdict to DynamoDB: {e}")


def notify(verdict):
    """Publish a moderation alert to SNS."""
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_TOPIC_ARN not configured; skipping notification")
        return

    top_labels = ", ".join(
        f"{l['name']} ({l['confidence']}%)" for l in verdict["labels"][:3]
    ) or "unspecified"

    message = (
        f"🚨 Unsafe content detected\n\n"
        f"Image: s3://{verdict['bucket']}/{verdict['key']}\n"
        f"Top confidence: {verdict['top_confidence']}%\n"
        f"Threshold: {verdict['threshold']}%\n"
        f"Flagged categories: {top_labels}\n"
        f"Timestamp: {verdict['timestamp']}"
    )

    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="Image Moderation Alert",
            Message=message,
            MessageAttributes={
                "status": {"DataType": "String", "StringValue": "flagged"},
                "confidence": {
                    "DataType": "Number",
                    "StringValue": str(verdict["top_confidence"]),
                },
            },
        )
        logger.info(f"SNS alert published for {verdict['key']}")
    except ClientError as e:
        logger.error(f"Failed to publish SNS alert: {e}")
