# Relay control and Douyin wire

The relay has independent PostgreSQL persistence, device pairing, scoped leases,
immutable frames, explicit stream bootstrap/RPO, checkpoints and historical
delivery facts, a per-room platform-operation fence, bounded task/fulfillment
workers and retention/quota checks. A missing official adapter uses an explicit
unavailable provider; callback/open cannot silently succeed. Expiry, fulfillment
and retention run independently. An unknown stop result stays unresolved.

The public wire implementation is `app/provider.py`. Source URLs are adjacent to
the implementation. It implements the published raw-header/body MD5 push
signature, process-wide per-app memory token refresh, task start/query/stop,
bounded array ACK reports and native launch-token room lookup. Original vendor
timestamps are diagnostics only; gameplay uses trusted relay receive time. Raw
UIDs are HMAC-normalized before local transport and never become public identities.

Request profile must be explicitly `data_open_access_token` or `openapi_rsa`.
The latter requires RSA-2048 application private key, key version and platform
public key, signs the exact five-line request and rejects unsigned/tampered 2xx
responses. Token bootstrap is the separately documented client-credential
endpoint. The MD5 push secret is distinct from AppSecret. No profile fallback or
G0/production pass is inferred from configuration or a contract fixture.

Comments have no documented trustworthy test flag. A trusted server provenance
resolver is mandatory: controlled test-live is explicitly test data; production
requires matching signed G0 channel/profile provenance. Body `is_test` cannot
authorize production. MockTransport adapters remain test providers.

Optional server configuration names (all empty/disabled by default):

```
DOUYIN_APP_ID
DOUYIN_APP_SECRET
DOUYIN_PUSH_SECRET
DOUYIN_IDENTITY_HMAC_KEY_B64URL
DOUYIN_REQUEST_PROFILE
DOUYIN_CHANNEL_PROFILE
DOUYIN_CHANNEL_ID
DOUYIN_EXTERNAL_CALLS_ENABLED=false
DOUYIN_RSA_KEY_VERSION
DOUYIN_RSA_PRIVATE_KEY_B64
DOUYIN_PLATFORM_PUBLIC_KEY_B64
```

PEM keys use standard base64 in the last two fields; the HMAC key uses canonical
unpadded base64url and decodes to 32 bytes. Secrets load only in this server.
Access tokens are memory-only and do not enter configuration. Constructing or
importing these classes does not fetch a token or make a platform request.

Apply both packaged relay SQL files through the migration owner (schema version
2). Runtime cannot DDL/delete frames. Callback batches are bounded at 1MiB/1000,
deduplicated once and inserted atomically with a single room/window/stream lock.
Only after commit is the official 204 acknowledgement returned. Frames remain
within 64KiB; all retained room segments count toward payload/message quotas.
Payload cleanup preserves source/ACK/terminal metadata and pending fulfillment
references. Native network responses negotiate the BE extension; C1 historical
frame bytes and hashes are never rewritten.

The dedicated tests use owned random PostgreSQL namespaces and real loopback
HTTP/WS; upstream vendor calls use MockTransport/public golden vectors. They are
not official account, Windows/OBS, G0, G1-R or G2 acceptance evidence.
