# Spec Delta

## Purpose

让已登录并获授权的员工通过插件使用远程 WeKnora 企业知识，获得能核对的真实证据、连续检索文本与受控原文，并在权限变化和远程响应异常时保持可解释且默认拒绝的行为。

## ADDED Requirements

### Requirement: Explicit read-only compatibility probe
The probe MUST require explicit live opt-in and complete environment inputs, make only read-only requests, validate LIST/SEARCH/READ response shapes, and produce sanitized evidence. Missing input MUST produce BLOCKED and zero HTTP requests. Synthetic tests MUST NOT establish remote compatibility.

#### Scenario: Missing deployment inputs
- **WHEN** no live flag or required URL/version/digest/key/test-resource input is configured
- **THEN** the probe reports only missing input names, exits nonzero, and makes zero HTTP requests

#### Scenario: Upstream schema drift
- **WHEN** a successful HTTP response has an invalid success flag or data type
- **THEN** the corresponding probe fails with a fixed sanitized schema error and never records response bodies or credentials

### Requirement: Explicit test transport exception
Remote HTTPS remains the default; an explicitly user-authorized test HTTP origin MAY be enabled only by exact normalized server-environment origin matching and MUST be reported as a test transport exception.

#### Scenario: Authorized test HTTP origin
- **WHEN** the configured non-loopback HTTP origin exactly matches the explicit server-side test origin
- **THEN** only that test destination is allowed, the report identifies the transport exception, and all other origins and redirects remain rejected

### Requirement: Five-way authorization and default denial
The effective scope MUST equal credential-visible ∩ enterprise-allowed ∩ employee-allowed ∩ expert-allowed ∩ session-selected IDs. Each call MUST recompute it; tool arguments may only shrink it. Missing grants and empty selections MUST deny knowledge access. Explicit out-of-scope IDs MUST reject the whole request.

#### Scenario: Mixed authorized and unauthorized IDs
- **WHEN** a tool requests one authorized and one unauthorized knowledge base
- **THEN** the entire operation is rejected with zero knowledge-search requests

#### Scenario: Employee catalog
- **WHEN** an employee lists available knowledge bases
- **THEN** no unauthorized name, description, count or ID is returned

### Requirement: Session selection snapshot consistency
The knowledge plugin MUST require the caller's observed connection identity and config, binding and grant revisions when saving a selection. It MUST reject stale snapshots without mutations and use matching expectations in the final atomic Host commit. Absent source records require explicit revision zero; omitted snapshot inputs are invalid.

#### Scenario: Caller snapshot is already stale before save starts
- **WHEN** any caller-observed connection identity or source revision differs from current sources
- **THEN** the plugin rejects the stale save rather than replacing caller expectations with newly read values

### Requirement: Bounded async read-only requests
Knowledge operations MUST have a 30-second request timeout, at most 10 requests and a 90-second overall deadline, without automatic retries or cross-host redirects. Search MUST return at most 8 snippets of at most 1,200 characters with explicit truncation and source fields.

#### Scenario: Request budget exhausted
- **WHEN** the operation has already started 10 requests
- **THEN** no eleventh request is started and the operation returns a budget failure

### Requirement: Revision-bound management probes
Connection catalog and probes MUST use the server-authenticated actor and an explicit connection identity and expected configuration revision. Administrators MAY inspect credential-visible catalog metadata and probe a saved disabled connection before activation. Employee catalog MUST retain the first four authorization intersections and reject disabled connections. Neither path may bypass recovery quarantine or use client-supplied credentials or URLs.

#### Scenario: Probe a pending disabled connection
- **WHEN** an administrator explicitly probes a saved disabled connection at its current revision
- **THEN** the backend performs only the requested read-only component without enabling the connection or granting employee access

#### Scenario: Old probe finishes after configuration changes
- **WHEN** a probe finishes after its observed configuration or recovery intent changed
- **THEN** its final atomic write is rejected and cannot update current readiness

#### Scenario: Incomplete probe evidence
- **WHEN** LIST, nonempty authorized SEARCH, and nonempty attributed READ have not all succeeded for the current configuration revision
- **THEN** the enabled connection is not READY; missing component inputs produce an explicit blocked result without requests for that component

### Requirement: Authorized continuous reading
Each read MUST return at most 8,000 characters, a continuation when incomplete, and a cursor bound to the actor/thread/document/source revision. Each continuation and original access MUST reauthorize. Source changes MUST invalidate unreliable cursors.

#### Scenario: Permission revoked between pages
- **WHEN** access is revoked after the first page but before continuation
- **THEN** continuation is denied before any new chunk or original request

#### Scenario: Single long chunk
- **WHEN** a document contains one 12,000-character chunk
- **THEN** two reads can recover all characters in order without silent loss or duplication

#### Scenario: Complete source validation exceeds the operation budget
- **WHEN** the backend cannot collect and validate the complete ordered chunk set within the shared request and time budgets
- **THEN** it returns an explicit failure without a successful partial page or an unreliable continuation

#### Scenario: Observed source content changes between reads
- **WHEN** the recomputed ordered content digest or validated pagination differs from the cursor snapshot
- **THEN** continuation is rejected and requires an explicit restart; the digest is not represented as an upstream document version or an atomic source snapshot

