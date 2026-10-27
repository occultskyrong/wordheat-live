# Backend wire contracts

Historical `LW-C1/1.0.0`, schema version 1, remains byte-for-byte unchanged in
`c1-baseline.json` (SHA256 `d69ae165a6d1169e00f20bb03b4cd66bdc9ed2fe7ef2a730c2a130934a88b4e8`).
The immutable route array contains 45 entries: 42 HTTP and three WS/WSS protocols.
Current backend additions negotiate `LW-BE/1.1.0`; missing/unsupported headers
retain the original compatibility rules. Existing read/C1 branches are not
silently replaced by mandatory BE branches.

`openapi.yaml` is JSON-formatted YAML 1.2 / OpenAPI 3.1. It combines the actual
standard API application factory, offline scorer factory and full relay factory.
`source-inventory.json` binds every actual route and all source/lock hashes.
Dynamic relay templates are exported with allowed operation values and explicit
concrete aliases, counted separately from actual router templates. Native local
socket actions are described in `native-control.md` and
`native-control.schema.json`, not invented HTTP endpoints.

`backend-extension.schema.json` contains standalone current DTO definitions;
`relay.schema.json` defines immutable decoded bodies and transport/control frames;
`local-websocket.schema.json` exports DISPLAY/STUDIO scopes independently.
`runtime-manifest.schema.json` freezes the release-manifest wire shape without
asserting that a Windows implementation/release has passed. No schema uses empty
Any placeholders. C1 diagnostic JSON values and provider extra fields have an
explicit recursive JSON grammar; typed maps remain typed maps, not open Any.

Generate from the repository root with each app's own locked interpreter:

```text
<api-python> -B scripts/backend/20261026-export-contracts.py
  --root <output-repository-root>
  --api-root <reviewed-api-source-root> --api-python <api-python>
  --scorer-root <reviewed-scorer-source-root> --scorer-python <scorer-python>
  --relay-root <reviewed-relay-source-root> --relay-python <relay-python>
  --source-status FROZEN_INPUT
```

Each application is imported in a separate subprocess with a minimal environment.
The exporter never loads `.env`, starts lifespan, connects a database or loads a
model. Connection APIs are blocked during construction. Scorer manifest loading
and relay DB-service construction are explicitly replaced only for schema
registration; actual route code/control routers are unchanged. Raw Request
handlers use reviewed concrete wire adapters in `export_models.py`; source changes
are detected and require regeneration/review. Cross-app schema collisions and
unreviewed raw routes fail export. Source hashes must remain stable across each
child export. `DRAFT_INPUT` remains visible until the caller provides final frozen
sources; this provenance label cannot grant runtime authorization.

```text
<api-python> -B packages/contracts/check_contracts.py --root <repository-root>
<api-python> -B -m pytest packages/contracts/tests -q
```

The checker independently validates every schema/ref/operation, actual route
coverage, baseline coverage, source hashes, versions, empty HTTP 204 semantics,
and positive/rejecting fixtures. A public Douyin callback returns 204 with no
body; it is not given a fictitious JSON success envelope. SDK callback fields,
headers and signature profile come from the actual published-wire adapter.
Fixtures are synthetic; SDK execution remains fail-closed without actual
credentials/provenance and no fixture is official success/G0 evidence.

A successful contract check means current backend wire definitions are present
and consistent. It does not establish frontend consumer signoff, actual database
or model execution, Windows/OBS acceptance, rc_verified, G0/G1/G2 or permission to
publish a release. Neither frontend source/generated clients nor the original
Spec/C1 bytes are modified by this exporter.
