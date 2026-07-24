# RAG Ingestion & Citation Delivery — Architecture & Code Review

Companion to [`rag-ingestion-pdf-preview-design.md`](./rag-ingestion-pdf-preview-design.md).
Reviews the design against AWS Well-Architected best practices, the KB metadata
rules in [`aws_kb.md`](./aws_kb.md), the ingestion requirements in
[`prompt.md`](./prompt.md), and the wider system in
[`Plan/architecture-v2.md`](./Plan/architecture-v2.md). Adds focused pseudo-code
(not a full project) for the four touched components: **CDK (infra SDK, Python
3)**, **ingestion Lambda**, **FastAPI delivery**, and **Next.js**.

Library APIs (FastAPI `StreamingResponse`, CDK v2 Python constructs) were checked
against current docs via Context7.

---

## 1. Review summary — findings by severity

| # | Severity | Area | Finding |
|---|----------|------|---------|
| F1 | 🔴 High | KB scoping | Content-hash keying keeps **every** superseded `<hash>.md` under `kb-content/`, which is *inside* `inclusionPrefixes`. The KB ingests all versions → duplicate/stale chunks and citations to old content. Directly contradicts §2's "no duplicate chunks." |
| F2 | 🔴 High | Networking | The ingestion Lambda must call the **external HATEOAS API (public internet)**. Putting it "in the VPC with no NAT" (§3) makes that call impossible. Gateway/Interface endpoints only reach AWS services, not the third-party API. |
| F3 | 🔴 High | Data lifecycle | S3 Lifecycle **cannot express "expire non-referenced objects"** (§2/§10). Age-based expiry deletes the *current* object too once it ages past the window — an unchanged doc's only object is never rewritten, so it silently expires and breaks downloads. |
| F4 | 🔴 High | Caching correctness | `immutable` + `max-age=86400` is only safe if the **browser-facing URL changes when content changes**. If the download route is `/documents/{doc_id}/pdf` (stable), the browser serves stale bytes for 24h after an update. The S3 key rotates; the URL must too (hash in path/query). |
| F5 | 🟠 Med | IAM | §7 omits the **Bedrock KB service role**, which needs `s3:GetObject` on `kb-content/*` **and** `kms:Decrypt` on the bucket key to sync. Also missing: KMS grants for the Lambda/ECS roles. |
| F6 | 🟠 Med | Security (encryption) | No **SSE-KMS** anywhere, despite `prompt.md` NFR "encrypt at rest with KMS." No bucket policy pinning access to the VPC endpoint (`aws:SourceVpce`) or denying non-TLS. "Nothing public" is asserted but not enforced at the resource policy. |
| F7 | 🟠 Med | Delivery capability | §5 advertises **Range/resumable downloads**, but the described path always does a full `GetObject` and `StreamingResponse` sets no `Content-Length` — so no resume, no progress bar. The claimed capability isn't delivered unless the incoming `Range` header is forwarded and `206` returned. |
| F8 | 🟠 Med | Consistency | Write order is objects → pointer (good), but **KB ingests from S3 independently of Aurora**. If the pointer upsert fails after the `.md` is uploaded, KB serves content Aurora doesn't know about ("object exists / pointer stale") — the inverse of the §8 alarm, and uncaught. |
| F9 | 🟠 Med | KB sync orchestration | Bedrock allows **one ingestion job per data source at a time**; a daily overlap throws `ConflictException` (unhandled). A 15-min Lambda also **cannot poll a long ingestion to completion** (§8 asks it to). This needs Step Functions / an EventBridge-driven poller, as `prompt.md` already models. |
| F10 | 🟡 Low | Access control | "Existing auth check" is **authentication, not authorization**. `doc_id` in the URL is enumerable → IDOR: any VPN user can pull any document. Needs per-doc/tenant authorization even inside the VPN. |
| F11 | 🟡 Low | PDF toolchain | Markdown→PDF that renders well needs a heavy engine (WeasyPrint native deps / headless Chromium). §9 rejects this in Next.js for exactly these reasons but puts it in the Lambda without noting the **container-image packaging, memory, `/tmp`, and 50 MB KB limit** implications. |
| F12 | 🟡 Low | Cache vs streaming | The §6 in-process LRU "cache the bytes" contradicts §4's "don't buffer the object in memory." Caching whole PDFs in each ECS task reintroduces the memory pressure streaming was chosen to avoid. Cache the **pointer**, not the bytes. |
| F13 | 🟡 Low | Schema | `content_assets` lacks `filename` / `content_type` / `content_length` (needed for `Content-Disposition` and `Content-Length`) and `is_current` / `superseded_at` (needed for F1/F3 cleanup). |
| F14 | 🟡 Low | Preview hardening | §4 correctly flags sanitisation, but also: return the `.md` with `Content-Type: text/markdown` + `X-Content-Type-Options: nosniff`, and note react-markdown only renders raw HTML if `rehype-raw` is added — so **the rule is "don't add `rehype-raw`,"** plus keep the default URL-scheme filtering. |

