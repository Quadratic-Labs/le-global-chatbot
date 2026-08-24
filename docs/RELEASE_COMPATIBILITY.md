# Release / rollback compatibility

## The forbidden assumption

**`GET /health == 200` (or "container health == healthy") does not mean a
backend release or rollback is safe to serve the currently-deployed
WordPress Admin.**

`/health` never touches ContactState, persisted source DOCX files, or the
Admin Contact API's own route/request shape. A backend image can be
completely healthy in isolation and still be functionally incompatible
with the WordPress plugin currently talking to it, or with data already
on disk from a *different* backend generation.

**"This image was previously stable, therefore rolling back to it is
safe" is also forbidden.** A rollback candidate can be stable in
isolation and still be incompatible with the *current* WordPress code or
the *current* persisted application state (ContactState JSON sidecars,
source DOCX files) - both of which keep evolving independently of any
one backend image.

Concrete incident this documents: backend commit `ed292d7` predates
`docx_parser.py`'s canonical-contact-table recognition tier
(`_extract_canonical_table_contacts`, matched by the hidden marker
`LE-GLOBAL-CONTACT-TABLE-V1`) entirely. As of this writing, exactly one
production document (`AU.docx`) has had its persisted source DOCX
rebuilt into that canonical table by a later backend generation.
Direct, live proof (see "Live gate" below) shows the actual failure
mode is **narrower and quieter** than "mutations fail structurally":

- `list_contacts()`, `get_document_download()`, and the contact-photo
  route are **byte-for-byte identical** between `ed292d7` and the
  current backend (verified directly by diffing both images' `app/`
  trees) - a live run against a real isolated `ed292d7` candidate
  shows all three continue to return HTTP 200 with correct content,
  even for the one canonical-table document.
- The actual, reproducible incompatibility is in
  **`document_chunk_builder.py`'s indexing-time contact extraction**:
  `build_document_chunks_from_docx()` calls
  `extract_contacts_from_docx()`, which on `ed292d7` falls straight to
  the legacy floating-shape parser (finding zero contacts, since the
  canonical table has no floating shape to find) instead of the new
  canonical-table tier - so indexing `AU.docx` under `ed292d7` silently
  builds **one fewer chunk** than the current backend (59 vs. 60 in the
  live proof below). No HTTP request ever fails; a chatbot/RAG query
  about that country's contact would just get worse grounding context
  on the older image, with nothing in any Admin API response
  announcing it.

`/health` cannot see any of this: the incompatibility is in the *data
format contract*, not in either version's own internal correctness,
and it does not manifest as an HTTP error on any of the four Admin
routes WordPress calls - only as a quietly incomplete search index.

## Required smoke gates before trusting a release or rollback

A release or rollback must not be declared successful, and a rollback
image must not be labelled a safe rollback candidate, until **all** of
these pass:

```
BACKEND_HEALTH=PASS
DOCUMENT_LIST=PASS
CONTACT_LIST=PASS
CONTACT_PHOTO=PASS
DOCUMENT_DOWNLOAD=PASS
```

Every gate must be **read-only**: no document/contact/photo mutation,
no reindex, no reseed, no OpenSearch state change.

## What currently enforces this

### Static contract (runs with no live backend, on every commit)

`wordpress/le-global-chatbot/tests/release-compatibility-contract.py`
proves, from both sides' own real source, that the WordPress Admin's
request construction for the four gated routes (list documents, list
contacts, contact photo, document download) matches the backend's own
route registration exactly - the same "verify both sides of a contract
from source, in one monorepo, without a live call" convention already
used by `test-contact-photo-crud-contract.py`'s
`test_photo_urls_use_the_same_documents_path_prefix_as_contacts`, paired
with the backend's own `test_photo_route_paths_share_the_documents_prefix`.

Run it with:

```
cd /opt/le-global-chatbot
python3 wordpress/le-global-chatbot/tests/release-compatibility-contract.py -v
```

**What this proves:** the four routes exist, at the paths/methods
WordPress expects.

**What this does NOT prove** (known limitation, not silently assumed):
request/response *body* schema drift that a route rename would not
cause, any behavioral difference in business logic once a request
reaches the route, or - the specific class of incident this document
opened with - whether an OLDER backend candidate can correctly read or
mutate data already written in a NEWER on-disk format (ContactState
schema, persisted-DOCX contact-area representation). A route staying at
the same path proves nothing about whether the code behind it still
understands today's data.

### Live gate (requires a reachable candidate backend)

`scripts/release_compatibility_smoke.py` makes the same five checks as
real, read-only HTTP requests against a running candidate, using
exactly the request shape WordPress itself uses (`request_backend()`
in `class-le-global-chatbot-admin.php`: `X-API-Key`/`X-Admin-Key`
headers, GET, JSON body):

