"""
Unit tests for scripts/release_backend_candidate.py - the canonical
deploy/rollback gate.

Every test here mocks the three expensive/mutating boundaries
(check_static_contract, run_live_smoke, deploy) so nothing here ever
touches Docker, the network, or the real `docker compose` boundary.
The one thing every test in this file cares about above all else:
deploy() - the ONLY function that can mutate the running stack - must
never be called unless every gate passed AND --deploy was requested.

Run directly:

    python3 scripts/tests/test_release_backend_candidate.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import release_backend_candidate as gate  # noqa: E402


ALL_LIVE_PASS = {
    "LIVE_BACKEND_HEALTH": True,
    "LIVE_DOCUMENT_LIST": True,
    "LIVE_CONTACT_LIST": True,
    "LIVE_CONTACT_PHOTO": True,
    "LIVE_DOCUMENT_DOWNLOAD": True,
    "RELEASE_COMPATIBILITY": True,
}


class RunGatesTests(unittest.TestCase):
    """A: an all-PASS run reports every gate as PASS (deployment WOULD
    be allowed), without this test itself ever calling deploy()."""

    def test_all_pass_reports_every_gate_as_passed(self):
        with patch.object(gate, "check_static_contract", return_value=True), \
             patch.object(gate, "run_live_smoke", return_value=dict(ALL_LIVE_PASS)):
            results = gate.run_gates("candidate:x", "test", 18000)

        self.assertTrue(gate.all_gates_passed(results))
        for name in gate.GATE_NAMES:
            self.assertTrue(results[name], f"{name} should have passed")

    def test_static_contract_failure_short_circuits_before_live_smoke(self):
        """D: a static contract failure must never even attempt the
        (expensive, Docker-based) live gate."""

        live_smoke_mock_calls = []

        def fake_live_smoke(*args, **kwargs):
            live_smoke_mock_calls.append((args, kwargs))
            return dict(ALL_LIVE_PASS)

        with patch.object(gate, "check_static_contract", return_value=False), \
             patch.object(gate, "run_live_smoke", side_effect=fake_live_smoke):
            results = gate.run_gates("candidate:x", "test", 18000)

        self.assertFalse(gate.all_gates_passed(results))
        self.assertFalse(results["STATIC_COMPATIBILITY"])
        self.assertEqual(
            live_smoke_mock_calls,
            [],
            "run_live_smoke must never be called after a static contract failure",
        )

    def test_any_single_live_gate_failure_fails_the_whole_run(self):
        """B: any one FAIL (here: CONTACT_PHOTO) must make the overall
        result not-all-passed, even though every other gate passed."""

        partial_failure = dict(ALL_LIVE_PASS)
        partial_failure["LIVE_CONTACT_PHOTO"] = False
        partial_failure["RELEASE_COMPATIBILITY"] = False

        with patch.object(gate, "check_static_contract", return_value=True), \
             patch.object(gate, "run_live_smoke", return_value=partial_failure):
            results = gate.run_gates("candidate:x", "test", 18000)

        self.assertFalse(gate.all_gates_passed(results))
        self.assertFalse(results["LIVE_CONTACT_PHOTO"])
        self.assertTrue(results["LIVE_BACKEND_HEALTH"])


class RunLiveSmokeParsingTests(unittest.TestCase):
    """C: a smoke process that never produces any status lines at all
    (a genuine crash: Docker unreachable, a missing fixture, killed
    before it got anywhere) must fail closed - every live gate ends up
    FAIL by the time run_gates() is done, because run_gates() itself
    pre-initializes every gate to False and nothing here ever sets one
    to True without a real PASS line to justify it.

    Note what this must NOT do: release_compatibility_smoke.py's own
    exit code is BY DESIGN 1 whenever RELEASE_COMPATIBILITY != PASS -
    that is its normal, documented way of failing a single check, not
    evidence of a crash. Treating "non-zero exit" as "blanket-fail
    every gate" would silently destroy the accurate PASS detail for
    every gate that genuinely passed alongside one real failure - the
    second test below is the regression test for exactly that bug.
    """

    def test_total_crash_with_no_status_lines_fails_every_gate_closed(self):
        fake_result = Mock(stdout="", returncode=1)

        with patch.object(gate.subprocess, "run", return_value=fake_result):
            statuses = gate.run_live_smoke("candidate:x", "test", 18000)

        self.assertEqual(statuses, {})

        # What actually blocks deployment: run_gates()'s own
        # pre-initialized False defaults for any gate that never
        # appeared in the (empty) parsed statuses.
        with patch.object(gate, "check_static_contract", return_value=True), \
             patch.object(gate, "run_live_smoke", return_value={}):
            results = gate.run_gates("candidate:x", "test", 18000)

        self.assertFalse(gate.all_gates_passed(results))
        for name in gate.GATE_NAMES:
            if name != "STATIC_COMPATIBILITY":
                self.assertFalse(results[name])

    def test_legitimate_partial_failure_preserves_accurate_per_gate_results(self):
        """A real RELEASE_COMPATIBILITY=FAIL (like the actual
        candidate-ed292d7 proof: only DOCUMENT_LIST fails) must keep
        reporting the OTHER gates as the real PASS they printed, not
        get blanket-overwritten just because the process's overall
        exit code is non-zero."""

        fake_result = Mock(
            stdout=(
                "BACKEND_HEALTH=PASS\n"
                "  status=ok\n"
                "DOCUMENT_LIST=FAIL\n"
                "  chunk_count too low\n"
                "CONTACT_LIST=PASS\n"
                "CONTACT_PHOTO=PASS\n"
                "DOCUMENT_DOWNLOAD=PASS\n"
                "RELEASE_COMPATIBILITY=FAIL\n"
            ),
            returncode=1,
        )

        with patch.object(gate.subprocess, "run", return_value=fake_result):
            statuses = gate.run_live_smoke("candidate:x", "test", 18000)

        self.assertTrue(statuses["LIVE_BACKEND_HEALTH"])
        self.assertFalse(statuses["LIVE_DOCUMENT_LIST"])
        self.assertTrue(statuses["LIVE_CONTACT_LIST"])
        self.assertTrue(statuses["LIVE_CONTACT_PHOTO"])
        self.assertTrue(statuses["LIVE_DOCUMENT_DOWNLOAD"])
        self.assertFalse(statuses["RELEASE_COMPATIBILITY"])


class MainDeploymentGuardTests(unittest.TestCase):
    """The core safety property: deploy() is called if and only if
    every gate passed AND --deploy was explicitly requested."""

    def _run_main(self, argv, static_ok, live_results):
        with patch.object(gate, "check_static_contract", return_value=static_ok), \
             patch.object(gate, "run_live_smoke", return_value=live_results), \
             patch.object(gate, "deploy") as deploy_mock:
            exit_code = gate.main(argv)

        return exit_code, deploy_mock

    def test_pass_gate_with_deploy_flag_invokes_deploy_exactly_once(self):
        """A: an all-PASS run WITH --deploy is allowed to deploy."""

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:good", "--deploy"],
            static_ok=True,
            live_results=dict(ALL_LIVE_PASS),
        )

        self.assertEqual(exit_code, 0)
        deploy_mock.assert_called_once_with("candidate:good")

    def test_any_fail_with_deploy_flag_never_invokes_deploy(self):
        """B: a FAIL gate, even WITH --deploy requested, must never
        call deploy()."""

        failing = dict(ALL_LIVE_PASS)
        failing["LIVE_DOCUMENT_LIST"] = False
        failing["RELEASE_COMPATIBILITY"] = False

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:bad", "--deploy"],
            static_ok=True,
            live_results=failing,
        )

        self.assertNotEqual(exit_code, 0)
        deploy_mock.assert_not_called()

    def test_smoke_process_failure_with_deploy_flag_never_invokes_deploy(self):
        """C: the live smoke step failing outright (modeled here as
        every live gate reporting FAIL, matching what a non-zero
        subprocess exit produces) must never call deploy(), even with
        --deploy requested."""

        all_fail = {name: False for name in gate._LIVE_SMOKE_NAME_TO_GATE.values()}

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:crashed", "--deploy"],
            static_ok=True,
            live_results=all_fail,
        )

        self.assertNotEqual(exit_code, 0)
        deploy_mock.assert_not_called()

    def test_static_contract_failure_with_deploy_flag_never_invokes_deploy(self):
        """D: a static contract failure, even WITH --deploy requested,
        must never call deploy() (and never even reach run_live_smoke,
        covered separately in RunGatesTests)."""

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:bad-contract", "--deploy"],
            static_ok=False,
            live_results=dict(ALL_LIVE_PASS),
        )

        self.assertNotEqual(exit_code, 0)
        deploy_mock.assert_not_called()

    def test_rollback_image_goes_through_the_identical_gate_with_no_bypass(self):
        """E: passing an OLD/rollback-looking image tag uses the exact
        same run_gates()/deploy() code path - no special-cased
        "trusted old image" branch exists anywhere to skip a gate for
        it. Proven two ways: (1) an old image that FAILS live gates
        (e.g. the historical candidate-ed292d7 class of incompatibility)
        is blocked exactly like any other failing candidate; (2) the
        source has no conditional on the image string at all."""

        failing = dict(ALL_LIVE_PASS)
        failing["LIVE_DOCUMENT_LIST"] = False
        failing["RELEASE_COMPATIBILITY"] = False

        exit_code, deploy_mock = self._run_main(
            ["--image", "le-global-backend:candidate-ed292d7", "--deploy"],
            static_ok=True,
            live_results=failing,
        )

        self.assertNotEqual(exit_code, 0)
        deploy_mock.assert_not_called()

        # Comment/docstring-aware, like the WordPress plugin's own
        # token_get_all()-based guard against wp_nonce_url() - a
        # naive substring check would false-positive on this very
        # docstring's own "no bypass exists" prose.
        import io
        import tokenize

        source = Path(gate.__file__).read_text()
        code_only_tokens = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
        ]
        code_only = " ".join(code_only_tokens)

        self.assertNotRegex(
            code_only,
            r"(trusted|is_rollback|skip_gate|bypass)",
            "no special-cased rollback/trusted-image bypass may exist in this file's CODE",
        )

    def test_validate_only_never_invokes_deploy_even_when_all_pass(self):
        """F: --validate-only must never mutate deployment state, even
        when every gate passes."""

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:good", "--validate-only"],
            static_ok=True,
            live_results=dict(ALL_LIVE_PASS),
        )

        self.assertEqual(exit_code, 0)
        deploy_mock.assert_not_called()

    def test_default_mode_with_neither_flag_never_invokes_deploy(self):
        """F (continued): the default (no --deploy, no --validate-only)
        must be the safe, non-mutating mode."""

        exit_code, deploy_mock = self._run_main(
            ["--image", "candidate:good"],
            static_ok=True,
            live_results=dict(ALL_LIVE_PASS),
        )

        self.assertEqual(exit_code, 0)
        deploy_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