**What the design gets right** (keep): Gateway endpoint over Interface for S3;
Aurora as pointer store not blob store; separate ingestion/delivery IAM roles;
`.md`-only inclusion prefix so the KB never pays PDF parser cost; streaming
instead of full buffering; rejecting CloudFront/presigned URLs for a private
deployment; the "pointer exists / object missing" drift alarm.

---

## 2. Detailed findings & recommended fixes

### F1 — Superseded Markdown stays in the KB inclusion prefix 🔴
`kb-content/<doc_id>/<hash>.md` is content-addressed, so an update *adds* a new
object and leaves the old one. Both are under `inclusionPrefixes: ["kb-content/"]`,
so the next sync embeds **both** → the KB retrieves stale chunks and can cite a
superseded version. Fix (pick one, first preferred):

1. **Delete the previous `<hash>.md` + `.metadata.json` on supersession**, then
   run the sync. Bedrock detects the deletion on the next sync and drops those
   chunks. Keep the *PDF* history if you want compliance versioning — PDFs are
   under `pdf-exports/`, which is not in scope for the KB.
2. Or write `is_current: true|false` into the sidecar and apply a
   **metadata filter** (`is_current = true`) at retrieval. Cheaper writes, but
   stale chunks still occupy the index and cost embedding spend.

Do **not** rely on the 30–90 day lifecycle to remove them — that leaves a month
of stale retrievals.

### F2 — In-VPC Lambda can't reach the external API 🔴
The "nothing public / no NAT" stance is right for the *AWS* hops but the source
API is third-party public internet. Options: (a) a **NAT Gateway** with egress
restricted to the API's domain via a firewall/proxy; (b) an **egress proxy**
endpoint if the API is exposed via PrivateLink (rare for third parties); (c) if
the API is on AWS behind PrivateLink, an interface endpoint. State the choice
explicitly — today §3 is internally contradictory.

