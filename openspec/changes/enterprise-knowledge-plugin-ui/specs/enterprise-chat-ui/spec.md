# Spec Delta

## Purpose

让员工在聊天输入区明确选择企业知识范围和检索模式，保留会话与专家之间的状态隔离，并通过可理解、可撤销且原样保存的确认步骤控制公开查询词，避免权限和默认值在页面切换时悄然扩大。

## ADDED Requirements

### Requirement: Scoped session selection
New sessions SHALL initialize from expert defaults restricted by current authorization; empty selections MUST block knowledge queries. Explicitly clearing a selection MUST persist without automatic refill. Switching users, agents or threads MUST load only that context's server state.

#### Scenario: Explicit clear followed by rerender
- **WHEN** an employee clears all selected knowledge bases and the composer rerenders
- **THEN** the empty selection remains and KB_ONLY/HYBRID sending is blocked with a localized reason

#### Scenario: Context switches during loading
- **WHEN** an old thread's resource response arrives after an expert/thread switch
- **THEN** it cannot replace current options, selection or query approval

### Requirement: New session and saved empty selection are distinguishable
session.get MUST return selection_present, revision and configuration revisions. Only selection_present=false permits initializing the current authorized expert defaults through revision-zero session.save. A saved empty array MUST return selection_present=true. Initialization MUST use a consistent server authorization snapshot and reject changed source revisions.

#### Scenario: Concurrent initialization
- **WHEN** two tabs initialize a previously absent session selection
- **THEN** only one revision-zero save commits; the loser reloads saved state without reapplying defaults

#### Scenario: Saved empty selection
- **WHEN** session.get returns selection_present=true and selected_kb_ids=[]
- **THEN** the UI preserves the empty array regardless of later expert default changes

### Requirement: Independent three-mode control
The plugin mode selector MUST show KB_ONLY by default and gated WEB_ONLY/HYBRID options while retaining ordinary ask/plan/craft controls. Missing web requirements MUST show a reason; WEB_ONLY MUST neither require selected knowledge bases nor trigger WeKnora readiness calls.

#### Scenario: Web mode unavailable
- **WHEN** permission or source configuration is lost
- **THEN** the stored mode remains visible but submission is blocked until the user resolves the issue or explicitly selects another valid mode

### Requirement: Exact query confirmation workflow
Before web submission the UI SHALL show the exact public query and explicit confirm/cancel actions. Approval MUST bind to current user/thread/expert/operation/revisions; editing mode, scope, context or query invalidates it. Display and submission MUST preserve whitespace and characters.

#### Scenario: User cancels confirmation
- **WHEN** the user cancels the query confirmation dialog
- **THEN** no web request starts and focus returns to the trigger

#### Scenario: Double submit
- **WHEN** the user double-clicks send with one approval
- **THEN** the UI holds a single pending submission and the backend consumes approval at most once

### Requirement: Immutable per-turn extension context
Plugin execution context MUST remain separate from display metadata and immutable per send or queue item. Native thread resolution MUST precede plugin session and operation preparation; failed preparation MUST retain drafts. Edits or context changes MUST NOT reuse stale approvals. Busy bound input MUST use a separate next turn, and HITL MUST restore the backend original context.

#### Scenario: First enterprise message has no native thread
- **WHEN** an employee submits the first message before a native thread exists
- **THEN** the host resolves the native thread, initializes that thread under the current context generation, prepares its operation and only then submits; failure retains the draft and a late result cannot switch another active context

#### Scenario: Queued message outlives a context change
- **WHEN** a queued message is flushed or reclaimed after composer context changes
- **THEN** its own copied context is revalidated and invalid approval requires explicit preparation again; current surface state cannot replace it

#### Scenario: Enterprise addition arrives while a turn is active
- **WHEN** a bound enterprise message is submitted while the previous turn is active
- **THEN** it waits as a separate next-turn item or is visibly rejected without losing the item, and no mid-turn steer consumes or discards its distinct operation context

#### Scenario: Edit and resend or HITL continuation
- **WHEN** a message is edited for resend or an existing HITL decision is resumed
- **THEN** the edit prepares a new operation and approval as needed, while HITL uses the backend original context; neither path invents missing execution input from display history metadata

### Requirement: Revision-bound local operation preparation
Submission SHALL call operation.prepare with version 1 and expected_session_revision in the resolved agent/thread context. The UI MUST use the server-returned operation ID and MUST NOT supply a question, actor, scope or operation ID in the preparation payload. Preparation MUST remain local without source HTTP. Each pending submission SHALL retain its own immutable text and attachment snapshot without overwriting newer drafts.

#### Scenario: Session changes before preparation
- **WHEN** the displayed session revision differs from the server during preparation, including revision-zero creation races
- **THEN** submission stops, retains the draft and reloads current state for explicit retry without silently widening selection

#### Scenario: User edits while preparation is pending
- **WHEN** the user edits the draft within the same context generation while an earlier snapshot is preparing
- **THEN** success may submit that earlier snapshot but cannot clear newer text or attachments, and failure cannot restore the older snapshot over the newer draft

### Requirement: Keyboard accessible localized composer
Selection and mode controls MUST have localized accessible names and support normal keyboard selection. Confirmation MUST initially focus Cancel, contain Tab focus, support Escape cancel and restore trigger focus. Disabled sends MUST expose a localized reason; pending sends MUST block click and keyboard duplicates. All composer state/error copy MUST support en/zh; displayed times MUST use server timezone.

#### Scenario: Keyboard confirmation cancellation
- **WHEN** a keyboard user opens confirmation, cycles Tab and presses Escape
- **THEN** focus remains within the dialog until cancellation, no query is approved and focus returns to its trigger

#### Scenario: Pending keyboard send
- **WHEN** the user invokes the send shortcut while a submission is pending
- **THEN** no extra request starts and the localized pending state is announced

### Requirement: Lifecycle state and fallback
The composer MUST show loading, unavailable, denied and configuration-conflict states without replacing server authority. Disabling/unloading a required plugin MUST remove active controls and visibly block bound sends. Text-only tool fallback MUST NOT count as completed UI acceptance.

#### Scenario: Required plugin disappears
- **WHEN** a required plugin is disabled while the composer is open
- **THEN** controls are cleaned up, stale approvals are unusable and the user sees why enterprise submission is blocked

### Requirement: Generic readiness survives plugin removal
The builtin chat shell MUST read authenticated generic requirement readiness without enterprise IDs. It SHALL render the blocker independently of unloaded plugin code, reconcile disabled or removed modules, and refresh before submission and on focus or lifecycle changes. Visible sessions SHALL refresh within 30 seconds. Read errors MUST NOT imply an unbound ready state; final backend admission remains authoritative.

#### Scenario: Shared employee opens the composer
- **WHEN** an employee accesses a shared Agent with their own thread
- **THEN** readiness uses the generic authenticated read contract without requiring Agent-owner settings permission or leaking another employee's requirements

#### Scenario: Plugin is removed from another session
- **WHEN** another administrator disables or removes a required plugin
- **THEN** the visible chat reconciles within 30 seconds and removes its controls; a submission refreshes readiness before sending and explains any blocker

#### Scenario: First send obtains its native thread
- **WHEN** the composer has no persisted thread yet
- **THEN** it checks Agent-scope readiness, creates the real native thread, and checks that thread before plugin preparation; no pending identifier is sent as a real thread