1. `GET /health` -> 200, `status: "ok"`, every dependency `"ok"`
   (`BACKEND_HEALTH`)
2. `GET /api/v1/admin/documents` -> 200, every manifest document
   present, **and** `chunk_count` at or above the known-good baseline
   recorded in the manifest for the canonical-table fixture document
   (`DOCUMENT_LIST`) - this last assertion is the one that actually
   catches the `ed292d7`-class incompatibility; see "Why a 200 is not
   enough" below.
3. `GET /api/v1/admin/documents/{id}/contacts` -> 200, the expected
   contacts and `has_photo` values, for every fixture document
   (`CONTACT_LIST`)
4. `GET /api/v1/admin/documents/{id}/contacts/{contact_id}/photo` ->
   200, non-empty body, `image/*` content type, for every fixture
   contact flagged `has_photo: true` (`CONTACT_PHOTO`)
5. `GET /api/v1/admin/documents/{id}/download` -> 200, non-empty body,
   the DOCX media type, and a valid ZIP container with
   `[Content_Types].xml` present, for every fixture document
   (`DOCUMENT_DOWNLOAD`)

It never uploads, reindexes, deletes, or mutates anything, and it never
imports from `app.*` - only `urllib`, exercising the HTTP boundary
itself.

**Fixtures.** `scripts/fixtures/release_compatibility_manifest.json`
lists only non-secret identifiers (document_id, country_code,
source_filename, contact_id, has_photo, an expected minimum
chunk_count) for four real, currently-active production documents,
chosen to cover: a document with the new canonical contact table
(`AU`), a document still in the legacy floating-shape format (`CA`), a
document with more than one contact (`BE`), and a contact with no
photo configured (`CL`). No document bytes, photo bytes, or credentials
are ever committed to the repository.

**Running it end to end**, from a snapshot of real (never
fabricated) current-format data, without ever writing to production or
touching production's own OpenSearch/Redis/network:

```sh
cd /opt/le-global-chatbot

# 1. Copy ONLY the manifest's documents/contacts/photos out of
#    production - read-only with respect to production, the output
#    directory must not already exist.
sudo python3 scripts/prepare_release_compatibility_snapshot.py \
  --production-source-dir /var/lib/le-global-chatbot/documents/source \
  --manifest scripts/fixtures/release_compatibility_manifest.json \
  --output-dir /var/tmp/le-global-smoke-fixture/documents/source
sudo mkdir -p /var/tmp/le-global-smoke-fixture/documents/processed
sudo chown -R 10001:10001 /var/tmp/le-global-smoke-fixture

# 2. Spin up an ISOLATED network + OpenSearch + Redis + the candidate
#    image, bootstrap the isolated OpenSearch from the snapshot (the
#    candidate's OWN scripts/index_docx_corpus.py, against its own
#    throwaway OpenSearch only), then run the smoke gate against it.
#    Tears itself down automatically on exit.
scripts/run_release_compatibility_smoke.sh <image-tag> <label> <host-port>
```

`RELEASE_SMOKE_SNAPSHOT_DIR` overrides the snapshot location if not
using the default above.

**Why a 200 is not enough.** The `ed292d7` incompatibility (see
above) never produces an HTTP error on any of these four routes - same
schema, same status code, same route. `DOCUMENT_LIST`'s chunk_count
assertion is what actually distinguishes a candidate whose docx parser
can read the current canonical-table format from one that silently
cannot, because that is the one place this specific divergence is
visible from outside the process at all.

#### Empirical proof (ran 2026-08-23, against this exact snapshot)

```
$ scripts/run_release_compatibility_smoke.sh le-global-backend:candidate-a3cb8e1 a3cb8e1 18011
BACKEND_HEALTH=PASS
DOCUMENT_LIST=PASS
CONTACT_LIST=PASS
CONTACT_PHOTO=PASS
DOCUMENT_DOWNLOAD=PASS
RELEASE_COMPATIBILITY=PASS

$ scripts/run_release_compatibility_smoke.sh le-global-backend:candidate-ed292d7 ed292d7 18012
BACKEND_HEALTH=PASS
DOCUMENT_LIST=FAIL
  doc_d600fa6a...4157 (AU): chunk_count=59, expected >= 60
CONTACT_LIST=PASS
CONTACT_PHOTO=PASS
DOCUMENT_DOWNLOAD=PASS
RELEASE_COMPATIBILITY=FAIL
```

