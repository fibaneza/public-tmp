# Generate AWS CDK Architecture for HATEOAS to Amazon Bedrock Knowledge Base

## Objective

Design a production-ready AWS solution that ingests documents daily from an external HATEOAS REST API into an Amazon Bedrock Knowledge Base.

## Functional Requirements

1. Trigger automatically once per day.
2. Authenticate with the external service and obtain a bearer token once per workflow execution whenever possible.
3. Call the HATEOAS search endpoint, which returns more than 200 item links.
4. Split the results into configurable batches (for example, 10–25 items per batch).
5. Process batches in parallel using AWS Step Functions Distributed Map.
6. Each worker should:
   - Retrieve the item content.
   - Retrieve the item metadata.
   - Transform the data into the required ingestion format.
   - Store the output in Amazon S3.
7. After all batches complete successfully, start an Amazon Bedrock Knowledge Base ingestion job and wait for its completion.
8. Implement retries with exponential backoff for transient failures (HTTP 5xx, throttling, and timeouts).
9. Handle partial failures by continuing other batches and producing a summary of failed items.
10. Emit metrics, logs, and alarms for operational monitoring.

## Non-Functional Requirements

- Follow AWS Well-Architected best practices.
- Use EventBridge Scheduler, Step Functions Standard, Lambda, Amazon S3, Secrets Manager, CloudWatch, SNS, and Amazon Bedrock Knowledge Bases.
- Apply least-privilege IAM.
- Encrypt data at rest with AWS KMS.
- Ensure idempotent processing.
- Configure Distributed Map `MaxConcurrency` to respect external API rate limits.
- Provide CloudWatch dashboards and alarms.
- Support configurable batch size, schedule, retry policy, and concurrency through configuration.

## Deliverables

- Architecture diagram
- Step Functions workflow diagram
- Sequence diagram
- AWS CDK implementation outline
- IAM role definitions
- Retry strategy
- Monitoring and alerting design
- Failure recovery strategy
- Cost optimization recommendations

## High-Level Architecture

```text
                           EventBridge Scheduler
                         (Daily at configured time)
                                    │
                                    ▼
                      AWS Step Functions (Orchestrator)
                                    │
            ┌───────────────────────┼─────────────────────────┐
            │                       │                         │
            ▼                       ▼                         ▼
     Get Auth Token         Search HATEOAS API         Error Handling
       Lambda                     Lambda
            │
            ▼
  Returns 200+ Item Links
            │
            ▼
      Split into batches
      (e.g. 20 items/batch)
            │
            ▼
        Distributed Map
   (Parallel execution)
            │
            ▼
 ┌──────────────────────────────────────────────┐
 │ Lambda Ingestion Worker                      │
 │                                              │
 │ foreach item in batch                        │
 │   GET item                                   │
 │   GET metadata                               | 
 |   Check on DynamoDB                          │
 │   Transform                                  │
 │   Upload document to S3                      │
 │   Store metadata                             │
 └──────────────────────────────────────────────┘
            │
            ▼
     Wait for completion
            │
            ▼
 Start Bedrock KB Sync Job
            │
            ▼
 Monitor until Complete
            │
            ▼
      Success / Failure
```

## Recommended Architecture

```text
Search API
    │
    ▼
HATEOAS Results
    │
    ▼
Worker Lambda
    │
    ├── Extract unique Item ID
    ├── Extract LastModified / Version / ETag
    │
    ▼
DynamoDB Metadata Table
    │
    ├── Exists?
    │      │
    │      ├── No → Process document
    │      │
    │      └── Yes
    │             │
    │             ├── Metadata changed?
    │             │
    │             ├── No → Skip
    │             │
    │             └── Yes → Reprocess
    │
    ▼
Upload to S3
    │
    ▼
Update DynamoDB
```

## Step Function Flow

```text
Start

↓

GetToken

↓

Search API

↓

Chunk Results

↓

Distributed Map

↓

Worker Lambda

↓

All Complete?

↓

No → Retry Failed Batch

↓

Yes

↓

Start KB Sync

↓

Wait

↓

Check Status

↓

Completed

↓

Success
```

## Authentication Strategy

