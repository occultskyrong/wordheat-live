# Backend entry and controlled local runtime

API, scorer and relay use separate locked environments. The API composition entry is
`app.application.create_app`; `app.main.create_app` is the core-only component entry.
The standard factory installs ScoreWorker, wordbank/replay/deadline workers,
AnalyticsMaintenanceWorker, ConnectorSupervisor, RuntimeWorker and the restricted
NativeControlServer. Their lifecycles stop before the API database is disposed.

The complete Mac/Linux development host is
`scripts/backend/20261025-run-local.py`. It starts the offline scorer first, checks its
per-boot IPC identity, then starts the API with a private stop/restart channel. The API
entry passes verified PrivateRuntimeBoot before runtime initialization. Neither process
receives the entire parent environment or per-boot keys through argv/env/files.

The following is an argument template, not a ready-to-run command. Supply the explicit
code/config roots, approved offline model manifest/root and each app's own interpreter:

```text
<api-python> scripts/backend/20261025-run-local.py
  --root <absolute-code-root>
  --config-root <private-config-root>
  --manifest <approved-model-manifest>
  --model-root <approved-offline-model-root>
  --api-python <api-interpreter>
  --scorer-python <scorer-interpreter>
```

The private config root contains an owner-only 0600 `.env` based on `.env.example`.
`WORDHEAT_INSTANCE_ID` must match the registered dedicated database. The runtime DSN,
migration DSN and test DSNs are consumed only by their own loaders; URI query overrides
are rejected. The full application lifespan requires an explicit root and never guesses
it from the working directory. Native/maintenance/control directories are current-user
0700; native socket/registration are 0600. Direct component Settings construction can
omit the root, but does not establish a running full application.

WARMING permits login, drafts/evaluation preparation, CREATED sessions and display
binding. Actual start/new rounds use current DB/model/wordbank/display checks. No official
keys/registered endpoint/candidate/grants are needed for local rehearsal. Official mode
requires the existing signed control-plane and registered platform facts; an environment
name or operator cookie cannot create them.

## Native control

`app.cli --root <config-root> native-control` uses the restricted Unix channel and actual
OS peer UID/PID/program identity. It does not read `.env` before asking for a hidden
password. Runtime HTTP commands request stop/maintenance; they cannot send confirmed
booleans, paths, root keys, programs or resource vectors. The native confirmation binds
installation ID, command and unchanged plan fingerprint. `device.prepare`/`device.confirm`
initialize the possession seed through this native channel. `pair.prepare`/`pair.confirm`
use a registered connector and signed pairing intent; the short launch token is read
privately and retained only in memory. `trust.prepare`/`trust.confirm` import registered
signed key bundles, gate evidence, live grants or revocations through the authoritative
repository. Freshness proofs are accepted only through a live challenge channel. This Mac port does not claim
Windows SID/DPAPI/UAC or OBS acceptance.

Mac long-term device seed is the dedicated private `.env` field; short PoP-issued device
tokens stay only in memory. The optional device file field is not a Mac raw-key JSON
adapter. Gate trust roots, release/candidate signing purposes and private backup SHA
registrations remain separate. A missing local PG tool registration does not prevent
rehearsal; backup/restore explicitly reports POSTGRES_TOOL_CONFIGURATION_REQUIRED.
Actual tool/target registrations are user-specific private files and are not built into
source, images or this document. No service/container/role lifecycle action is implied.

## Candidate integrity

Official candidate material is a restricted Ed25519/JCS envelope with all nine component
groups: API/scorer/relay/contracts/platform/Windows/model/wordbank/calibration. API/scorer
runtime code and dependency locks, current model identity and activated joint resources
must match; the Windows package includes its declared prebuilt Web inventory. The signed
release reference, schema and compatibility metadata are checked too. An API source hash
alone is not an RC. Candidate verification never creates rc_verified/G0/G1/G2 evidence.
The monitor invalidates changed file identities before full background revalidation;
file/model IO is outside business SQL transactions. Mac development metadata monitoring
is not Windows read-only ACL protection against a malicious administrator.

## Isolated real child QA

`LOCAL_TEST_NAMESPACE_FILE` is allowed only in `test`. It registers an already-created
`wordheat_pytest_<random>` schema in the dedicated test DB with its exact owner/random
comment/installation marker and no runtime CREATE privilege. Each engine checkout
revalidates it and sets only that search_path, with no public fallback. The child never
creates/drops schemas, cannot authorize production and rejects a backup registry pointing
to public. Migration CLI rejects this mode; the parent fixture owns migrations/cleanup.
Real API/scorer child and Native CLI tests use this registration and prove the preexisting
public sentinel remains unchanged. Test resources and fake authorization fixtures are not
formal platform, G1-Q or Windows/OBS evidence.
