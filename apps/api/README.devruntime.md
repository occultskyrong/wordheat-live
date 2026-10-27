# Mac/Linux development processes

This host is a developer tool for WP04/WP08 integration. It does not implement
WP18's Windows native Launcher, SID/ACL/DPAPI, Job Objects, single instance lock,
or Windows/OBS acceptance. Windows and other platforms fail with
`WINDOWS_NATIVE_REQUIRED`; no POSIX substitute is silently selected.

The assembled source root must contain the normal API/scorer source, the real
`ScorerClient`, `app.application.create_app`, and this overlay's four runtime
files. Each child runs its own interpreter, cwd, and `app` package path. The
interpreters may point to the same dependency-only virtualenv during development;
neither app distribution needs to be installed into it. There is no runtime
installation, model download, database migration, or PostgreSQL/OBS lifecycle call.

Run the host with explicit absolute paths:

```sh
/private/tmp/wordheat-backend-development-4u1eo07u/venv/bin/python \
  "$SOURCE_ROOT/scripts/backend/20261025-run-local.py" \
  --root "$SOURCE_ROOT" \
  --config-root "$API_CONFIG_ROOT" \
  --api-python /private/tmp/wordheat-backend-development-4u1eo07u/venv/bin/python \
  --scorer-python /private/tmp/wordheat-backend-development-4u1eo07u/venv/bin/python \
  --manifest /private/tmp/wordheat-backend-development-4u1eo07u/model-manifest.json \
  --model-root /private/tmp/wordheat-backend-development-4u1eo07u/models/validated-bge-small-zh-v1.5
```

`SOURCE_ROOT` means the primary agent's explicitly assembled source tree, not this
partial overlay. `API_CONFIG_ROOT` is an explicitly selected private directory
containing `.env` (0600). Only the API calls `load_settings(API_CONFIG_ROOT)`; its
existing whitelist consumes `LOCAL_DATABASE_URL`, `WORDHEAT_INSTANCE_ID`, and
`LOCAL_ANALYTICS_CURSOR_KEY`. The host and scorer do not parse that file.
`WORDHEAT_TEST_CONFIG` is a test fixture selector, not an app runtime config or a
replacement for the private `.env` boundary. Run database smoke only in the
primary agent's allocated test slot; do not concurrently clear shared fixtures.

API binds 127.0.0.1:8765; scorer binds 127.0.0.1:8124. The host forwards only PATH,
LANG, LC_ALL, LC_CTYPE, SYSTEMROOT plus fixed offline/thread flags. It strips
PYTHONPATH, PYTHONHOME, database/cloud credentials, proxy/token/cache settings and
all other ambient configuration. No boot key is passed in environment, argv,
files, logs, or `.env`: a fresh random 32-byte key is passed over an anonymous
pipe with an explicit `pass_fds` list. A separate private return pipe carries an
HMAC-domain-separated acknowledgment binding OS child PID, role, boot ID,
challenge, canonical manifest SHA-256 and actual bound loopback endpoint. The
acknowledgment is sent only after successful application lifespan and socket
binding; an occupied port cannot identify a child. Child raw output is suppressed
to avoid private library errors escaping; the host prints fixed reason codes.

The scorer validates all manifest files and strictly warms the real model before
acknowledging. Manifest SHA-256 is over RFC8785 canonical bytes. API composition
uses `app.application.create_app(settings, scorer=proxy)` so implemented routers
are retained. `ScorerRuntime` allocates and closes one `ScorerClient` per async
score/embeddings call, including calls run on different `asyncio.run` loops by
the synchronous service. An independent background monitor sends authenticated
real embeddings probes once a second outside database transactions. Readiness
only reads the immutable in-memory timestamp; failures invalidate it immediately
and success expires at 2 seconds. The lifespan wrapper preserves existing API
startup/shutdown and cancels/joins the monitor.

Ctrl-C/SIGTERM, startup failures/timeouts, and owned-child exit cause sequential
API-then-scorer terminate/wait/kill/wait. Only returned Popen handles are acted on;
no lookup or kill by port/PID pattern occurs. POSIX parent SIGKILL/crash containment
is outside this dev tool's guarantee and is not Windows Job Object evidence.

Verification command (mechanism tests, no PostgreSQL or real model):

```sh
/private/tmp/wordheat-backend-development-4u1eo07u/venv/bin/python -m pytest -q \
  --basetemp=/private/tmp/wordheat-backend-development-4u1eo07u/devruntime/.test-tmp \
  /private/tmp/wordheat-backend-development-4u1eo07u/devruntime/tests/integration/test_local_bootstrap.py
```

Overlay manifest:
- `scripts/backend/20261025-run-local.py`
- `apps/api/app/runtime_entry.py`
- `apps/scorer/app/runtime_entry.py`
- `apps/api/app/modules/scoring/runtime.py`
- `tests/integration/test_local_bootstrap.py`
- `apps/api/README.devruntime.md`
