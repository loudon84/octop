# Spec Delta

## Purpose

为受信任的 Octop 插件提供统一的身份、持久记录、密文凭据、动作分发和生命周期能力，让插件能独立升级业务，同时限制宿主修改范围并保证普通插件与会话兼容。

## ADDED Requirements

### Requirement: Trusted actor and namespace isolation
The host MUST derive actor identity from server authentication and bind each plugin facade to exactly one plugin namespace. Client payload MUST NOT replace user identity or access another plugin's records or credentials.

#### Scenario: Forged actor or foreign record
- **WHEN** a client supplies a different user ID or a plugin requests another plugin's credential reference
- **THEN** the host rejects the action before a secret or record is returned

### Requirement: Atomic revision checked storage
The host SHALL provide record creation and revision checked updates plus transactions across records and credential activation. A stale update MUST leave all records and active credential references unchanged. A transaction SHALL also accept plugin-scoped read-only revision preconditions, including revision zero for absence, and validate them atomically with its mutations without changing the checked records.

#### Scenario: Concurrent writers
- **WHEN** two writers update the same record from the same revision
- **THEN** exactly one commits and the other receives a conflict with no partial effects

#### Scenario: Failed transaction
- **WHEN** the second mutation in a multi-record transaction fails
- **THEN** the first mutation and credential activation are rolled back

#### Scenario: Authorization source changes before session save
- **WHEN** a caller saves a record based on source record revisions and any source changed or an expected absent source was created before the transaction acquires its modification boundary
- **THEN** the transaction returns conflict and leaves destination records, required-extension declarations and credential activation unchanged

#### Scenario: Source is stable throughout conditional commit
- **WHEN** all read-only source revision preconditions match
- **THEN** concurrent source writers cannot commit between validation and the destination commit, and source revisions and timestamps remain unchanged

### Requirement: Encrypted staged credentials
Credentials MUST be encrypted at rest, identified by opaque references, and excluded from ordinary tool configuration, browser storage, responses and logs. A staged credential MUST NOT become usable until the associated configuration commits.

#### Scenario: Save fails after staging
- **WHEN** a credential is staged and the configuration revision check fails
- **THEN** the old active reference remains usable and the new staged credential is released or held unusable for explicit cleanup

#### Scenario: Recovery reference is persisted before staging
- **WHEN** a trusted plugin supplies a preallocated opaque UUIDv4 credential ID after persisting its recovery intent
- **THEN** staging uses that exact ID without replacing any existing credential, so failed later record updates or cleanup cannot make a successful stage unlocatable

### Requirement: Registered typed actions and resources
The host SHALL authenticate and dispatch only explicitly registered actions with documented input/output schemas and resource handlers. Plugins own business authorization; the host MUST NOT dispatch arbitrary Python names or unregistered paths.

#### Scenario: Unregistered action
- **WHEN** a caller requests an unknown action name
- **THEN** the host returns a typed not-found error without executing plugin code

### Requirement: Required extensions survive uninstall
Agent/thread required-extension bindings MUST persist across disable, unload, reload and uninstall. Missing or incompatible dependencies MUST reject new bound calls even when clients omit plugin context; old callbacks and in-flight late commits MUST become unusable.

#### Scenario: Policy disabled
- **WHEN** a required policy plugin is disabled while a bound thread exists
- **THEN** new calls are rejected without remote requests and existing data remains retained

#### Scenario: Business binding transaction fails
- **WHEN** an action attempts to save a plugin binding and its required-extension declaration but either revision check fails
- **THEN** neither record changes and no unguarded business binding is created

#### Scenario: Cached dependency is replaced
- **WHEN** a service proxy has been resolved and a required plugin is disabled or reloaded, including reload with the same version
- **THEN** the old proxy rejects subsequent calls before plugin service code executes

### Requirement: Lifecycle commits preserve asynchronous progress
Storage side effects MUST be serialized with plugin generation invalidation through the actual database commit. An invalidation that has completed MUST prevent old workers from committing later. Event-loop generation checks MUST NOT synchronously wait on a storage lease held during database or encryption work.

#### Scenario: Stop races with final commit
- **WHEN** a worker is paused at the final database commit while plugin invalidation is requested
- **THEN** the commit and invalidation have a defined order, already committed data remains retained, and no old worker commits after invalidation has completed

#### Scenario: Another plugin waits for storage
- **WHEN** a plugin storage worker is paused while holding its operation lease
- **THEN** event-loop tasks and another plugin's short state checks can still progress