### Requirement: Prepared retrieval precedes answer generation
The plugin SHALL prepare an actor/thread/revision-bound operation locally without remote requests. A new graph invocation MUST claim a fresh operation and retrieve evidence before invoking the model. Empty, denied or failed retrieval MUST prevent unsupported model generation. Repeated search tools MUST NOT start another retrieval batch. COMPLETED SHALL mean successful agent graph completion, not delivery acknowledgment.

#### Scenario: Empty retrieval before the first model call
- **WHEN** the authorized initial retrieval returns no evidence
- **THEN** no model provider is invoked and the graph returns a localized empty-result explanation without invented citations

#### Scenario: Duplicate retrieval or old operation replay
- **WHEN** two graph invocations claim one prepared operation or a new turn supplies an already used operation
- **THEN** at most one invocation starts retrieval and the other is rejected; search tools in the admitted graph reuse registered evidence without another search batch

#### Scenario: Local preparation matches the current session
- **WHEN** an authenticated caller prepares an operation with a required expected session revision, including zero for an absent session
- **THEN** local nonempty scope, source and quarantine checks precede an atomic create, concurrent session changes conflict, and no upstream request occurs

#### Scenario: Retrieval result publication is atomic
- **WHEN** a claimed retrieval succeeds or returns no evidence
- **THEN** the operation status, bounded observed scope and any successful evidence are committed together with operation, source, session and quarantine revision checks; a conflict cannot publish a partial successful result

#### Scenario: Reusing a query observation
- **WHEN** the admitted graph lists its observed knowledge bases or searches its registered evidence again
- **THEN** fresh local authorization and pinned revisions are checked, optional ID narrowing cannot expand scope, and no replacement query or additional upstream batch is accepted

### Requirement: Owned read-only knowledge tools
The plugin SHALL register distinct SDK list/search/read functions and Host read-only capabilities. Invocations MUST use fresh execution identity and authorization. Discovery and execution SHALL recognize actual enabled callables and sanitized names, not names alone. Bound resume/continuation MUST remain denied until explicitly implemented.

#### Scenario: Another plugin uses the same name
- **WHEN** a foreign tool collides with a knowledge tool name or presents a different asynchronous implementation
- **THEN** it does not become an owned knowledge capability; the real renamed tool remains identifiable by its registered callable

#### Scenario: Search result reaches the evidence renderer
- **WHEN** the real cached-search tool returns current registered evidence
- **THEN** its controlled renderer envelope carries the operation and evidence IDs plus bounded source text/metadata, without actor, credential or connection authority fields; source content cannot create additional trusted references

#### Scenario: Continuation has no implemented business contract
- **WHEN** a bound execution uses resume or continuation before the explicit lifecycle contract is implemented
- **THEN** knowledge service methods deny it instead of treating it as a fresh turn

### Requirement: Evidence backed citations and original access
Evidence MUST use server-generated UUIDv4 IDs and the strict versioned schema octop.enterprise.knowledge.v1. Only registered evidence for the current actor/thread/operation may become a trusted citation. Original access MUST use authenticated proxy references; chunk numbers MUST NOT be presented as page numbers.

#### Scenario: Fabricated citation
- **WHEN** a model invents an evidence ID or arbitrary source URL
- **THEN** it is not accepted as a trusted citation

#### Scenario: Original unavailable
- **WHEN** the deployed source exposes no verified original-file capability
- **THEN** the UI explicitly marks it unavailable while authorized retrieved text remains readable

### Requirement: Policy admission and owned graph completion
The paired policy hook MUST preserve ordinary turns before resolving knowledge services. Bound turns MUST validate the current server input marker, retrieve before the external model, and permit only owned knowledge tools plus genuine SDK discovery. Completion MUST require this invocation's admitted operation receipt and successful real cached-search result. Refused replays MUST NOT terminate another invocation's operation.

#### Scenario: Ordinary invocation through the global hook
- **WHEN** a valid invocation has no enterprise requirements, or has unrelated requirements only
- **THEN** the policy performs no knowledge service resolution or source request and preserves ordinary model and tool behavior

#### Scenario: Invalid current input marker
- **WHEN** the bound turn lacks a current Human marker with exact integer version one and nonblank string text
- **THEN** it returns a localized refusal before source or external model calls, without using history, attachments or rendered placeholders as the question

#### Scenario: Native deferred discovery and cached evidence
- **WHEN** default SDK middleware exposes deferred knowledge tools
- **THEN** a local deterministic model delegates through the public handler to initialize discovery and execute the real owned cached search; the original model subsequently receives only the permitted tool inventory

#### Scenario: Refusal reaches the graph exit hook
- **WHEN** a new invocation is denied or empty and native graph routing still invokes after-agent hooks
- **THEN** its admitted receipt is cleared and no previously admitted operation is completed or failed by that refusal

#### Scenario: Tool failure before answer generation
- **WHEN** an admitted invocation sees a current tool error or loses source authorization before the next model call
- **THEN** it cannot invoke the external model or become COMPLETED; any permitted terminal update affects only that admitted operation
