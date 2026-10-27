# Private native control protocol

This is a local Unix socket protocol, `wordheat-native-control/1`, rather than an
HTTP API. It is not exposed as `/api/v1/native/*`. The Mac/Linux implementation
uses kernel peer UID/PID and approved interpreter/CLI/program hashes, an owner-only
0700 root and 0600 socket/registration. Windows SID/ACL/DPAPI/UAC remain separate
implementation and acceptance requirements.

Each connection carries one authenticated action. JSON frames are UTF-8 objects
terminated by one newline; maximum frame size is 65536 bytes including framing.
Duplicate keys, non-JSON numbers, embedded extra frames and unknown action fields
are rejected. Current private transport/auth/client request shapes are frozen in
`native-control.schema.json`; they are checked independently from HTTP coverage.

1. The server validates the OS peer before sending `protocol`, `boot_id`,
   `installation_id`, and a fresh `challenge`.
2. The CLI validates the server PID/program/registration before reading a hidden
   password or password FD. It responds with `action=auth`, that challenge,
   `username` and `password`. The CLI does not read `.env` for authentication.
3. The server verifies the current operator, returns `authenticated=true`, then
   accepts one `{action,payload}` object. Current permission/ownership checks run
   again at the action and commit boundary.
4. Success is `{data:<action result>}`. Errors are
   `{error:{code:<string>,message:<string>}}`; neither includes secrets or raw
   exception text. This outer result frame is described here rather than being
   mislabeled as a C1 HTTP envelope.

| Action | Payload | Result and decision boundary |
| --- | --- | --- |
| `status` | Empty object | Private current runtime/readiness projection; no mutation. |
| `request` | `command`, `reason`, optional maintenance `operation` | Persistent CommandResult; request alone cannot confirm maintenance. |
| `prepare` | `command_id`, registered `artifact_id`/`target_id`, optional `allow_data_loss` | Fixed registered paths, immutable file/resource hashes, installation and plan fingerprint; data loss requires explicit review. |
| `confirm` | `command_id`, `fingerprint`, `nonce`, `installation_id` | Current native confirmation bound to the unchanged plan; queues fixed stages. |
| `cancel` / `recover` | `command_id` | Persistent scoped CommandResult; recovery reconciles prior outcomes. |
| `trust.prepare` | Registered `source_id` | Review of fixed control file bytes/hash and declared signed domain/type. Parsing is not authorization. |
| `trust.confirm` | Same four confirmation fields | Typed repository receipt: object/type/issuer revision/import status, signature-input digest, file SHA and confirmation reference. Key bundle, gate evidence, live grant and revocation retain repository verification/issuer/lifecycle/revocation rules. Freshness proof files are rejected. |
| `device.prepare` | Empty object | Review existing public device identity or initialize intent. |
| `device.confirm` | Same four confirmation fields | Public device fingerprint. Mac seed is atomically persisted only to the private root `.env`; no short token is persisted. |
| `pair.prepare` | Registered `source_id` for signed pairing intent | Current operator/device/app/room/installation/candidate scope review; no browser room fact. |
| `pair.confirm` | Four confirmation fields plus hidden `launch_token` | Registered relay pairing/PoP and platform room verification, then paired public status. Launch/device tokens stay in memory; no arbitrary target URL or root key is accepted. |

The generated source inventory hashes `runtime_socket.py`, the native controller,
artifact registry, fixed CLI and pairing bridge. Domain result DTOs are the current
runtime status/command models and native plan/repository/device result types in
those source-bound adapters. The request schema does not assert that a serialized
`verified`, `passed`, OS identity, or `confirmed` boolean grants any capability.
The native issuer/context and execution evidence are process-private typed values.

Synthetic schema fixtures establish no OS identity, SDK permission or gate pass.
Actual OS/native and cold-restart evidence belongs to runtime/assembly tests; this
contract document does not substitute for those executions.
