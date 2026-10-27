# WordHeat private scorer

WP04/WP08 component implementation for BE-A02/T06–08/T21/WT04. The independent
process only returns embeddings. API owns exact matches, aliases, win decisions,
calibration and points. Component tests and real encoding do not grant G1-Q,
G1-R or platform acceptance.

A trusted launcher calls `create_app(secret: bytes, manifest_path: Path,
model_root: Path, encoder=None)` and serves the returned ASGI app using one
Uvicorn worker bound to `127.0.0.1` or `::1`, without reload. The launcher supplies
at least 32 bytes of fresh per-boot secret through its restricted in-memory/IPC
boundary. There is deliberately no CLI/environment/default-secret startup path.
The optional encoder is dependency injection for component tests only; production
uses the real offline SentenceTransformer.

Startup verifies the manifest and every allowlisted file's relative path, size,
SHA-256 and absence of symlinks or extra files. The model uses only local files,
CPU, float32, safetensors, `trust_remote_code=False`, two torch CPU threads and
one model instance. Fixed Transformer/Pooling/Normalize modules are permitted;
custom module code and mismatched pooling/tokenizer/dimension fail startup.
Manifest fields include model/revision/license/files/dimension/normalize/pooling/
precision, `tokenizer.revision` equal to model revision, `preprocessing: identity`
and bounded `warmup_texts`. A real failed warmup leaves readiness false.
Calibration/wordbank versions must be frozen by their owning publication flow;
encoding tests do not invent approved versions.

Only `POST /internal/v1/embeddings` exists; automatic docs/OpenAPI are disabled.
The launcher must bind loopback, and non-loopback peers are rejected. Input:
`{request_id,manifest_id,texts,deadline_ms}`. IDs are 1–128 ASCII letters/digits/
`_.:-`; texts are 1–32 strings of 1–256 characters; deadline is an integer
1–10000 milliseconds. Unknown fields, coercions, duplicate JSON keys and
noncanonical request JSON are rejected. A request carries a relative monotonic
queue/inference budget; expiry never accepts a late result. Inference cannot be
preempted inside torch; one executor task can finish after its caller times out.
New work remains bounded by 200 queued requests, 32 texts per merged batch and
one worker. The merge window is 20ms. Same-text work within a batch coalesces;
fixed-manifest/identity-preprocessing SHA-256 text cache uses LRU with at most
1024 entries and an 8MiB conservative resident-size accounting budget. This
cache never stores puzzle mappings or scores.

## Internal byte protocol: wordheat-embeddings/1

Both directions use RFC8785 JCS canonical UTF-8 JSON, no trailing newline.
`manifest_sha256` is lowercase SHA-256 of the **JCS manifest object**, not its
pretty-printed file bytes. Request and response body digests are lowercase
SHA-256 of the exact bytes transmitted.

Request headers are `Content-Type: application/json`,
`X-WordHeat-Issued-At: <13 decimal Unix milliseconds>` and
`X-WordHeat-Signature: <64 lowercase hex HMAC-SHA256>`. Issued-at must be within
30 seconds of wall time and is authenticated. A boot-fixed UTC/monotonic anchor
is never refreshed under the current secret. A deviation above 500ms or a
monotonic rollback permanently poisons that app's boot identity, marks readiness
false and returns `BOOT_CLOCK_UNTRUSTED`; recovery requires a new boot and fresh
key from the trusted launcher. Checks run before admitting requests and before
signing inference results. Verified request IDs are retained for 62 monotonic
seconds: 30s maximum future timestamp plus 30s allowed age, 500ms drift allowance
and a safety margin. At most 10000 IDs are retained; full replay storage refuses
new requests. Expired signed requests cannot become valid when the replay entry
expires.

The signed bytes are JCS of this object (values shown as descriptions):

```json
{
  "protocol": "wordheat-embeddings/1",
  "direction": "request or response",
  "method": "POST",
  "path": "/internal/v1/embeddings",
  "request_id": "the originating request ID",
  "body_sha256": "lowercase SHA256 of exact direction body bytes",
  "manifest_sha256": "lowercase SHA256 of JCS manifest object",
  "issued_at": "original request header string"
}
```

Response body is exactly `{request_id,manifest_id,dimension,vectors}` and carries
`X-WordHeat-Signature`; the signature binds the original request ID and issued-at.
API validates the signature before parsing/accepting vectors, then exact identity,
model, dimension, finite coordinates and unit norm (absolute tolerance 0.001).
The client enforces one total monotonic deadline for sending, full body reading,
stream close, signature/JSON parsing and vector validation with `asyncio.timeout`,
then checks the monotonic clock again before accepting the vectors. Individual
httpx read timeouts are additional bounds; a slow drip cannot extend the total
budget. Any exhausted budget raises `SCORER_TIMEOUT` (504), even if the final
signature and vector are otherwise valid. Body limits are 64KiB request/4MiB
client response. No automatic retries, network
model downloads, fake-score fallback or redirects are enabled. Errors have
`{error:{code,message,retryable}}`; the client never consumes error bodies as vectors.

## API adapter

`apps/api/app/modules/scoring/client.py` independently implements the byte
contract; it imports no other app. Construct `ScorerClient(secret, manifest_id,
manifest_sha256, dimension, base_url=loopback_http_url)` in the API startup
boundary. Call `await client.score(word=..., frozen_puzzle=...)`, close an owned
client with `await client.aclose()`. An injected httpx client is caller-owned and
must itself be local/trusted; injection is used for transport component tests.

Frozen puzzle input requires matching `manifest_id` and
`calibration: {b,u,gamma}` with finite numbers, `u>b`, `gamma>0`. Supply a unit
`target_vector` of the matching dimension, or `word` (legacy `answer` also
accepted), in which case guess and target are encoded in the same batch.
Calibration is never defaulted. Nonexact output uses the Spec's dot-product /
calibration formula, rounded to one decimal and capped at 99.9. Exact/alias
handling remains in API. `ScorerFailure` exposes `code`, `message`, `status_code`,
`retryable`, `details`, ready for mapping into the API's DomainError envelope.

## Verification

API and scorer are separate services with separate virtual environments and
service working directories. Their Python package is independently named `app`;
do not install both distributions into one environment or run both import trees
in one process. Each service starts from its own `apps/<service>` working
directory. The development verification environment shares installed dependency
wheels only: tests run in a fresh Python process, and the API client is loaded by
an explicit importlib file location. This does not prove that co-installing the
two app packages is supported.

From `apps/scorer` with its frozen scorer environment:

```sh
python -m pytest -q tests/unit
WORDHEAT_TEST_MODEL_MANIFEST=/absolute/private/model-manifest.json \
WORDHEAT_TEST_MODEL_ROOT=/absolute/private/validated-model \
python -m pytest -q tests/integration -m real_model
```

Without both explicit model paths, the real test reports `NOT_RUN` as a skip,
never a real-model pass. The real test uses synthetic calibration solely to
check the formula and authenticated transport, not approved quality metrics.
Dependencies in `pyproject.toml` are pinned to the actually executed environment;
the workspace owner maintains its complete transitive lock. Model weights stay
private and are not included in source. `resources/model-manifest.json` contains
the real downloaded BGE file hashes, not fixture hashes.
