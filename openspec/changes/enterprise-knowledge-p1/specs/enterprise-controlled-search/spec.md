# Spec Delta

## Purpose

为员工提供可明确选择的知识、联网和混合检索模式，使用服务端保存的用户原样批准词检索公开信息，限制主备搜索的外呼时间、次数和可携带内容，并防止工具旁路扩大访问范围。

## ADDED Requirements

### Requirement: Explicit search mode
KB_ONLY MUST be the default and make zero web requests. WEB_ONLY MUST make zero WeKnora requests, including probes. HYBRID MUST finish its internal search before any approved external search. Existing ask/plan/craft modes MUST remain independent.

#### Scenario: Web-only while knowledge source is down
- **WHEN** a permitted employee uses WEB_ONLY with valid web settings
- **THEN** no knowledge source readiness check or request is made

### Requirement: Exact public query approval
Web requests MUST use the exact user-approved public_query held only in memory for the current operation, without trim, rewriting or enrichment. Persistent records MUST contain only its digest plus approval, claim, consumption, and request-count state. Approval MUST be bound to user/thread/expert/operation and session/search/configuration revisions, and its consumption MUST be atomic with the first durable request reservation. Restart MUST make the plaintext unavailable and MUST NOT revive or replay the approval. Internal evidence, history, attachments and customer identifiers MUST NOT be added to provider input.

#### Scenario: Query changed by one space
- **WHEN** the pending query changes after approval, including a whitespace-only change
- **THEN** the old approval is invalid and web search remains blocked pending fresh confirmation

### Requirement: Bounded primary-secondary fallback
Each source MUST have a 10-second deadline and the operation a 22-second deadline, including authorization, TLS/client construction, body parsing and result validation. Only timeout, HTTP 429 and 5xx may trigger one secondary attempt. Authentication denial, EMPTY, cancellation, permission denial, protocol failure and invalid schema MUST NOT trigger fallback. A single operation MUST durably reserve and start at most two requests across repeated or concurrent calls. Cleanup remains owned and drainable, but an unbounded client/TLS/parser drain MUST NOT delay fallback or reset either deadline.

#### Scenario: Primary unauthorized
- **WHEN** the primary source returns 401 or 403
- **THEN** the search fails without a secondary request

#### Scenario: Repeated tool call
- **WHEN** the model or concurrent worker requests web search twice for one operation
- **THEN** both callers share the same result or failure and no second fallback round starts

### Requirement: Permission and bypass enforcement
Web access MUST require current employee search permission, expert web_allowed and valid primary/secondary configuration. Actual tool execution MUST reject searchfree, legacy search/MCP and unverified browser/exec/delegation paths that bypass policy.

#### Scenario: Legacy tool selected
- **WHEN** a bound enterprise agent selects an unrestricted legacy search tool
- **THEN** execution is denied before provider contact

### Requirement: Finalization and cancellation
Cancelled, interrupted or superseded operations MUST reject late evidence/final-answer commits. Restart MUST invalidate pending approvals; retries MUST use a fresh operation and approval.

#### Scenario: Provider finishes after cancel
- **WHEN** a cancelled request returns a response
- **THEN** it does not create final evidence or an answer and no secondary request starts
