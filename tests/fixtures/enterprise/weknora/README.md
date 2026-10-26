# Synthetic WeKnora v0.8.3 contract fixtures

These fixtures are synthetic shapes modeled on the fixed Tencent/WeKnora `v0.8.3`
source contract documented in `prd/evidence/enterprise-p1/weknora-v083-source-contract.md`.
They are not captured from a deployed service, contain no user documents or credentials,
and do not establish deployed-version compatibility or A-01 acceptance.

Official source tag: <https://github.com/Tencent/WeKnora/tree/v0.8.3>

`knowledge_bases.json` models the non-paginated tenant KB list; `search.json` models
the JSON search envelope with optional KB attribution; `knowledge.json` models the
detail fallback used to prove attribution; `chunks.json` models text chunks and the
top-level page envelope.
