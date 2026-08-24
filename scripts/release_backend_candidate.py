"""
Canonical, repository-owned release/rollback entry point for a backend
candidate image.

This is the ONE place a deploy or rollback of `le-global-backend` is
allowed to happen from. It exists because, before this file, deploys
and rollbacks were done ad hoc - a docker-compose override file
written by hand (see the historical `/tmp/le-global-*.yml`,
`/tmp/le-global-contact-rollback.yml`,
`/tmp/le-global-emergency-rollback.yml` files this investigation
recovered) and applied directly with no compatibility gate in between.
That is precisely how a genuinely incompatible image class (see
docs/RELEASE_COMPATIBILITY.md) could reach production at all: nothing
stopped it.

Pipeline, every stage required, in this order:

    candidate image
         |
         v
    STATIC_COMPATIBILITY   (release-compatibility-contract.py - no live backend needed)
         |
         v
    LIVE_BACKEND_HEALTH    \\
    LIVE_DOCUMENT_LIST      \\  scripts/run_release_compatibility_smoke.sh -
    LIVE_CONTACT_LIST       /   isolated network + OpenSearch + Redis +
    LIVE_CONTACT_PHOTO     /    the candidate, snapshot mounted read-only
    LIVE_DOCUMENT_DOWNLOAD /
         |
         v
    RELEASE_COMPATIBILITY=PASS
         |
         v
    deployment allowed  ->  deploy() (only with --deploy; never with --validate-only)

If ANY gate is not PASS, the pipeline stops there and `deploy()` is
NEVER called - not "called and rejected", never invoked at all. There
is exactly one code path for this, used identically for a forward
deploy and for a rollback: passing an older image tag does not skip,
shortcut, or weaken any gate. "This image was previously stable" is
not a bypass this script recognizes (see docs/RELEASE_COMPATIBILITY.md
for why that assumption is the one this whole gate exists to forbid).

Usage:

    # Check whether `image` would be allowed to deploy - never touches
    # the running stack.
    python3 scripts/release_backend_candidate.py \\
        --image le-global-backend:candidate-abc123 \\
        --validate-only

    # Validate, and only if every gate passes, actually deploy (or
    # roll back to) that image via infra/compose.yml.
    python3 scripts/release_backend_candidate.py \\
        --image le-global-backend:candidate-abc123 \\
        --deploy

--validate-only is the default when neither flag is given, so an
accidental bare invocation can never deploy anything.

Secrets: this script itself takes no credentials at all (the isolated
live smoke stack it drives uses its own throwaway test credentials,
never production's) and never prints anything but image tags, gate
names, and PASS/FAIL - see scripts/release_compatibility_smoke.py for
the redaction discipline the live gate itself applies to its own
diagnostic output.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_CONTRACT_SCRIPT = (
    REPO_ROOT
    / "wordpress/le-global-chatbot/tests/release-compatibility-contract.py"
)
LIVE_SMOKE_SCRIPT = REPO_ROOT / "scripts" / "run_release_compatibility_smoke.sh"
COMPOSE_FILE = REPO_ROOT / "infra" / "compose.yml"

GATE_NAMES = (
    "STATIC_COMPATIBILITY",
    "LIVE_BACKEND_HEALTH",
    "LIVE_DOCUMENT_LIST",
    "LIVE_CONTACT_LIST",
    "LIVE_CONTACT_PHOTO",
    "LIVE_DOCUMENT_DOWNLOAD",
    "RELEASE_COMPATIBILITY",
)

_LIVE_SMOKE_NAME_TO_GATE = {
    "BACKEND_HEALTH": "LIVE_BACKEND_HEALTH",
    "DOCUMENT_LIST": "LIVE_DOCUMENT_LIST",
    "CONTACT_LIST": "LIVE_CONTACT_LIST",
    "CONTACT_PHOTO": "LIVE_CONTACT_PHOTO",
    "DOCUMENT_DOWNLOAD": "LIVE_DOCUMENT_DOWNLOAD",
    "RELEASE_COMPATIBILITY": "RELEASE_COMPATIBILITY",
}

_STATUS_LINE = re.compile(r"^([A-Z_]+)=(PASS|FAIL)\s*$")


def check_static_contract() -> bool:
    """LAYER 1: source-only WordPress<->backend route contract.

    No live backend, no Docker, no network call - a plain unittest
    run. Kept as its own function so tests can mock it independently
    of the (expensive, Docker-based) live gate below.
    """

    result = subprocess.run(
        [sys.executable, str(STATIC_CONTRACT_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    return result.returncode == 0


def run_live_smoke(image: str, label: str, host_port: int) -> dict[str, bool]:
    """LAYER 2: the live, read-only, isolated compatibility smoke gate.

    Delegates entirely to scripts/run_release_compatibility_smoke.sh,
    which owns the isolated Docker network/OpenSearch/Redis/candidate
    lifecycle and its own teardown - this function only runs it as a
    subprocess and parses its deterministic PASS/FAIL status lines.

    A non-zero exit from the smoke process is the NORMAL, designed way
    it signals "RELEASE_COMPATIBILITY != PASS" (see
    release_compatibility_smoke.py's own main()) - it does not by
    itself mean every individual gate failed, and must not be used to
    overwrite gates that DID print PASS. What actually blocks
    deployment when the process crashes outright (Docker unreachable,
    a missing fixture, no status lines printed at all) is that any
    gate whose line never appears here is left absent from this dict,
    and run_gates() pre-initializes every gate to False - so a total
    crash still fails closed, without this function ever needing to
    guess at gates it has no real information about.
    """

    result = subprocess.run(
        [str(LIVE_SMOKE_SCRIPT), image, label, str(host_port)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    statuses: dict[str, bool] = {}

    for line in result.stdout.splitlines():
        match = _STATUS_LINE.match(line.strip())

        if match is None:
            continue

        name, status = match.group(1), match.group(2)
        gate_name = _LIVE_SMOKE_NAME_TO_GATE.get(name)

        if gate_name is not None:
            statuses[gate_name] = status == "PASS"

    return statuses


def deploy(image: str) -> None:
    """
    Actually mutate the running stack - the ONE function this entire
    script exists to gate. Never call this directly; go through
    main()/run_gates(), which only reaches this call when every gate
    in GATE_NAMES has already passed.

    Writes a minimal image-only override (the same shape as every
    historical /tmp/le-global-*.yml override this investigation
    found) and applies it with docker compose, exactly the convention
    already used for le-global-backend (see
    `docker inspect le-global-backend`'s own
    com.docker.compose.project.config_files label).
    """

    override_content = f"services:\n  backend:\n    image: {image}\n"

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yml",
        prefix="le-global-release-",
        delete=False,
    ) as override_file:
        override_file.write(override_content)
        override_path = override_file.name

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-f",
            override_path,
            "up",
            "-d",
            "backend",
        ],
        cwd=COMPOSE_FILE.parent,
        check=True,
    )


def run_gates(
    image: str,
    label: str,
    host_port: int,
) -> dict[str, bool]:
    """
    Run every required gate in order, short-circuiting as soon as one
    fails - never spends the several minutes the live Docker stack
    takes when the free, instant static contract has already failed.
    """

    results: dict[str, bool] = {name: False for name in GATE_NAMES}

    results["STATIC_COMPATIBILITY"] = check_static_contract()

    if not results["STATIC_COMPATIBILITY"]:
        return results

    live_results = run_live_smoke(image, label, host_port)

    for gate_name in GATE_NAMES:
        if gate_name in live_results:
            results[gate_name] = live_results[gate_name]

    return results


def all_gates_passed(results: dict[str, bool]) -> bool:
    return all(results.get(name, False) for name in GATE_NAMES)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--image",
        required=True,
        help="Candidate backend image tag to validate (and, with "
        "--deploy, to switch the running stack to). Used identically "
        "for a forward deploy or a rollback - there is no separate "
        "rollback mode.",
    )
    parser.add_argument(
        "--label",
        default="release-gate",
        help="Label for the isolated Docker resources the live gate "
        "creates (default: release-gate). Use a distinct label per "
        "concurrent validation run.",
    )
    parser.add_argument(
        "--host-port",
        type=int,
        default=18000,
        help="Host port to publish the isolated candidate on during "
        "the live gate (default: 18000). Never the production port.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help="Run every gate and report the result. Never deploys "
        "anything. This is the default when neither flag is given.",
    )
    mode.add_argument(
        "--deploy",
        action="store_true",
        help="Run every gate, and ONLY if all of them pass, apply "
        "--image to the running stack via infra/compose.yml. Any gate "
        "failure leaves the running stack untouched.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)

    results = run_gates(
        image=arguments.image,
        label=arguments.label,
        host_port=arguments.host_port,
    )

    for gate_name in GATE_NAMES:
        print(f"{gate_name}={'PASS' if results[gate_name] else 'FAIL'}")

    if not all_gates_passed(results):
        print("DEPLOYMENT_GATE_ENFORCED=YES")
        print(
            "RELEASE_COMPATIBILITY != PASS - deployment blocked, "
            "docker compose was never invoked.",
            file=sys.stderr,
        )
        return 1

    if arguments.deploy:
        deploy(arguments.image)
        print("DEPLOYED=YES")
    else:
        print("DEPLOYED=NO")
        print("(--validate-only: every gate passed, nothing was deployed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
