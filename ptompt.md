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
If you paste the code path that runs when a session is clicked (or point me at it in the repo), I'll give you the concrete before/after — most of the time the fix is swapping a retrieve_* call or an agent-invoke for a direct paginated ListEvents plus a small DynamoDB read-model.
