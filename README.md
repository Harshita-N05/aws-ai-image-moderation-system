# AWS AI Image Moderation System

A fully serverless, event-driven content moderation pipeline. Images uploaded to S3
automatically trigger AI-based moderation via Amazon Rekognition, with confidence-scored
results, SNS alerting, and a REST API for clients — zero servers to manage, scales
automatically with load.

## Architecture

```
Client ──POST /upload-url──▶ API Gateway ──▶ Lambda (api_handler) ──▶ presigned S3 URL
   │
   └──PUT image───────────────────────────────────────────────────▶ S3 (uploads bucket)
                                                                          │
                                                              s3:ObjectCreated event
                                                                          ▼
                                                          Lambda (moderate_image)
                                                                          │
                                                        ┌─────────────────┼──────────────────┐
                                                        ▼                 ▼                  ▼
                                                  Rekognition       DynamoDB           S3 tagging
                                              DetectModerationLabels (audit log)     (status + score)
                                                        │
                                                if confidence ≥ threshold
                                                        ▼
                                                  SNS Topic ──▶ Email / downstream subscribers
                                                        │
                                                        ▼
                                            (optional) Quarantine S3 bucket

Client ──GET /status/{key}──▶ API Gateway ──▶ Lambda (api_handler) ──▶ DynamoDB lookup
```

## Components

| Service | Role |
|---|---|
| **S3** | Stores uploaded images; triggers Lambda on `ObjectCreated`; quarantine bucket for flagged content |
| **Lambda (`moderate_image.py`)** | Calls Rekognition, scores confidence, tags object, writes audit record, publishes SNS alert |
| **Rekognition** | `DetectModerationLabels` — detects nudity, violence, drugs, hate symbols, etc. with per-label confidence |
| **SNS** | Fan-out alerting (email, SQS, downstream Lambda) for any image over the confidence threshold |
| **API Gateway + Lambda (`api_handler.py`)** | REST endpoints: `POST /upload-url` (presigned upload), `GET /status/{key}` (moderation verdict) |
| **DynamoDB** | Audit trail of every moderation verdict (optional but recommended) |

## Key design decisions

- **Event-driven, not polling**: S3 → Lambda trigger means moderation runs the instant an
  object lands, with no idle compute cost.
- **Confidence-based routing**: a single `CONFIDENCE_THRESHOLD` env var controls the
  flag/clean cutoff so it can be tuned per use case without code changes.
- **Presigned URLs for upload**: clients never touch AWS credentials; the API Lambda hands
  out a short-lived, scoped S3 PUT URL.
- **Quarantine bucket**: flagged images are copied (not moved) to an isolated bucket for
  human review, keeping the original upload bucket's access policy simple.
- **Idempotent tagging**: every object gets `moderation-status` / `moderation-confidence`
  S3 tags, so downstream systems can filter via S3 Inventory or Batch Operations without
  needing DynamoDB.

## Deployment

Requires the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```bash
cd infrastructure
sam build --template-file template.yaml
sam deploy --guided \
  --stack-name image-moderation \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AlertEmail=you@example.com
```

This provisions: 2 S3 buckets, 2 Lambda functions, 1 DynamoDB table, 1 SNS topic
(+ email subscription), and an API Gateway REST API — all wired together.

After deploy, grab the `ApiEndpoint` output and test:

```bash
# 1. Request a presigned upload URL
curl -X POST https://<api>/prod/upload-url \
  -H "Content-Type: application/json" \
  -d '{"filename": "test.jpg", "contentType": "image/jpeg"}'

# 2. PUT the image to the returned uploadUrl
curl -X PUT "<uploadUrl>" -H "Content-Type: image/jpeg" --data-binary @test.jpg

# 3. Poll for the verdict (moderation runs async via the S3 trigger)
curl https://<api>/prod/status/<key>
```

## Local testing

```bash
pip install pytest boto3 --break-system-packages
AWS_DEFAULT_REGION=us-east-1 pytest tests/ -v
```

Tests cover the confidence-scoring and verdict-building logic in isolation
(no live AWS calls).

## Resume bullet (as implemented)

> Built a serverless content moderation pipeline on AWS: images uploaded to S3 trigger
> Lambda, which calls Rekognition to detect unsafe/explicit content with confidence
> scoring. Routed moderation alerts via SNS notifications; exposed REST endpoint through
> API Gateway — fully event-driven, zero-server architecture with auto-scaling.

## Possible extensions

- Step Functions to orchestrate multi-stage review (auto-reject / human-review / auto-approve)
- Cognito-authenticated API Gateway routes
- CloudWatch dashboard + alarms on flagged-image rate
- Batch re-scan of existing bucket contents via S3 Batch Operations