### F3 — "Expire non-referenced objects" isn't a lifecycle capability 🔴
Lifecycle acts on age/prefix/**tag**, never on "is Aurora still pointing here."
Fix: at supersession, **tag the old object** `state=superseded` and scope the
lifecycle expiry to `tag:state=superseded`. The current object is never tagged,
so it never expires regardless of age. (Combine with F1's delete for the `.md`;
use tag-expiry for the `pdf-exports/` history you want to age out.)

### F4 — Immutable caching needs a content-addressed URL 🔴
Put the hash in the browser-facing URL: `GET /documents/{doc_id}/pdf?v={hash}`
(or `/documents/{doc_id}/{hash}/pdf`). Then `Cache-Control: private, max-age=...,
immutable` is correct — a new version yields a new URL, old URL is simply never
requested again. Also change `public` → **`private`**: these are
per-user-authenticated responses; `public` invites shared/intermediary caches to
store sensitive bytes.

### F5 / F6 — KB service role, KMS, and a VPC-pinned bucket policy 🟠
Add the KB data-source service role (`s3:GetObject` + `s3:ListBucket` on
`kb-content/*`, `kms:Decrypt`). Encrypt the bucket with a **customer-managed KMS
key**; grant `kms:Decrypt` (delivery, KB) and `kms:GenerateDataKey` (ingestion).
Add a bucket policy that **denies** any access whose `aws:SourceVpce` isn't the
gateway endpoint and denies `aws:SecureTransport = false`.

### F7 — Actually support Range 🟠
Forward the request's `Range` header to `GetObject`, return `206` with
`Content-Range`/`Accept-Ranges`, and set `Content-Length` from S3's
`ContentLength` on full responses. See the FastAPI pseudo-code.

### F8 / F9 — Make ingestion → sync transactional-ish and orchestrated 🟠
Only trigger the KB sync for documents whose **pointer upsert committed**. Prefer
the `prompt.md` model: **Step Functions** fans out per-batch workers, then a
single "start sync + poll to completion" tail with `ConflictException` retry —
instead of one Lambda that fires-and-forgets and can't wait 15 min+.

---

## 3. Pseudo-code — related components only

> Pseudo-code: illustrative, not a runnable project. Elisions marked `...`.
> Style follows the repo (`from __future__ import annotations`, dataclasses,
> module-level boto3 clients, env-driven config).

### 3.1 Infra SDK — AWS CDK (Python 3)

```python
# infra/stacks/rag_delivery_stack.py  (pseudo-code)
from __future__ import annotations

from aws_cdk import (
    Duration, RemovalPolicy, Stack,
    aws_ec2 as ec2, aws_s3 as s3, aws_kms as kms, aws_iam as iam,
    aws_lambda as lambda_, aws_events as events, aws_events_targets as targets,
    aws_cloudwatch as cw, aws_cloudwatch_actions as cw_actions, aws_sns as sns,
)
from constructs import Construct


class RagDeliveryStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, vpc: ec2.IVpc, **kw) -> None:
        super().__init__(scope, cid, **kw)

        # --- KMS: one CMK for the content bucket (F6) ---
        key = kms.Key(self, "ContentKey", enable_key_rotation=True,
                      removal_policy=RemovalPolicy.RETAIN)

        # --- S3: private, KMS-encrypted, versioned; TLS + VPCe enforced (F6) ---
        bucket = s3.Bucket(
            self, "ContentBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS, encryption_key=key,
            enforce_ssl=True,                      # denies aws:SecureTransport=false
            versioned=True,
            lifecycle_rules=[
                # F3: only SUPERSEDED-tagged objects expire; current is untagged.
                s3.LifecycleRule(
                    id="expire-superseded",
                    tag_filters={"state": "superseded"},
                    expiration=Duration.days(60),
                ),
                s3.LifecycleRule(  # tidy incomplete multipart uploads
                    abort_incomplete_multipart_upload_after=Duration.days(7)),
            ],
        )

        # S3 Gateway Endpoint (free, private) — reuse if it already exists (§10).
        vpc.add_gateway_endpoint("S3Gw", service=ec2.GatewayVpcEndpointAwsService.S3)

        # Pin bucket access to the VPC endpoint (F6). Deny everything not via VPCe.
        bucket.add_to_resource_policy(iam.PolicyStatement(
            effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["s3:*"], resources=[bucket.bucket_arn, bucket.arn_for_objects("*")],
            conditions={"StringNotEquals": {"aws:SourceVpce": "<s3-gateway-vpce-id>"}},
        ))

        # --- IAM: three least-privilege roles (F5, §7) ---
        kb_content = bucket.arn_for_objects("kb-content/*")
        pdf_exports = bucket.arn_for_objects("pdf-exports/*")

        ingestion_role = iam.Role(self, "IngestionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        ingestion_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:PutObjectTagging",  # tagging for F3
                     "s3:DeleteObject"],                     # delete superseded .md (F1)
            resources=[kb_content, pdf_exports]))
        ingestion_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"],
            resources=["<kb-data-source-arn>"]))
        key.grant_encrypt_decrypt(ingestion_role)            # GenerateDataKey to write

        delivery_role = iam.Role(self, "DeliveryTaskRole",  # assumed by the ECS task
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        delivery_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[kb_content, pdf_exports]))  # NO PutObject
        key.grant_decrypt(delivery_role)

        kb_service_role = iam.Role(self, "KbServiceRole",   # F5: the KB's own role
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
        kb_service_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[bucket.bucket_arn, kb_content]))       # kb-content/ only
        key.grant_decrypt(kb_service_role)

        # --- Ingestion Lambda: container image (heavy PDF renderer, F11) ---
        # F2: attach a NAT/egress path for the external API; interface endpoints
        # for secrets/kms/logs/bedrock so only the third-party call leaves the VPC.
        fn = lambda_.DockerImageFunction(
            self, "IngestionFn",
            code=lambda_.DockerImageCode.from_image_asset("lambda/ingestion"),
            role=ingestion_role, timeout=Duration.minutes(15), memory_size=2048,
            vpc=vpc, vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),  # F2
            environment={"BUCKET": bucket.bucket_name, "KB_ID": "<kb-id>",
                         "DATA_SOURCE_ID": "<ds-id>", "AURORA_SECRET_ARN": "..."})

        events.Rule(self, "Daily",                # EventBridge daily schedule (§3)
            schedule=events.Schedule.cron(hour="3", minute="0"),
            targets=[targets.LambdaFunction(fn, retry_attempts=2)])

        # --- Observability (F9, §8) ---
        topic = sns.Topic(self, "Alerts")
        for alarm in [
            fn.metric_errors().create_alarm(self, "IngestErrors", threshold=1,
                                            evaluation_periods=1),
            # "no successful run in >25h" — daily-workflow-missed (prompt.md).
            fn.metric_invocations(period=Duration.hours(25)).create_alarm(
                self, "IngestMissed", threshold=1,
                comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1, treat_missing_data=cw.TreatMissingData.BREACHING),
        ]:
            alarm.add_alarm_action(cw_actions.SnsAction(topic))
        # Plus custom-metric alarms emitted by the app: PdfConversionFailed,
        # KbIngestionFailed, PointerObjectMissing (F8/§8).
```

### 3.2 Ingestion Lambda

```python
# lambda/ingestion/handler.py  (pseudo-code)
from __future__ import annotations
import hashlib, json, os, boto3
from botocore.config import Config

_RETRY = Config(retries={"max_attempts": 5, "mode": "adaptive"})  # 5xx/throttle backoff
s3 = boto3.client("s3", config=_RETRY)
bedrock = boto3.client("bedrock-agent", config=_RETRY)
BUCKET = os.environ["BUCKET"]


def _content_hash(md: str) -> str:
    # Normalise (line endings, trailing ws) so cosmetic diffs don't rev the hash.
    return hashlib.sha256(md.strip().encode("utf-8")).hexdigest()[:16]


def handler(event, _ctx) -> dict:
    token = fetch_bearer_token()             # F2: external API over NAT/egress
    items = search_hateoas(token)            # 200+ links; batch upstream via Step Fns
    changed, failed = [], []

    for item in items:
        try:
            # Pre-download change check when the API exposes version/etag/last-modified
            # (prompt.md "optional optimisation") — else compare after fetch.
            md = to_markdown(fetch_item(item, token))
            h = _content_hash(md)
            prev = get_pointer(item.doc_id)          # Aurora read (indexed, tiny)
            if prev and prev.content_hash == h:
                continue                              # unchanged → skip (§3.4)

            base = f"{item.doc_id}/{h}"
            md_key = f"kb-content/{base}.md"
            # 1) Write objects FIRST, pointer LAST (F8 ordering).
            s3.put_object(Bucket=BUCKET, Key=md_key, Body=md.encode(),
                          ContentType="text/markdown",
                          # F4: private, and immutable is safe only because the
                          # delivery URL carries the hash.
                          CacheControl="private, max-age=86400, immutable")
            s3.put_object(Bucket=BUCKET, Key=f"{md_key}.metadata.json",
                          Body=sidecar(item, h))      # is_current etc. (aws_kb.md)

            try:                                      # F11: heavy renderer; isolate
                pdf = markdown_to_pdf(md)             # container image w/ engine
                s3.put_object(Bucket=BUCKET, Key=f"pdf-exports/{base}.pdf",
                              Body=pdf, ContentType="application/pdf",
                              CacheControl="private, max-age=86400, immutable")
            except Exception as e:                    # PDF failure ≠ KB failure (§3)
                emit_metric("PdfConversionFailed", 1); log.warning("pdf %s: %s", base, e)

            # F1/F3: retire the previous version so the KB drops its chunks.
            if prev:
                old = f"kb-content/{prev.doc_id}/{prev.content_hash}"
                s3.delete_object(Bucket=BUCKET, Key=f"{old}.md")
                s3.delete_object(Bucket=BUCKET, Key=f"{old}.md.metadata.json")
                s3.put_object_tagging(Bucket=BUCKET, Key=f"pdf-exports/{old}.pdf",
                    Tagging={"TagSet": [{"Key": "state", "Value": "superseded"}]})

            # 2) Commit pointer only after objects are durable (F8).
            upsert_pointer(item.doc_id, h, md_key, f"pdf-exports/{base}.pdf",
                           filename=item.title, content_type="application/pdf")
            changed.append(item.doc_id)
        except Exception as e:                        # partial failure → continue (§3)
            failed.append({"doc_id": item.doc_id, "error": str(e)})

    # F9: single sync at the end, only if something changed; handle the
    # one-job-at-a-time conflict. Completion polling belongs in Step Functions,
    # not this 15-min Lambda — return the jobId for the state machine to wait on.
    job_id = None
    if changed:
        try:
            job_id = bedrock.start_ingestion_job(
                knowledgeBaseId=os.environ["KB_ID"],
                dataSourceId=os.environ["DATA_SOURCE_ID"])["ingestionJob"]["ingestionJobId"]
        except bedrock.exceptions.ConflictException:
            emit_metric("KbIngestionConflict", 1)     # a job is already running
    return {"changed": len(changed), "failed": failed, "jobId": job_id}
```

### 3.3 FastAPI delivery

```python
# app/routes/documents.py  (pseudo-code)
from __future__ import annotations
import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

router = APIRouter()
s3 = boto3.client("s3")                      # thread-safe, reuse (see Plan/kb.py)
BUCKET = settings.bucket


def _stream(body, chunk=1 << 16):            # 64 KiB chunks; never buffer whole obj
    try:
        for part in body.iter_chunks(chunk_size=chunk):
            yield part
    finally:
        body.close()                         # release the connection on disconnect


async def _serve(request: Request, key: str, want_hash: str,
                 disposition: str, actor) -> Response:
    row = await pointers.get(key_doc_id(key))            # Aurora: pointer only (F12)
    if row is None:
        raise HTTPException(404, "unknown document")
    authorize(actor, row)                                # F10: per-doc authz, not just authn
    if want_hash and want_hash != row.content_hash:      # F4: stale URL → not immutable
        raise HTTPException(404, "stale version")

    get_kwargs = {"Bucket": BUCKET, "Key": key}
    rng = request.headers.get("range")                   # F7: honour Range → 206
    if rng:
        get_kwargs["Range"] = rng
    try:
        obj = s3.get_object(**get_kwargs)
    except s3.exceptions.NoSuchKey:
        emit_metric("PointerObjectMissing", 1)           # §8 drift alarm (F8)
        raise HTTPException(404, "object missing")

    headers = {
        "Content-Disposition": f'{disposition}; filename="{row.filename}"',
        "Cache-Control": "private, max-age=86400, immutable",   # F4
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",                    # F14
        "ETag": obj["ETag"],
    }
    status = 200
    if "ContentRange" in obj:                            # partial content (F7)
        headers["Content-Range"], status = obj["ContentRange"], 206
    else:
        headers["Content-Length"] = str(obj["ContentLength"])
    return StreamingResponse(_stream(obj["Body"]), status_code=status,
                             media_type=obj["ContentType"], headers=headers)


@router.get("/documents/{doc_id}/pdf")
async def download_pdf(doc_id: str, request: Request, v: str = "",
                       actor=Depends(current_user)):     # authn dependency
    row = await pointers.get(doc_id)
    if row is None:
        raise HTTPException(404, "unknown document")
    return await _serve(request, row.s3_key_pdf, v, "attachment", actor)


@router.get("/documents/{doc_id}/preview")               # Markdown preview (§4)
async def preview_md(doc_id: str, request: Request, v: str = "",
                     actor=Depends(current_user)):
    row = await pointers.get(doc_id)
    if row is None:
        raise HTTPException(404, "unknown document")
    # Returned as text/markdown; the client sanitises before render (F14).
    return await _serve(request, row.s3_key_markdown, v, "inline", actor)
```

### 3.4 Next.js (App Router)

```tsx
// app/documents/[docId]/PreviewModal.tsx  (pseudo-code)
'use client';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSanitize from 'rehype-sanitize';   // F14: sanitise external content

export function PreviewModal({ docId, version }: { docId: string; version: string }) {
  const [md, setMd] = useState('');
  useEffect(() => {
    // Same-origin call to FastAPI; cookie/session auth flows automatically.
    // Version in the query makes the response immutably cacheable (F4).
    fetch(`/api/documents/${docId}/preview?v=${version}`, { credentials: 'include' })
      .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(setMd)
      .catch(() => setMd('_Preview unavailable._'));
  }, [docId, version]);

  return (
    <div role="dialog" aria-modal="true">
      {/* No rehypeRaw ⇒ raw HTML is dropped; rehypeSanitize also filters
          javascript: URLs in links/images. This is the stored-XSS guard (§4/F14). */}
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>
        {md}
      </ReactMarkdown>
    </div>
  );
}

