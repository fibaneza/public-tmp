The user is asking about AgentCore Memory latency when loading conversation history — this is squarely in the AgentCore skill's domain. Let me pull in that expertise before answering.

Ran skill
/aws-dev-toolkit:agentcore
I have the AgentCore platform context. The core of your problem is almost certainly which Memory path you're reading through — let me confirm the API surface against current AWS docs before I give you specifics.


Used 3 tools
Good — the docs confirm the shape of the problem. Here's what's almost certainly going on and how to fix it.

The core issue: you're likely reading through the wrong Memory path
AgentCore Memory has two access paths with very different latency profiles, and loading a transcript for display should use the fast one:

Path	API	What it's for	Latency
Short-term / raw events	ListEvents	Reconstructing conversation history verbatim	Synchronous direct read — fast, paginated
Long-term / semantic	retrieve_memory_records / retrieve_memories	Giving the agent relevant context via vector search	Embedding + vector search — much slower, and returns extracted insights, not the actual turns
AWS's own Well-Architected Agentic AI Lens names this exactly: "The root cause of poor memory performance is typically an access-pattern mismatch. Using a single storage layer for both sub-millisecond session reads and large-scale semantic searches forces one pattern to carry cost and latency characteristics suited to the other." (AGENTPERF03-BP01)

So the two big mistakes that cause "big latency to load the conversation" are:

Using semantic retrieval (retrieve_memories) to render the chat. Switch to ListEvents — it's literally the API designed "for applications that need to reconstruct conversation histories."
Re-invoking the Strands agent on Runtime just to load history. If clicking a session calls invoke_agent_runtime, you're paying container cold-start + framework init + model-client init (seconds) to do a read. Loading a transcript should call the bedrock-agentcore data-plane ListEvents directly from your UI backend, bypassing Runtime entirely. Only invoke the agent when the user sends a new message.
Ranked levers
1. Right API, called directly (biggest win) — ListEvents from your backend, not semantic retrieval, not through the agent.

2. Paginate / lazy-load. ListEvents supports maxResults + nextToken and includePayloads. Load the last ~20–30 turns, render, then lazy-load older turns on scroll-up. Don't pull an entire long conversation in one shot.

3. Kill the N+1 on the session list. If your session list calls list_sessions and then ListEvents per session to get a title/preview, that's N round-trips. Keep a lightweight session index (title, last-message preview, updatedAt) in DynamoDB written at first-event time, so the list is one query. Also pass filter={'eventFilter': 'HAS_EVENTS'} to list_sessions to skip empties, and paginate (default is only 20).

4. Add a read tier (CQRS). Past turns are immutable, so they cache perfectly. Dual-write each turn into a fast read store — DynamoDB (single-digit ms) or ElastiCache/Valkey (sub-ms) keyed by sessionId — and have the UI read that projection. AgentCore Memory stays the system of record and the source for long-term extraction. This is the tiered-memory pattern the WA lens recommends.

5. Trim round-trip overhead. Same region for backend + Memory; avoid Lambda cold starts on the read path (provisioned concurrency/SnapStart, or read DynamoDB directly); parallelize the session-list fetch with a prefetch of the most-recent session's first page; prefetch on hover.

To pinpoint your bottleneck
A few facts would let me tell you exactly which lever matters most:

How are you loading the chat today — ListEvents, retrieve_memories/retrieve_memory_records, or by invoking the agent on Runtime?
Where is the latency measured — the AWS API call itself, or end-to-end (including any Runtime invocation / frontend hop)?
How many turns are in a slow conversation, and are you loading all of them at once?

------
I have a aurora for session metadata, but not saving conversation. any recomendation

Yes — and you're in a good spot, because you already run Aurora. The cleanest fix is to make Aurora your conversation read model too: dual-write each turn into it, and read the transcript from Aurora instead of AgentCore Memory. That removes Memory from the display hot path entirely, and you avoid introducing a second datastore.

Why Aurora (not a new store)
You already have it, it's transactionally consistent with your session metadata, and a single indexed query returns a transcript in single-digit ms. AgentCore Memory stays the system of record + the source for long-term extraction — but the UI never waits on it. This is exactly the tiered/CQRS split AWS's Well-Architected Agentic AI Lens recommends; you're just adding the read tier you're currently missing.

Schema
CREATE TABLE messages (
  session_id   uuid        NOT NULL REFERENCES sessions(session_id),
  seq          bigint      NOT NULL,          -- monotonic turn order within session
  role         text        NOT NULL,          -- user | assistant | tool
  content      jsonb       NOT NULL,          -- text + any tool-call/attachment parts
  event_id     text        NOT NULL,          -- the AgentCore CreateEvent id
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, seq),
  UNIQUE (event_id)                           -- idempotency for retries/replays
);
The (session_id, seq) PK gives you fast ordered retrieval and keyset pagination for scroll-back. event_id UNIQUE makes the dual-write safe to retry.

Write path
At the same point you already call CreateEvent on AgentCore Memory, project the turn into Aurora:

event = memory.create_event(...)          # keep this — feeds LTM extraction + agent context
db.execute(
    "INSERT INTO messages (session_id, seq, role, content, event_id) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
    (session_id, seq, role, content, event["eventId"]),
)
# also bump denormalized fields on the session row:
db.execute("UPDATE sessions SET last_preview=%s, message_count=message_count+1, "
           "updated_at=now() WHERE session_id=%s", (preview, session_id))
Keep writing to Memory so long-term memory (semantic/summary/preference) still works. If a projection write ever fails, a small backfill job that reads ListEvents and upserts by event_id heals the gap.

Read path
Transcript: SELECT ... WHERE session_id=%s AND seq < %s ORDER BY seq DESC LIMIT 30 — keyset pagination, no OFFSET, no Memory call.
Session list: your existing Aurora query, now with last_preview and message_count denormalized onto the session row — zero joins, no N+1.
Ops tips
Serve transcript reads from the Aurora reader endpoint to keep them off the writer.
If your backend is Lambda, put RDS Proxy in front to avoid connection storms; if it's ECS/Fargate/long-running, a normal pool is fine.
On Aurora Serverless v2, set min ACU high enough that scale-up doesn't add cold latency to the first query.
Bonus: Aurora becomes your durable transcript archive, so you can set a short retention on Memory STM (just long enough for LTM extraction + the agent's context window) and trim that storage cost.
One fork worth confirming
The recommendation above assumes you're using AgentCore long-term memory (semantic/summary/preference strategies) and want to keep it. If you're only using short-term memory for conversation continuity and not using LTM, you could go further — make Aurora the primary transcript store, hydrate the Strands agent's context from Aurora, and drop AgentCore Memory from the write path entirely.

Which is it — are you using long-term memory strategies, or just short-term for continuity? That determines whether Memory stays in your write path or comes out of it, and I'll give you the exact target architecture for your case.
