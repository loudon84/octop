# Plugin Host API v1

Octop exposes an optional application-scoped Host facade to plugins that opt in
through `register_host_setup(ctx, callback)`. The callback receives a
plugin-scoped facade after the harness loader returns its `LoadedPlugin`; it may
register versioned services, typed actions, and a resource reader. Registration
does not read the database. The facade resolves current shared services when an
operation runs, so database binding and rebinding remain dynamic.

Plugins that do not register the callback keep the existing tool, hook, and
skill behavior. A manager created without a Host (for example, the standalone
CLI) can still load ordinary plugins. Host actions and resources are
unavailable unless the application has created and bound a Host.

## Registered actions

`register_action(name, input_model, output_model, handler, summary,
permission)` accepts a fixed action name, Pydantic input and output models, and an
async handler. `permission` is `authenticated`, `admin`, or `agent_owner`.
Handlers receive a `PluginActor` created from the authenticated user and a
validated optional scope. They do not receive a `Request`, database repository,
or server object.

Authenticated clients can inspect the public action catalog at
`GET /api/plugins/{plugin_id}/actions`. It contains the registered summary,
permission, and input/output JSON schemas, not plugin configuration or
credentials. Call an action with
`POST /api/plugins/{plugin_id}/actions/{action}`:

```json
{
  "api_version": 1,
  "context": {"agent_id": "agent-id", "thread_id": "thread-id"},
  "payload": {"field": "value"}
}
```

The envelope rejects extra fields. In particular, actor identity and
permissions are never accepted from the client. Agent and thread identifiers
are untrusted references and are checked against the current user before
dispatch. The action's payload schema is available from the catalog. The
handler output is validated against its declared output model.

Unexpected callback, input, and output failures are converted to safe host
errors; exception text and validation input are not returned. A plugin may raise
`PluginActionError(code, message, status=..., details=...)` only when it has
already produced an explicitly safe, localized business error. The API forwards
that declared error contract.

## Resources and lifecycle

`register_resource(handler)` registers an async opaque-token reader. The reader
returns `PluginResource(body, media_type, filename=None)`, where `body` is bytes
or an async byte iterator. Octop forwards the body and media type, plus a
validated inline filename when supplied. Redirects, arbitrary response headers,
filesystem paths, and provider URLs are not part of this contract. Each request
rechecks the authenticated actor and optional agent/thread context.

Plugin disable, replacement, reload, uninstall, and server stop invalidate the
old generation and cancel registered actions and active resource streams. A
handler that completes after invalidation cannot return a successful action
result. Host storage and encryption operations remain on the executor; lifecycle
entry points that can wait for those operations also run outside the event
loop. Closing the Host waits for registered work before the database is closed.

The Host facade is a capability boundary for trusted plugin code, not a direct
HTTP or LLM database API. Plugin-specific resource authorization and business
rules remain the plugin's responsibility. Ordinary plugins that do not use the
Host callback remain compatible.

## Required extensions and trusted turns

`RequiredExtensionWrite` may be included in `facade.commit(...,
requirements=...)`. Its reserved `@host/required_extensions` row is committed in
the same database transaction as the plugin's records and credential
activations. Plugin record reads and writes cannot access that namespace. Each
facade can change only its own declaration; an empty map explicitly clears it
while advancing and retaining the revision. Host declarations are keyed by an
encoded agent/thread/declaring-plugin tuple and survive plugin unload or Host
recreation.

`facade.commit(writes, activations=(), *, requirements=(), checks=())` also
accepts immutable `RecordCheck(namespace, logical_key, expected_revision)`
preconditions. Checks read records only in the calling plugin's namespace and
do not update their payload, revision, timestamps, or owner. Revision `0`
means the record must be absent; positive revisions must match exactly. A
mismatch raises the safe `conflict` Host error and rolls back writes,
credential activations, and required-extension updates in that commit. Check
revisions must be non-boolean integers at least zero. Repeated identical
checks are evaluated once; checks for the same key with different revisions
are rejected as `invalid`.

The checks and all writes run in one database transaction. SQLite takes its
existing `BEGIN IMMEDIATE` writer lock before the checks. PostgreSQL commits
that include checks first execute the fixed
`LOCK TABLE plugin_records IN SHARE ROW EXCLUSIVE MODE` statement, before any
record read or write; the lock is held through commit. Commits without checks
do not take this PostgreSQL table lock.

`await facade.get_requirement(actor, agent_id, thread_id=None)` returns the
facade's own revision and exact required plugin versions after checking current
user, agent ownership or administration, `agent_plugins` permission, and thread
ownership. At execution time the Host combines agent and thread declarations
and rejects missing identity, unavailable extensions, or exact-version
mismatches before the harness is called.

Authenticated Dashboard turns carry a process-local, Host-bound ticket in
server-only inbound metadata. The ticket binds the authenticated user, agent,
thread, plugin context, and server-selected `turn`, `continuation`, or `resume`
kind. It is never restored from JSON, passed in the harness request, or written
to history. `await facade.current_execution()` is valid only inside that scope;
it rechecks the user's current enabled state, role, permissions, and agent/thread
access on each call and returns a fresh actor plus a deep copy of that plugin's
JSON context. A revoked permission is not retained in an earlier actor snapshot.

The common AgentManager stream, call, and HITL resume seams and both gateway
processor entries enforce required extensions. A bound invocation without a
trusted Dashboard identity is rejected; this includes IM, Bridge, CLI, cron,
and cross-agent child calls. Unbound agents and teams retain the existing
behavior. In-flight work is registered with every required plugin generation,
so disabling or replacing one cancels the turn. Plugins execute as trusted
in-process Python; these controls do not sandbox malicious plugin code.
