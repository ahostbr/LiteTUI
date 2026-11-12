"""Synthetic checker controls only: no frontend, provider, clipboard or device IO.

A green synthetic record tests verifier logic, never live acceptance or an
actual measured render-delay injection. All artifacts live in temporary dirs.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

MODULE = Path(__file__).resolve().parents[1] / "e2e/rust_frontend_acceptance.py"
SPEC = importlib.util.spec_from_file_location("rust_frontend_acceptance", MODULE)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PRODUCT = "c" * 40


class AcceptanceChecker(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0
        self.write("ledger.md", "SYNTHETIC CHECKER TEST ledger, not live evidence")
        self.write("src/example.py", "# SYNTHETIC CHECKER TEST source\n")

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def document(self, name, value):
        return self.write(name, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def pin(self, path):
        return {"path": str(path.relative_to(self.root)), "sha256": gate.sha(path)}

    def ref(self, actual, cover, kind="unit", host=None, observer=None, pointer=""):
        self.counter += 1
        path = self.document(f"proof-{self.counter}.json", actual)
        provenance = {
            "kind": kind,
            "product_revision": PRODUCT,
            "covers": [cover],
            "description": "SYNTHETIC CHECKER TEST; no live observation",
        }
        if host is not None:
            provenance["host"] = host
        if observer is not None:
            provenance["observer"] = observer
        return {
            **self.pin(path),
            "pointer": pointer,
            "expected": gate.pointer(actual, pointer),
            "provenance": provenance,
        }

    def matrix(self):
        rows = []
        for name in gate.REQUIRED_IDS:
            positive = self.ref({"observed": True}, name, pointer="/observed")
            negative = self.ref({"effects": []}, name, pointer="/effects")
            terminals: dict[str, Any] = {
                host: {"status": "shared", "gate": "P4"} for host in gate.HOSTS
            }
            if name == "P4":
                terminals = {
                    host: {
                        "status": "passed",
                        "evidence": [
                            self.ref(
                                {"observed": True},
                                name,
                                "host_observation",
                                host,
                                pointer="/observed",
                            ),
                            self.ref(
                                {
                                    "type": "visual_signoff",
                                    "observer": "Ryan",
                                    "approved": True,
                                    "terminal": host,
                                    "product_revision": PRODUCT,
                                    "scope": "SYNTHETIC checker tests",
                                    "covered_ids": list(gate.REQUIRED_IDS),
                                },
                                name,
                                "human_signoff",
                                host,
                                "Ryan",
                            ),
                        ],
                    }
                    for host in gate.HOSTS
                }
                for record in terminals.values():
                    for ref in record["evidence"]:
                        ref["provenance"]["covers"] = list(gate.REQUIRED_IDS)
            rows.append(
                {
                    "id": name,
                    "source_revision": PRODUCT,
                    "source_paths": ["src/example.py"],
                    "entry_action": "SYNTHETIC action",
                    "preconditions": ["SYNTHETIC fixture"],
                    "request": {"kind": "none", "reason": "synthetic"},
                    "response": "Synthetic observed result",
                    "events": ["synthetic"],
                    "state_owner": "synthetic",
                    "rust_surface": "synthetic",
                    "positive_check": {
                        "description": "Synthetic positive",
                        "status": "passed",
                        "evidence": [positive],
                    },
                    "negative_probe": {
                        "description": "Synthetic refusal",
                        "status": "passed",
                        "evidence": [negative],
                    },
                    "terminals": terminals,
                    "status": "passed",
                    "evidence": [],
                }
            )
        return {
            "schema_version": 1,
            "kind": "rust_frontend_acceptance",
            "product_revision": PRODUCT,
            "ledger": self.pin(self.root / "ledger.md"),
            "source_hashes": {"src/example.py": gate.sha(self.root / "src/example.py")},
            "rows": rows,
        }

    def closed_p0(self, matrix):
        source = self.write(
            "closure.md",
            "SYNTHETIC quote: P0 phase closed; numerical approval remains P4.",
        )
        decision = {
            "type": "phase_closure",
            "phase": "P0",
            "observer": "Ryan",
            "performance_accepted": False,
            "scope": "Only P0 phase; performance remains P4",
            "quote": source.read_text(encoding="utf8"),
            "source": self.pin(source),
        }
        row = next(r for r in matrix["rows"] if r["id"] == "P0")
        row.update(
            status="closed_by_user",
            positive_check=None,
            negative_probe=None,
            decision=self.ref(decision, "P0", "human_decision", observer="Ryan"),
        )
        return row

    def lifecycle(self, host=gate.HOSTS[0]):
        runs = []
        for index, fault in enumerate(sorted(gate.FAULTS)):
            run = {
                "id": f"synthetic-{index}",
                "terminal": host,
                "fault": fault,
                "checks": dict.fromkeys(gate.LIFECYCLE_CHECKS, True),
            }
            run["evidence"] = self.ref(
                copy.deepcopy(run), "lifecycle", "lifecycle", host
            )
            runs.append(run)
        return {
            "schema_version": 1,
            "kind": "lifecycle_results",
            "product_revision": PRODUCT,
            "runs": runs,
        }

    def performance(self, frontend):
        runs = []
        for host in gate.HOSTS:
            context = {
                "hardware": "SYNTHETIC hardware",
                "fixture_sha256": "a" * 64,
                "fixture_messages": 10000,
                "provider_boundary": "SYNTHETIC controlled provider",
                "terminal": {
                    "host": host,
                    "version": "SYNTHETIC",
                    "columns": 120,
                    "rows": 40,
                    "font": "SYNTHETIC",
                    "dpi": 96,
                },
                "background_load": {"description": "SYNTHETIC idle"},
                "workload": "SYNTHETIC same workload",
                "resource_scope": "frontend_and_owned_python",
                "shared_model_services": {"count": 0},
            }
            for phase, count in (("launch", 5), ("steady", 3)):
                for index in range(count):
                    cache = "cold" if index == 0 else "warm"
                    names = gate.METRICS[:1] if phase == "launch" else gate.METRICS[1:]
                    metrics = {}
                    for metric in names:
                        value: float = {
                            "first_editable_ms": 80,
                            "input_render_ms": 50,
                            "scroll_10000_ms": 60,
                            "cpu_idle_percent": 1,
                            "cpu_stream_percent": 20,
                            "rss_peak_bytes": 100000000,
                        }[metric]
                        if frontend == "rust":
                            value *= 0.8
                        observation: dict[str, Any] = {"value": value}
                        if metric.endswith("_ms"):
                            observation.update(
                                method="decoded_host_cells",
                                start_ns=1000000000,
                                end_ns=1000000000 + int(value * 1000000),
                            )
                        metrics[metric] = observation
                    run = {
                        "id": f"{frontend}-{host}-{phase}-{index}",
                        "terminal": host,
                        "cache": cache,
                        "cache_basis": "observed",
                        "cache_procedure": "SYNTHETIC observed cache setup",
                        "phase": phase,
                        "context": copy.deepcopy(context),
                        "metrics": metrics,
                    }
                    run["evidence"] = self.ref(
                        copy.deepcopy(run),
                        "performance",
                        "performance_measurement",
                        host,
                    )
                    runs.append(run)
        return {
            "schema_version": 1,
            "kind": "performance_samples",
            "frontend": frontend,
            "product_revision": PRODUCT,
            "completed_at": "2026-11-11T12:00:00+00:00",
            "runs": runs,
        }

    def perf_bundle(self):
        baseline, results = self.performance("textual"), self.performance("rust")
        path = self.document("baseline.json", baseline)
        limits = [
            {
                "terminal": host,
                "cache": cache,
                "metric": metric,
                "statistic": "median",
                "max_ratio": 1,
            }
            for host in gate.HOSTS
            for cache in ("cold", "warm")
            for metric in gate.METRICS
        ]
        approval = {
            "type": "performance_threshold_approval",
            "observer": "Ryan",
            "approved": True,
            "baseline_sha256": gate.sha(path),
            "limits_sha256": hashlib.sha256(gate.canonical(limits)).hexdigest(),
            "approved_at": "2026-11-11T13:00:00+00:00",
        }
        thresholds = {
            "schema_version": 1,
            "kind": "performance_thresholds",
            "product_revision": PRODUCT,
            "baseline": self.pin(path),
            "limits": limits,
            "approval": self.ref(
                approval, "performance", "human_decision", observer="Ryan"
            ),
        }
        before = self.write("injection/before.txt", "SYNTHETIC render before")
        after = self.write("injection/after.txt", "SYNTHETIC render after 900ms delay")
        control = {
            "type": "render_delay_control",
            "terminal": gate.HOSTS[0],
            "cache": "cold",
            "metric": "input_render_ms",
            "injection_target": "frontend_render",
            "injection": {
                "before": self.pin(before),
                "after": self.pin(after),
                "added_delay_ms": 900,
                "description": "SYNTHETIC unit control; no actual frontend injection",
            },
            "baseline_sha256": gate.sha(path),
            "limits_sha256": approval["limits_sha256"],
            "observation": {
                "value": 950,
                "method": "decoded_host_cells",
                "start_ns": 1000000000,
                "end_ns": 1950000000,
            },
            "gate_exit_code": 1,
        }
        control["evidence"] = self.ref(
            copy.deepcopy(control),
            "performance",
            "performance_measurement",
            gate.HOSTS[0],
        )
        results["render_delay_control"] = control
        return baseline, results, thresholds, path

    def rebind_run(self, run):
        run["evidence"] = self.ref(
            {k: copy.deepcopy(v) for k, v in run.items() if k != "evidence"},
            "performance",
            "performance_measurement",
            run["terminal"],
        )

    def cli(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gate.main([*args, "--evidence-root", str(self.root)])
        return code, json.loads(output.getvalue())

    def test_complete_synthetic_matrix_requires_both_checks_and_shared_hosts(self):
        result = gate.validate_matrix(self.matrix(), self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(result["required_rows"], 38)

    def test_deleted_evidence_and_wrong_typed_expectation_fail(self):
        matrix = self.matrix()
        ref = matrix["rows"][0]["positive_check"]["evidence"][0]
        ref["expected"] = 1  # True must not coerce to one.
        self.assertFalse(gate.validate_matrix(matrix, self.root)["schema_valid"])
        (self.root / ref["path"]).unlink()
        self.assertFalse(gate.validate_matrix(matrix, self.root)["schema_valid"])

    def test_missing_duplicate_and_unknown_required_row_fail(self):
        for mutation in ("missing", "duplicate", "unknown"):
            matrix = self.matrix()
            if mutation == "missing":
                matrix["rows"].pop()
            elif mutation == "duplicate":
                matrix["rows"][-1] = copy.deepcopy(matrix["rows"][0])
            else:
                matrix["rows"][-1]["id"] = "B99"
            with self.subTest(mutation=mutation), self.assertRaises(gate.Invalid):
                gate.validate_matrix(matrix, self.root)

    def test_pending_report_is_honest_and_complete_mode_fails(self):
        matrix = self.matrix()
        matrix["rows"][0]["status"] = "skipped"
        path = self.document("matrix.json", matrix)
        code, result = self.cli(["--matrix", str(path)])
        self.assertEqual(code, 0)
        self.assertFalse(result["passed"])
        code, _ = self.cli(["--matrix", str(path), "--require-complete"])
        self.assertEqual(code, 1)

    def test_extra_evidence_never_replaces_positive_or_negative_check(self):
        for field in ("positive_check", "negative_probe"):
            matrix = self.matrix()
            row = matrix["rows"][0]
            row["evidence"] = row[field]["evidence"]
            row[field]["evidence"] = []
            self.assertFalse(gate.validate_matrix(matrix, self.root)["passed"])

    def test_shared_host_gate_and_human_signoff_cannot_be_unit_evidence(self):
        matrix = self.matrix()
        p4 = next(r for r in matrix["rows"] if r["id"] == "P4")
        p4["terminals"][gate.HOSTS[0]]["evidence"][-1]["provenance"]["kind"] = "unit"
        self.assertFalse(gate.validate_matrix(matrix, self.root)["passed"])
        p4["terminals"][gate.HOSTS[0]] = {"status": "shared", "gate": "P4"}
        self.assertFalse(gate.validate_matrix(matrix, self.root)["schema_valid"])

    def test_negative_or_pending_visual_signoff_does_not_pass(self):
        for decision in (
            False,
            "pending",
            {"type": "visual_signoff", "approved": False},
        ):
            matrix = self.matrix()
            row = next(r for r in matrix["rows"] if r["id"] == "P4")
            ref = self.ref(decision, "P4", "human_signoff", gate.HOSTS[0], "Ryan")
            ref["provenance"]["covers"] = list(gate.REQUIRED_IDS)
            row["terminals"][gate.HOSTS[0]]["evidence"][-1] = ref
            self.assertFalse(gate.validate_matrix(matrix, self.root)["passed"])

    def test_partial_shared_host_scope_does_not_close_other_rows(self):
        matrix = self.matrix()
        row = next(r for r in matrix["rows"] if r["id"] == "P4")
        row["terminals"][gate.HOSTS[0]]["evidence"][0]["provenance"]["covers"] = [
            "P4",
            "B01",
        ]
        result = gate.validate_matrix(matrix, self.root)
        self.assertFalse(result["passed"])
        self.assertTrue(
            any(
                "B02" in error and "does not cover" in error
                for error in result["errors"]
            )
        )

    def test_source_hash_drift_and_missing_coverage_fail(self):
        matrix = self.matrix()
        matrix["rows"][0]["source_paths"].append("src/unpinned.py")
        with self.assertRaisesRegex(gate.Invalid, "source_paths"):
            gate.validate_matrix(matrix, self.root)
        matrix = self.matrix()
        self.write("src/example.py", "# changed source")
        with self.assertRaisesRegex(gate.Invalid, "hash changed"):
            gate.validate_matrix(matrix, self.root)

    def test_p0_only_retained_closure_does_not_accept_performance(self):
        matrix = self.matrix()
        self.closed_p0(matrix)
        self.assertTrue(gate.validate_matrix(matrix, self.root)["passed"])
        matrix["rows"][0]["status"] = "closed_by_user"
        self.assertFalse(gate.validate_matrix(matrix, self.root)["schema_valid"])
        matrix = self.matrix()
        row = self.closed_p0(matrix)
        decision = copy.deepcopy(row["decision"]["expected"])
        decision["performance_accepted"] = True
        row["decision"] = self.ref(decision, "P0", "human_decision", observer="Ryan")
        self.assertFalse(gate.validate_matrix(matrix, self.root)["passed"])

    def test_closure_requires_exact_quote_in_pinned_original(self):
        matrix = self.matrix()
        row = self.closed_p0(matrix)
        decision = copy.deepcopy(row["decision"]["expected"])
        decision["quote"] = "Not in the original"
        row["decision"] = self.ref(decision, "P0", "human_decision", observer="Ryan")
        self.assertFalse(gate.validate_matrix(matrix, self.root)["passed"])

    def test_path_escape_wrong_cover_and_stale_revision_rejected(self):
        for field, value in (
            ("path", "../outside.json"),
            ("cover", "B02"),
            ("revision", "d" * 40),
        ):
            matrix = self.matrix()
            ref = matrix["rows"][0]["positive_check"]["evidence"][0]
            if field == "path":
                ref["path"] = value
            elif field == "cover":
                ref["provenance"]["covers"] = [value]
            else:
                ref["provenance"]["product_revision"] = value
            self.assertFalse(gate.validate_matrix(matrix, self.root)["schema_valid"])

    def test_json_rejects_duplicate_keys_nonfinite_and_overflow(self):
        for payload in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'):
            with self.subTest(payload=payload), self.assertRaises(gate.Invalid):
                gate.read_json(self.write("invalid.json", payload))

    def test_rfc6901_and_false_zero_empty_assertions_remain_distinct(self):
        obj = {"a/b": {"~": [False, 0, []]}}
        self.assertIs(gate.pointer(obj, "/a~1b/~0/0"), False)
        for index, expected in enumerate((False, 0, [])):
            ref = self.ref(obj, "B01", pointer=f"/a~1b/~0/{index}")
            self.assertTrue(
                gate.typed_equal(
                    gate.Evidence(self.root, PRODUCT).assertion(ref, "B01"), expected
                )
            )
        self.assertFalse(gate.typed_equal(False, 0))
        self.assertFalse(gate.typed_equal(1, 1.0))
        with self.assertRaises(gate.Invalid):
            gate.pointer(obj, "/a~2b")

    def test_lifecycle_three_retained_runs_cover_faults_without_live_execution(self):
        self.assertTrue(
            gate.validate_lifecycle(self.lifecycle(), self.root, gate.HOSTS[0], 3)[
                "passed"
            ]
        )

    def test_lifecycle_missing_fault_repeat_or_recovery_observation_fails(self):
        for mutation in ("fault", "repeat", "recovery", "identity"):
            document = self.lifecycle()
            if mutation == "fault":
                document["runs"][0]["fault"] = document["runs"][1]["fault"]
            elif mutation == "repeat":
                document["runs"].pop()
            elif mutation == "recovery":
                document["runs"][0]["checks"]["no_automatic_resend"] = False
            else:
                document["runs"][0]["id"] = document["runs"][1]["id"]
            with self.subTest(mutation=mutation), self.assertRaises(gate.Invalid):
                gate.validate_lifecycle(document, self.root, gate.HOSTS[0], 3)

    def test_synthetic_performance_has_explicit_approved_limits(self):
        b, r, t, path = self.perf_bundle()
        result = gate.validate_performance(b, r, t, self.root, path)
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["comparisons"]), 24)

    def test_artificial_numeric_render_delay_trips_latency_gate(self):
        b, r, t, path = self.perf_bundle()
        run = next(
            run
            for run in r["runs"]
            if run["phase"] == "steady" and run["cache"] == "cold"
        )
        run["metrics"]["input_render_ms"].update(value=900, end_ns=1900000000)
        self.rebind_run(run)
        result = gate.validate_performance(b, r, t, self.root, path)
        self.assertFalse(result["passed"])
        failures = [row for row in result["comparisons"] if not row["passed"]]
        self.assertEqual([row["metric"] for row in failures], ["input_render_ms"])

    def test_performance_missing_host_samples_cache_or_render_boundary_fails(self):
        for mutation in (
            "host",
            "samples",
            "cache",
            "send_only",
            "metric",
            "resource",
            "context",
        ):
            b, r, t, path = self.perf_bundle()
            if mutation == "host":
                r["runs"] = [
                    run for run in r["runs"] if run["terminal"] == gate.HOSTS[0]
                ]
            elif mutation == "samples":
                r["runs"].pop(1)
            else:
                run = r["runs"][0]
                if mutation == "cache":
                    run["cache_basis"] = "first_run_assumed_cold"
                elif mutation == "send_only":
                    run["metrics"]["first_editable_ms"]["method"] = "command_sent"
                elif mutation == "metric":
                    run["metrics"].clear()
                elif mutation == "resource":
                    run["context"]["resource_scope"] = "rust_only"
                else:
                    run["context"]["hardware"] = "different"
                self.rebind_run(run)
            with self.subTest(mutation=mutation), self.assertRaises(gate.Invalid):
                gate.validate_performance(b, r, t, self.root, path)

    def test_duplicate_performance_ids_and_reused_evidence_do_not_count(self):
        b, r, t, path = self.perf_bundle()
        r["runs"][1]["id"] = r["runs"][0]["id"]
        with self.assertRaises(gate.Invalid):
            gate.validate_performance(b, r, t, self.root, path)
        b, r, t, path = self.perf_bundle()
        r["runs"][1]["evidence"] = copy.deepcopy(r["runs"][0]["evidence"])
        with self.assertRaises(gate.Invalid):
            gate.validate_performance(b, r, t, self.root, path)

    def test_missing_numeric_approval_and_changed_limits_fail(self):
        for mutation in ("absent", "unapproved", "limit", "baseline"):
            b, r, t, path = self.perf_bundle()
            if mutation == "absent":
                t.pop("approval")
            elif mutation == "limit":
                t["limits"][0]["max_ratio"] = 2
            else:
                approval = copy.deepcopy(t["approval"]["expected"])
                approval[
                    "approved" if mutation == "unapproved" else "baseline_sha256"
                ] = False if mutation == "unapproved" else "0" * 64
                t["approval"] = self.ref(
                    approval, "performance", "human_decision", observer="Ryan"
                )
            with self.subTest(mutation=mutation), self.assertRaises(gate.Invalid):
                gate.validate_performance(b, r, t, self.root, path)

    def test_zero_baseline_ratio_and_nonfinite_metric_fail(self):
        b, r, t, path = self.perf_bundle()
        for run in b["runs"]:
            if run["phase"] == "steady":
                run["metrics"]["cpu_idle_percent"]["value"] = 0
                self.rebind_run(run)
        self.document("baseline.json", b)
        t["baseline"] = self.pin(path)
        approval = copy.deepcopy(t["approval"]["expected"])
        approval["baseline_sha256"] = gate.sha(path)
        t["approval"] = self.ref(
            approval, "performance", "human_decision", observer="Ryan"
        )
        with self.assertRaisesRegex(gate.Invalid, "zero baseline"):
            gate.validate_performance(b, r, t, self.root, path)
        with self.assertRaises(gate.Invalid):
            gate.number(float("inf"), "metric")

    def test_actual_delay_record_absent_or_nonfailing_is_rejected(self):
        for mutation in (
            "missing",
            "not_failed",
            "wrong_limits",
            "not_render",
            "same_source",
        ):
            b, r, t, path = self.perf_bundle()
            if mutation == "missing":
                r.pop("render_delay_control")
            else:
                control = r["render_delay_control"]
                if mutation == "not_failed":
                    control["gate_exit_code"] = 0
                elif mutation == "wrong_limits":
                    control["limits_sha256"] = "0" * 64
                elif mutation == "not_render":
                    control["observation"]["method"] = "command_sent"
                else:
                    control["injection"]["after"] = control["injection"]["before"]
                control["evidence"] = self.ref(
                    {
                        k: copy.deepcopy(v)
                        for k, v in control.items()
                        if k != "evidence"
                    },
                    "performance",
                    "performance_measurement",
                    control["terminal"],
                )
            with self.subTest(mutation=mutation), self.assertRaises(gate.Invalid):
                gate.validate_performance(b, r, t, self.root, path)

    def test_explicit_pending_performance_artifact_reports_no_acceptance(self):
        document = {
            "schema_version": 1,
            "kind": "performance_samples",
            "product_revision": PRODUCT,
            "status": "pending",
            "pending_reason": "Actual host cache samples unavailable",
            "runs": [],
        }
        path = self.document("pending.json", document)
        code, result = self.cli(
            [
                "--scenario",
                "performance",
                "--baseline",
                str(path),
                "--results",
                str(path),
                "--thresholds",
                str(path),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("Actual host cache", result["pending"])

    def test_missing_cli_results_and_thresholds_fail_closed(self):
        for args in (
            ["--scenario", "performance"],
            ["--scenario", "lifecycle", "--terminal", "suite"],
            ["--matrix", str(self.root / "absent.json"), "--require-complete"],
        ):
            code, result = self.cli(args)
            self.assertEqual(code, 2)
            self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