### Requirement: Execution identity is bound to authenticated ingress
Trusted execution context MUST originate at a server-authenticated entry point and remain scoped to that application, actor, Agent and thread. Serialized plugin context, numeric channel subjects, request user strings and connection owners MUST NOT mint an employee identity. Common stream, call and resume boundaries MUST enforce persisted requirements independently of the policy hook.

#### Scenario: Client session key references another user's thread
- **WHEN** a caller supplies a session key already bound to another user's thread
- **THEN** the entry point rejects it before queue admission or Harness execution

#### Scenario: Bound parent delegates to an ordinary Agent
- **WHEN** an execution with required extensions attempts a cross-Agent child invocation without a supported trusted delegation contract
- **THEN** the child invocation is rejected even if the child has no own required extensions

#### Scenario: Busy bound ingress lacks a per-message extension context contract
- **WHEN** new busy input targets a required-extension-bound thread without a supported per-message extension context contract
- **THEN** the host rejects mid-turn steering rather than silently discarding new context or merging approvals; a separate authorized next turn and ordinary unbound steering retain their existing semantics

### Requirement: Shared registry mutation drains runtime work
Supported mutations of the process-wide plugin registry MUST reserve a single process mutation, publish stale state to affected runtime Hosts, preserve the actual storage commit ordering, and drain affected work on its owning event loops before replacing SDK registrations or plugin files. Cancellation of the requesting coroutine MUST NOT release the reservation while its worker is still mutating. A manager without a Host MUST NOT bypass other registered runtime Hosts.

#### Scenario: Cleanup needs the old plugin files
- **WHEN** an affected action or resource cleanup reads old plugin files or awaits executor work
- **THEN** mutation waits for that cleanup on its running owning loop before replacing the files, without blocking event-loop progress

#### Scenario: Mutation requester is cancelled
- **WHEN** a requester is cancelled while its mutation worker continues
- **THEN** other mutations remain excluded until the worker and required cleanup actually finish

#### Scenario: Initiator installs another plugin after conservative invalidation
- **WHEN** a successful explicit mutation invalidates all contexts and leaves other enabled plugins loaded in the initiating manager
- **THEN** that manager rebinds its actual current surviving contexts without reviving disabled or removed plugins; other Hosts and old graph snapshots remain stale

#### Scenario: An HTTP action belongs to a dependent provider
- **WHEN** a plugin required by a provider service is invalidated while that provider has an active action
- **THEN** the dependent old action is invalidated and drained before registry or file mutation; conservative invalidation of all plugin generations is permitted

#### Scenario: Startup upgrades an installed bundled plugin
- **WHEN** startup seeding replaces an installed plugin directory while runtime Hosts are registered
- **THEN** it uses the same mutation reservation and drains affected work before file replacement; standalone initialization without runtime Hosts retains its synchronous behavior

### Requirement: Required extension admission proves actual graph mounts
Required-extension admission MUST validate the actual successfully built Agent graph, its effective plugin configuration, loaded context identities and a current registry epoch. Global plugin ID and version presence MUST NOT substitute for a required hook mounted in that graph. Snapshot validation and execution tracking MUST be atomic with short invalidation publication and MUST NOT wait for long storage or mutation locks on the event loop.

#### Scenario: Required hook is disabled only for an Agent
- **WHEN** the required plugin is globally active but its hook is excluded from that Agent graph
- **THEN** stream, call and resume reject before Harness execution and resume does not consume pending input

#### Scenario: Same-version plugin context is replaced
- **WHEN** another runtime replaces the registry context with the same plugin ID and version during graph construction or admission
- **THEN** old or intermediate snapshots cannot admit execution; only a successful current rebuild can restore admission

### Requirement: Optional trusted execution inspection
Trusted plugin middleware MAY inspect an optional execution scope. Absence MUST yield no identity or authority; a present invalid, foreign or revoked scope MUST still fail authorization. An authenticated view SHALL expose an immutable fresh required-extension map so global middleware can preserve ordinary unbound behavior without resolving unavailable business services.

#### Scenario: Ordinary execution without a ticket
- **WHEN** global middleware explicitly requests optional inspection and no execution ticket exists
- **THEN** it receives no execution view, while required-extension public admission still rejects missing identity and business tools gain no authority

#### Scenario: Revoked identity is not ordinary absence
- **WHEN** an existing ticket fails current actor or application authorization
- **THEN** optional inspection rejects it instead of treating the execution as an ordinary unbound call

### Requirement: Mounted read-only tool capabilities
The Host SHALL accept read-only declarations only during trusted setup for tools registered by that same plugin context. Native conversation modes MUST consume only declared tools actually mounted in the current successful graph, using their final runtime names. Client configuration MUST NOT expand this capability. Declarations MUST expire with their context and generation.