```text
Step Function

↓

GetToken Lambda

↓

Token valid?

↓

No

↓

Call Auth API

↓

Return Token

↓

Pass token to every worker
```

If the token expires during execution:
```text
Worker

↓

401 Unauthorized

↓

Refresh Token Lambda

↓

Retry request
```

## Parallelism

```text
240 items

↓

24 batches

↓

Distributed Map

↓

MaxConcurrency = 20
```

This prevents:

- API throttling
- Lambda burst limits
- Bedrock ingestion bottlenecks

## Retry Strategy

Every external REST call should have retries.
```text
Retry

Errors:
- 500
- 502
- 503
- 504
- Timeout

Interval:
2 sec

Backoff:
2x

Attempts:
5
```
Do not retry:

- 400
- 404
- Invalid payload

## Error Handling

Worker Lambda
```text
Download

↓

Transform

↓

Failure?

↓

Send failure record

↓

Continue
```
The workflow should continue processing other batches even if individual items fail. At the end, generate a report of successful and failed items for review.

## Monitoring

CloudWatch Dashboard:

- Number of items found
- Items ingested
- Failed items
- Average execution time
- Lambda duration
- Lambda errors
- Step Function failures
- KB ingestion duration
- API latency

Recommended alarms:

- Step Function Failed
- Lambda Error > 5
- Lambda Throttles
- KB Sync Failed
- Daily workflow not executed
- Authentication failures
- High external API latency

## Security Best Practices

- Store API credentials in AWS Secrets Manager.
- Retrieve secrets once per execution and cache the authentication token in the Step Functions execution context.
- Encrypt S3 with SSE-KMS.
- Use least-privilege IAM roles for Lambda and Step Functions.
- Place Lambda functions in a VPC only if required to access private resources; otherwise, keep them outside to avoid unnecessary networking complexity and latency.
- Enable CloudTrail for auditing.
- Enable CloudWatch Logs with appropriate retention.

## Performance Recommendations

- Use Step Functions Distributed Map for large-scale parallelism.
- Batch 10–25 items per worker Lambda to reduce invocation overhead.
- Set MaxConcurrency based on the external API's rate limits.
- Compress large payloads before storing them in S3 if appropriate.
- Make worker Lambdas idempotent so retries do not create duplicate documents.

## DynamoDB Table

**Table:** `KnowledgeBaseIngestion`

**Partition Key**
- `itemId`

**Attributes**
- sourceUrl
- lastModified
- version
- etag
- checksum
- lastProcessed
- ingestionStatus
- bedrockDocumentId

## Change Detection Priority

Use the first available value:

1. Version
2. LastModified
3. ETag
4. SHA-256 checksum of the content

If none are available, compute a checksum after downloading the document.

## Worker Logic

1. Receive item URL.
2. Read metadata.
3. Query DynamoDB by `itemId`.
4. If no record exists:
   - Process the document.
5. If the record exists:
   - Compare Version / LastModified / ETag / Checksum.
6. If unchanged:
   - Skip processing.
7. If changed:
   - Download and process the document.
8. Update DynamoDB with the latest metadata.

## Step Functions Flow

```text
Search

↓

Distributed Map

↓

Check DynamoDB

↓

Changed?

├── No
│
└── Skip

└── Yes
     │
     ▼
Download

↓

Transform

↓

Upload S3

↓

Update DynamoDB
```

## Optional Optimization

If the search API returns `LastModified`, `Version`, or `ETag`, perform the comparison **before downloading the full document**, significantly reducing API calls, bandwidth, and execution time.

## Benefits

- Avoids downloading unchanged documents.
- Reduces Lambda execution time and cost.
- Reduces external API traffic.
- Shortens Bedrock ingestion windows.
- Scales efficiently to hundreds of thousands of documents.
- Enables resumable executions after failures.

## Best Practices

- Store one DynamoDB item per source document.
- Make processing idempotent.
- Use conditional writes to avoid race conditions.
- Add a TTL attribute only if you want to automatically forget documents after a retention period.
- Emit CloudWatch metrics for:
  - Documents processed
  - Documents skipped
  - Documents updated
  - Documents failed
