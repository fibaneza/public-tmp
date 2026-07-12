# Incremental Processing (Avoid Reprocessing Items)

## Goal

Prevent reprocessing documents that have already been ingested into the Amazon Bedrock Knowledge Base unless they have changed.

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