#### Scenario: Declared tool in Ask and Plan
- **WHEN** an enabled declared read-only tool is mounted in a current graph
- **THEN** native Ask and Plan permit its discovery and execution while undeclared mutating tools retain native restrictions

#### Scenario: Disabled, replaced or forged capability
- **WHEN** a tool is disabled, its context is replaced, or a caller supplies arbitrary extra read-tool names
- **THEN** those names do not become read-only capabilities and required-extension admission still rejects stale graphs

#### Scenario: Runtime tool name is sanitized
- **WHEN** actual graph construction renames a declared registration because of sanitization or a name collision
- **THEN** only the final name of that same mounted tool receives the declaration, not an unrelated tool with a similar name

#### Scenario: Capability provider changes during an invocation
- **WHEN** an invocation uses declared read-only tools without persistent required-extension bindings and their provider is invalidated
- **THEN** its transient generation lease is invalidated before stale mode capabilities can execute against a replacement graph; no persistent requirement is added

#### Scenario: Native deferred-tool discovery in read-only modes
- **WHEN** a current declared read-only tool needs the SDK's native client tool-search discovery
- **THEN** Ask and Plan permit the SDK-owned metadata-only discovery tool; plugin and MCP registrations cannot impersonate its reserved runtime name or gain permission to execute discovered mutating tools

### Requirement: Native synthetic assistant output
Plugins SHALL use native graph message output for synthetic assistant responses that end without model invocation. Native streaming, call and history MUST retain exactly one matching assistant response. Additional custom-to-token conversion MUST NOT duplicate an already native response. Ordinary model streams and unrelated custom events MUST retain their behavior.

#### Scenario: Plugin ends without a model call
- **WHEN** a plugin appends a synthetic assistant message and ends the graph without invoking the model
- **THEN** native chat receives that text once and call/history retain the matching message

#### Scenario: Ordinary streams and custom payloads
- **WHEN** an ordinary model streams text or the graph emits an unrelated custom payload
- **THEN** the existing output is preserved without converting custom payloads into additional assistant text

### Requirement: Current user input provenance
The authenticated Dashboard gateway SHALL stamp the current normalized user text separately from rendered Human message content. Empty text MUST remain explicit when attachments add hints or extracted content. Client metadata MUST NOT replace this server-created value. The field SHALL confer no identity or permissions and MAY persist under existing Human message metadata rules.

#### Scenario: Rendered attachments are not the user's question
- **WHEN** rendering adds attachment hints, extracted document text or localized placeholders
- **THEN** current-input metadata retains only the actual current inbound text, including an empty value

#### Scenario: Display text is rewritten
- **WHEN** native conversation-mode logic rewrites the Human display content
- **THEN** current-input metadata remains unchanged and does not inherit client-supplied replacement metadata

### Requirement: Backward compatible host extension
Business rules and UI MUST reside in plugin packages. Core changes SHALL be generic Host API or fixed UI surfaces. An ordinary agent with no required extensions MUST retain existing behavior.

#### Scenario: Existing renderer plugin
- **WHEN** an existing tool-renderer plugin is loaded with no new host registrations
- **THEN** its existing setup and rendering still work without enterprise configuration

### Requirement: Authenticated generic requirement readiness
The Host SHALL expose a read-only readiness hint for persisted Agent and optional thread requirements. It MUST authorize the current user, shared-Agent access and ownership of any supplied thread. Missing or disabled required plugins MUST remain visible. Invalid records, conflicting versions or read failures MUST NOT become an empty ready result. The hint MUST NOT replace final execution admission.

#### Scenario: Shared employee reads current requirements
- **WHEN** a non-owner employee can access a shared Agent and supplies their own persisted thread
- **THEN** the generic response includes merged Agent and matching-thread requirements without exposing declaration owners, credentials or other threads

#### Scenario: First native thread does not exist yet
- **WHEN** the request supplies a valid Agent and no thread
- **THEN** only Agent-scope requirements are returned; pending or invented thread IDs cannot stand in for a persisted thread

#### Scenario: Required extension has been uninstalled
- **WHEN** persisted requirements name a plugin that is missing, disabled or has an incompatible active version
- **THEN** that required ID remains in the response and is unavailable; no plugin facade or source request is needed to report it

#### Scenario: Readiness changes after the read
- **WHEN** a previously ready hint becomes stale before a turn starts
- **THEN** the existing backend execution gate independently rejects unmet requirements
