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

## Deployment rule

A candidate image must satisfy **both** layers before it is deployed:

```
static contract tests (release-compatibility-contract.py)
  +
live compatibility smoke (scripts/release_compatibility_smoke.py)
  = eligible to deploy
```

**A rollback image must satisfy the exact same two gates**, run against
a snapshot of *today's* persisted data - not the data that existed
when that image was last in production. `container healthy != release
compatible`, and `previously deployed successfully != safe rollback
today`: both gates exist because health and prior deployment history
tell you nothing about whether a given image's code can still read
what is on disk *right now*.

This repository has no existing deployment/rollback pipeline script to
wire this into (`scripts/` held no automation before this gate was
added, and `admin/`/`wordpress-plugin/` are empty placeholders) - when
one is built, it must call `scripts/release_compatibility_smoke.py`
(after `scripts/run_release_compatibility_smoke.sh`-style isolated
bootstrap, or an equivalent) and abort on `RELEASE_COMPATIBILITY=FAIL`,
for both a forward deploy and a rollback.

## Rule going forward

Whenever the persisted ContactState schema or the persisted-DOCX contact
area representation changes in a way an OLDER backend version cannot
read, that change is a **one-way forward migration**: every backend
image older than the migration is permanently disqualified as a rollback
target for any document that has been migrated, regardless of how
healthy that older image tested in isolation. Record migrations of this
kind here as they happen.
