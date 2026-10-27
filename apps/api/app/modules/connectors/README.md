# Native relay control

`OfficialCoordinator` is the SQL/application port; `ConnectorSupervisor` owns
the single active native WSS and independent dispatch, historical ACK, delivery
observation and 0.5-second safety loops. Network waits never sit in business
transactions. Registered endpoints reject HTTP/WS redirects and implicit proxies.

The application composes `TrustedControl` with fixed public roots, a cached
actual candidate digest resolver and the same boot identity as the core. Wire
`service.official`, readiness's `trusted_control` and `connector`, command lookup,
the analytics `observe_delivery` sink, receipt and terminal-feedback hooks. The
coordinator requires that composition at every official result/open guard.
Without it, local rehearsal remains independent and official operations reject.

API metadata is connector-owned. Apply `20261026-connector-control.sql` through
the normal migration owner. `connector_ack_outbox` counts pending/running/retry;
total attempts never reset, while an explicit new query intent with
`retry_failed_deliveries=true` permits one further bounded retry cycle. It only
re-reports already validated facts, never replays game logic or display effects.

An explicit new-scene start may bootstrap a fresh stream namespace. Its initial
watermarks mean no data in that new namespace (`start_seq-1`). Old stream
watermarks, byte hashes and gap/RPO evidence remain in history. Reconnect cannot
bootstrap or skip a gap. Historical display ACKs retain original stream/source,
installation, verified client time and paired-device scope independently of the
new active scene. Unknown remote fulfillment remains null.

`NativePairingClient` and `NativePairingBridge` are private native use cases,
not Web runtime operations. The bridge validates the OS peer context and actor's
maintenance permission, prepares a digest-bound confirmation, and persists only
the long-term Mac development seed and non-secret pairing id in the product root
`.env`, atomically with 0600 permissions. It never overwrites an existing seed.
Short device/launch tokens stay in memory and TLS bodies, not argv/env/logs.
Windows production uses its separate DPAPI/ACL implementation.

Pairing requires a signer explicitly allowed for `pairing_intent`, fixed app,
room, anchor hash, installation, actor, candidate and device public-key hash.
Ordinary live-grant permission cannot manage a device. The actual native client
obtains the platform-verified tuple through relay issue/complete and registers
`VerifiedRoomBinding` transactionally. Same binding id cannot change its immutable
app/room/installation/connector tuple. Public key bundles rebuild from pinned
roots on every boot; key/object revocation cannot be undone by old package replay.