`candidate-ed292d7` indexed the identical snapshot with one fewer
chunk for `AU` (59 vs. 60) than `candidate-a3cb8e1`, and only for
`AU` - `CA`/`BE`/`CL` (none of which use the canonical table) indexed
identically on both images. This is the live, reproducible signal that
would have caught the historical rollback before it reached
production. Both live stacks were isolated (their own Docker network,
their own throwaway OpenSearch/Redis, the snapshot mounted read-only)
and torn down automatically; production's own containers, network,
OpenSearch, Redis, and documents directory were never touched by
either run (verified directly: identical `stat` mtimes on every
production source file referenced by the manifest, before and after
both runs).

## Deployment rule - now mechanically enforced

`scripts/release_backend_candidate.py` is the **one** repository-owned
entry point allowed to deploy or roll back `le-global-backend`. It
exists because, before it, every deploy/rollback found in this
investigation was an ad hoc, hand-written docker-compose override
applied directly against `infra/compose.yml` with nothing in between -
see the historical `/tmp/le-global-*.yml`,
`/tmp/le-global-contact-rollback.yml`,
`/tmp/le-global-emergency-rollback.yml`,
`/tmp/le-global-restore-a3cb8e1.yml` files this investigation
recovered from the actual incident. That gap - a real deploy path with
no compatibility gate on it at all - is how a genuinely incompatible
image could reach production in the first place.

```
candidate image
     |
     v
STATIC_COMPATIBILITY   (release-compatibility-contract.py)
     |
     v
LIVE_BACKEND_HEALTH / LIVE_DOCUMENT_LIST / LIVE_CONTACT_LIST /
LIVE_CONTACT_PHOTO / LIVE_DOCUMENT_DOWNLOAD
     (scripts/run_release_compatibility_smoke.sh - isolated network +
      OpenSearch + Redis + the candidate, snapshot mounted read-only)
     |
     v
RELEASE_COMPATIBILITY=PASS
     |
     v
deployment allowed -> docker compose (ONLY with --deploy; never with
                       --validate-only, which is also the default)
```

If any gate is not PASS, `deploy()` - the only function in this
script that ever calls `docker compose` - is never invoked at all,
whether or not `--deploy` was requested. There is exactly **one** code
path, used identically for a forward deploy and a rollback: passing an
older image tag does not skip, shortcut, or weaken a single gate.
`container healthy != release compatible`, and `previously deployed
successfully != safe rollback today` are not just documented anymore -
they're the reason this script has no branch that treats an "old,
previously-stable" image any differently from a brand-new one.

```sh
# Check whether an image would be allowed to deploy - never touches
# the running stack.
python3 scripts/release_backend_candidate.py \
    --image le-global-backend:candidate-abc123 --validate-only

# Validate, and ONLY if every gate passes, actually apply that image
# to the running stack (forward deploy or rollback - same command).
python3 scripts/release_backend_candidate.py \
    --image le-global-backend:candidate-abc123 --deploy
```

Empirical proof this blocks a real regression (ran 2026-08-23, same
snapshot as above): `--deploy --image le-global-backend:candidate-ed292d7`
reported `LIVE_DOCUMENT_LIST=FAIL` / `RELEASE_COMPATIBILITY=FAIL` /
`DEPLOYMENT_GATE_ENFORCED=YES` and exited non-zero; `docker inspect
le-global-backend` showed an identical image digest and container
`Created` timestamp before and after the run - `docker compose` was
never invoked. The equivalent run against `candidate-a3cb8e1` reported
every gate `PASS`.

**Status labels, kept unambiguous on purpose:** `STATIC_*` names are
the source-only Layer 1 checks; `LIVE_*` names are real HTTP calls
against an isolated running candidate (Layer 2); `RELEASE_COMPATIBILITY`
is the combined verdict; `DEPLOYMENT_GATE_ENFORCED` /
`ROLLBACK_GATE_ENFORCED` describe this script's own mechanical
behavior (verified by `scripts/tests/test_release_backend_candidate.py`),
never a documented-but-manual rule. Only this section, about this
specific script, may claim "enforced" - everywhere else in this
document that describes a *rule* rather than *this script's tested
behavior*, "documented" is the correct word.

## The unresolved Contact 400 - separate from the chunk-count regression

Real production evidence (`docker logs le-global-wordpress`, Apache
combined log, 2026-08-23 00:11-00:21 UTC) shows `list_contacts`
requests genuinely returning HTTP 400 for several different
document_ids, including ones (e.g. `AR`) that never touch the
canonical-table code path at all. **This is a different, still
unresolved symptom from the `ed292d7` chunk-count regression proven
above - do not treat the chunk-count fix as having explained it.**

What the investigation established, by directly reading every
candidate code path and empirically testing the live boundary (never
guessed):

- `relay_json_result()` in `class-le-global-chatbot-admin.php` only
  ever calls `wp_send_json_error($detail, $status_code)` with
  `$status_code` set to whatever `wp_remote_retrieve_response_code()`
  actually received - so the observed 400 is a REAL status code some
  server sent, not a WordPress-side default or misreport.
