# Relay server entrypoint

Run the independent relay package with Python 3.12 and its frozen `uv.lock`:

```
uv sync --locked --extra test
.venv/bin/wordheat-relay migrate --config-root /absolute/path/to/deploy/public-relay
.venv/bin/wordheat-relay serve --config-root /absolute/path/to/deploy/public-relay --host 127.0.0.1 --port 8766
```

`--config-root` names the one relay configuration directory. The only environment file read is that directory's `.env`, corresponding to the repository's `deploy/public-relay/.env`. There is no ambient environment merge, parent discovery, root API `.env`, `local.env`, second `relay.env`, or command-line secret. The directory must be owned by the process user and not group/world writable; the fixed file must be a regular, owner-only 0600 file. Symlinks in the root or its ancestry and symlink files are rejected. Startup errors use static codes, and HTTP access logging is disabled.

`migrate` loads only the migration DSN, database identity, runtime role and environment. `serve` loads only the runtime DSN and its runtime options. Their roles must remain distinct; runtime role may not own the database or hold DDL privileges. The relay database is independent of the local API database. `migrate` applies the packaged relay migration chain; running it is an explicit database mutation. The CLI creates no database, role or container. Runtime startup verifies the database marker and required schema revisions before listening. The HTTP host is an explicit IP option; reverse proxy/TLS and server deployment remain the operator's responsibility.

With empty official credentials, roots, signer and deployment allowlist, the process can serve `/relay/v1/health/live` and run durable maintenance/fulfillment loops. This liveness response proves only process availability. Official callback input returns unavailable, pairing/leases cannot manufacture authority, and freshness cannot be signed. Missing official configuration never creates a gate or production pass. SDK traffic remains disabled unless `DOUYIN_EXTERNAL_CALLS_ENABLED=true` and the separately required profile/credentials are present. No external request is made during composition. See `README.control.md` for protocol and external acceptance limits.

## Restricted trust configuration

`RELAY_TRUST_ROOTS_FILE` is an optional simple filename within the same private root, owner-only 0600, at most 1 MiB. Its exact JSON shape is:

```
{"version":1,"keys":[{"key_id":"authority-id","public_key_b64url":"<canonical unpadded base64url 32 bytes>","domains":["wordheat-live.readiness"],"types":["live_grant"],"test_only":false}]}
```

Readiness keys may list only `gate_evidence`, `live_grant`, `revocation`, `freshness_proof`, `pairing_intent` within `wordheat-live.readiness`. Trust initialization keys use only domain `wordheat-live.trust`, type `key_bundle`. Mixed domains/types, unknown purposes, duplicate IDs, repeated public keys, noncanonical keys or more than 128 keys are rejected. A test-only key requires `WORDHEAT_ENV=test`. This file is a preconfigured trust anchor; no Web setter or self-trusting bootstrap exists. Signed rotation/revocation retains the original control-plane checks.

`RELAY_FRESHNESS_KEY_ID` and `RELAY_FRESHNESS_PRIVATE_KEY_B64URL` configure an optional long-term Ed25519 server signer. The seed is exactly 32 canonical base64url bytes and stays only in the private relay `.env`. The matching configured public key must have exclusively the readiness `freshness_proof` purpose. A grant, gate, pairing or key-bundle signing private key cannot be loaded as this server signer. No key is generated automatically. This is a server freshness signature, not platform or release approval.

`RELAY_ALLOWED_CANDIDATES_FILE` optionally identifies another owner-only 0600 file, exact shape:

```
{"version":1,"allowed_candidate_digests":["<64 lowercase hexadecimal digits>"]}
```

At most 256 unique digests are cached immutably at process start. Empty policy denies official candidate scopes. Changing the file requires restarting this process. The policy only filters deployment candidates: it cannot sign or substitute for a live grant, gate, RC identity, pairing intent, platform room verification, revocation or G0/G2 evidence. Each request and current owner still passes the existing signature, immutable scope, lifecycle and revocation checks.

Short platform access tokens, device tokens, launch tokens and boot keys must remain in memory. Known ephemeral-token environment names are rejected. Vendor app/push/RSA keys are separate long-term server settings; do not equate push secret with app secret or silently change signature profile. The committed `.env.example` contains empty values only.

## Isolated execution checks

The test-only `RELAY_TEST_NAMESPACE` and `RELAY_TEST_NAMESPACE_MARKER` can address only a previously created `wordheat_relay_pytest_<32 lowercase hex>` namespace when `WORDHEAT_ENV=test`. The exact existing schema comment must match the fixture's 32-hex nonce; migration additionally requires that the current migration role owns that schema. The CLI never creates a namespace through these settings, and they are forbidden in non-test mode. Normal deployment omits both and uses `relay`.

Regression tests exercise configuration rejection, signature-purpose validation, cached deployment policy, owned PostgreSQL migration and a real loopback relay child without official keys. These are local execution evidence, not Windows or real-platform G0/G2 acceptance.
