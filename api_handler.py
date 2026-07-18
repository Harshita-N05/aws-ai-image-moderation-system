"""
API Gateway REST handler for the Image Moderation System.

Exposes two endpoints (via API Gateway Lambda proxy integration):

  POST /upload-url        -> returns a presigned S3 URL the client can PUT an image to
  GET  /status/{key+}     -> returns the moderation verdict for a given S3 key (from DynamoDB)

Env vars expected:
    UPLOAD_BUCKET      - S3 bucket clients upload images into (triggers moderate_image.py)
    MODERATION_TABLE   - DynamoDB table storing moderation verdicts
    URL_EXPIRY_SECONDS - presigned URL TTL (default 300)
"""

import json
import logging
import os
import uuid

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET")
MODERATION_TABLE = os.environ.get("MODERATION_TABLE")
URL_EXPIRY_SECONDS = int(os.environ.get("URL_EXPIRY_SECONDS", "300"))

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def lambda_handler(event, context):
    route_key = event.get("routeKey") or f"{event.get('httpMethod')} {event.get('resource')}"
    logger.info(f"Routing: {route_key}")

    try:
        if event.get("httpMethod") == "POST" and "upload-url" in event.get("path", ""):
            return handle_upload_url(event)
        if event.get("httpMethod") == "GET" and "status" in event.get("path", ""):
            return handle_status(event)
        return respond(404, {"error": "Not found"})
    except ClientError as e:
        logger.error(f"AWS error: {e}")
        return respond(502, {"error": "Upstream AWS error", "detail": str(e)})
    except Exception as e:
        logger.exception("Unhandled error")
        return respond(500, {"error": "Internal server error", "detail": str(e)})


def handle_upload_url(event):
    """Generate a presigned PUT URL so clients can upload directly to S3."""
    body = json.loads(event.get("body") or "{}")
    filename = body.get("filename", f"{uuid.uuid4()}.jpg")
    content_type = body.get("contentType", "image/jpeg")

    if not any(filename.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
        return respond(400, {"error": "Only jpg/jpeg/png files are supported"})

    key = f"uploads/{uuid.uuid4()}-{filename}"

    presigned_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": UPLOAD_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=URL_EXPIRY_SECONDS,
    )

    return respond(
        200,
        {
            "uploadUrl": presigned_url,
            "key": key,
            "expiresIn": URL_EXPIRY_SECONDS,
            "statusEndpoint": f"/status/{key}",
        },
    )


def handle_status(event):
    """Look up the moderation verdict for a given key from DynamoDB."""
    key = event.get("pathParameters", {}).get("key")
    if not key:
        return respond(400, {"error": "Missing key path parameter"})

    if not MODERATION_TABLE:
        return respond(501, {"error": "Moderation table not configured"})

    table = dynamodb.Table(MODERATION_TABLE)
    response = table.scan(
        FilterExpression="contains(#k, :key)",
        ExpressionAttributeNames={"#k": "key"},
        ExpressionAttributeValues={":key": key},
    )
    items = response.get("Items", [])

    if not items:
        return respond(202, {"key": key, "status": "pending", "message": "Moderation still in progress or not found"})

    latest = sorted(items, key=lambda i: i["timestamp"], reverse=True)[0]
    return respond(200, latest, default=str)


def respond(status_code, body, default=None):
    return {
        "statusCode": status_code,
        "headers": HEADERS,
        "body": json.dumps(body, default=default),
    }