- Ruled out by direct code reading (both `candidate-a3cb8e1` and
  `candidate-ed292d7` - `admin_contacts.py`, `admin_contact_photos.py`,
  `admin_document_lifecycle.py`, `app/main.py`'s exception handler and
  `ApiProtectionMiddleware` are all byte-identical or read in full):
  no code path in either backend version's Contact service, routing,
  global exception handling, or auth/rate-limit middleware can produce
  a bare HTTP 400 for this endpoint - only 422/404/409/429/502/503.
- Ruled out by direct code reading of the WordPress plugin: nonce
  rejection (`check_ajax_referer`) dies with a bare `"-1"` body and
  HTTP 200, never 400; `read_document_id_for_json()`'s own validation
  uses 422; a transport/DNS failure or a non-JSON backend body both
  map to WP_Error -> HTTP 503 in `relay_json_result()`, never 400.
- Ruled out empirically: a raw, deliberately malformed HTTP request
  sent directly to the live backend confirmed uvicorn's own
  protocol-level 400 has a `text/plain` body ("Invalid HTTP request
  received.") - which `json_decode()` rejects, so THAT class of 400
  would surface to WordPress as 503, not 400, and cannot be the
  observed mechanism either.
- Ruled out: a network-alias collision with the one identifiable
  leftover container on `le-global-network`
  (`le-global-backend-candidate-2b4f669`) - it has no `backend` alias
  registered, now or (given it was never recreated) at the incident
  time.
- The docker/journal timeline (reconstructed from `docker inspect`'s
  `com.docker.compose.project.config_files` label plus
  `journalctl -u docker`'s `sbJoin`/`stopping restart-manager` events
  for `le-global-backend`) shows the FIRST 400 at 00:16:36 occurred
  against a container recreated at 00:00:27 - a deployment with no
  surviving override file - roughly **3 minutes before** the
  `candidate-ed292d7` "emergency rollback" redeploy at 00:19:49, and
  400s continued for at least 2 minutes AFTER that redeploy too (e.g.
  00:21:15). The symptom therefore spans a container recreation and is
  not specific to `candidate-ed292d7`'s code.
- `request_backend()` never logged anything for a successful-but-error
  HTTP round trip before this investigation (only for a hard transport
  failure) - confirmed by an empty PHP error log for the entire
  incident window. This is why the real status code could not be
  recovered from application logs and had to be reconstructed from the
  Apache access log instead.

```
CONTACT_400_ROOT_CAUSE=UNRESOLVED
```

Every hypothesis this investigation could construct from the available
code and configuration was tested and eliminated; none of them survive
contact with the evidence. The most defensible open candidates - a
resource/concurrency condition during rapid, back-to-back manual
testing of many document_ids and photo downloads right after each
redeploy, or an HTTP/1.1 connection-reuse desync on a keep-alive
connection between WordPress and the backend - are plausible but
**not proven**, and are not claimed as the cause here. If this
recurs, the observability change below (`request_backend()` now logs
the real status code, method, and path for every non-2xx response, and
separately flags a non-JSON body) should make the real mechanism
immediately visible in the PHP error log, instead of requiring this
kind of multi-hour forensic reconstruction again.

## Observability: distinguishing backend failure classes

`request_backend()` (`class-le-global-chatbot-admin.php`) now logs,
for every proxied action, via PHP's own `error_log()`:

- **A non-2xx HTTP response** (400/401/403/404/500/502/...): the real
  status code, HTTP method, and path - e.g. `Backend returned a
  non-success status (400) for GET
  /api/v1/admin/documents/doc_.../contacts.`
- **A non-JSON response body**: the same method/path plus the real
  status code that came with the unparseable body.
- **A transport/DNS/connect failure** (already logged before this
  change): the WP_Error code from `wp_remote_request()`.

None of these log lines ever include `X-Admin-Key`, `X-API-Key`, or
response/document body content - only method, path (which contains
only an opaque `document_id`, never document content), and the status
code. A normal 2xx response logs nothing, so there is no added noise
on the success path. WordPress's own nonce rejection
(`check_ajax_referer`) remains self-diagnosing without a code change:
its distinctive `"-1"`-body, HTTP-200 signature is already
unambiguous in the Apache access log next to a genuine backend error.
Covered by `wordpress/le-global-chatbot/tests/admin-contacts.test.php`.

## Rule going forward

Whenever the persisted ContactState schema or the persisted-DOCX contact
area representation changes in a way an OLDER backend version cannot
read, that change is a **one-way forward migration**: every backend
image older than the migration is permanently disqualified as a rollback
target for any document that has been migrated, regardless of how
healthy that older image tested in isolation. Record migrations of this
kind here as they happen.