// Download: a plain anchor to the hash-versioned URL. The browser streams it,
// honours Range/resume (F7), and caches immutably (F4). No JS blob buffering.
export function DownloadLink({ docId, version }: { docId: string; version: string }) {
  return <a href={`/api/documents/${docId}/pdf?v=${version}`} download>Download PDF</a>;
}
```

---

## 4. Suggested additions to the design's "Open Items" (§10)

- [ ] **F1/F3:** delete superseded `.md`/sidecar on update **and** tag old PDFs
      `state=superseded`; scope the lifecycle rule to that tag, not raw age.
- [ ] **F2:** decide the egress path for the external HATEOAS API (NAT +
      domain-restricted firewall, or PrivateLink if available) — the in-VPC
      Lambda cannot reach it otherwise.
- [ ] **F4:** put `content_hash` in the browser-facing download/preview URL;
      switch `Cache-Control` from `public` to `private`.
- [ ] **F5/F6:** add the KB service role, a customer-managed KMS key, and a
      bucket policy pinning access to the S3 gateway endpoint + denying non-TLS.
- [ ] **F7:** implement `Range`→`206` pass-through and set `Content-Length`.
- [ ] **F9:** move KB-sync start + completion polling into Step Functions with
      `ConflictException` handling (aligns with `prompt.md`).
- [ ] **F10:** define the per-document authorization rule (tenant/ACL), not just
      authentication.
- [ ] **F13:** extend `content_assets` with `filename`, `content_type`,
      `content_length`, `is_current`, `superseded_at`.
