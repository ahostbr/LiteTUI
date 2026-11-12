"""Read-only verifier for retained Rust frontend acceptance evidence (schema 1).

This command never launches a frontend, contacts a provider, or opens a device.
Run from the worktree with --evidence-root . (the default). Evidence paths are
relative to that root and must resolve inside it. Every JSON assertion is typed,
hash-bound, and accompanied by explicit provenance. This verifies consistency
of retained records; it cannot independently authenticate the human who made a
record or establish that an operator's description of an observation is true.

Functional mode replaces the historical seed path with the current ledger:
  python e2e/rust_frontend_acceptance.py --matrix \
    docs/Plans/rust-tui-port/acceptance-matrix.json --require-complete
The original 528-row parity.json remains a historical discovery seed. Required
IDs here are independently fixed at B01..B32 and P0..P5. Without
--require-complete a structurally valid pending matrix can be reported with
exit 0; its JSON still says passed=false. Invalid evidence/schema always fails.

Matrix: {schema_version:1,kind:"rust_frontend_acceptance",product_revision,
ledger:{path,sha256},source_hashes:{repo_relative_path:sha256},rows:[...]}.
All row.source_paths must be covered and their current bytes rehashed. Rows retain the 15 sub-terminal P0 fields.
positive_check and negative_probe are {description,status,evidence:[REF]}.
Other metadata may be nonempty strings/lists/objects; source_paths is a list of
paths and source_revision equals product_revision. Extra row.evidence does not
make either check pass. terminals has litesuite_embedded and windows_terminal;
each is {status,evidence:[REF]} or {status:"shared",gate:"P4"}. P4 itself must
have direct host_observation and Ryan human_signoff records for both hosts.
A signoff asserts {type:"visual_signoff",observer:"Ryan",approved:true,terminal,
product_revision,scope,covered_ids:[...]}. Shared rows must appear in the P4
host observation covers and that signoff's covered_ids; a Settings-only record
cannot close unrelated shared coverage.
A REF is {path,sha256,pointer,expected,provenance:{kind,product_revision,covers,
description,host?,observer?}}. pointer is RFC6901. expected is required and may
be false, 0 or []: equality never coerces JSON types. covers includes the row ID
(or "performance"/"lifecycle" in those modes). Provenance does not substitute
for a check's explicit passed status and matching underlying observation.

Only P0 may use status=closed_by_user and decision=REF (human_decision/Ryan).
Its asserted decision object must contain type=phase_closure, phase=P0,
observer=Ryan, performance_accepted=false, scope, quote, source:{path,sha256}.
The quote must occur verbatim in the hash-verified original document. This is
not a waiver mechanism for other rows or a numerical/visual approval.

Retained lifecycle mode preserves the planned command and adds --results:
  python e2e/rust_frontend_acceptance.py --scenario lifecycle \
    --terminal windows_terminal --repeats 3 --results lifecycle.json
Results: {schema_version:1,kind:"lifecycle_results",product_revision,runs:[...]}
Each run has id,terminal,fault,evidence (a lifecycle REF),checks. At least three
distinct runs per selected host collectively cover kill_runtime_during_stream,
stderr_flood and close_stdin; this does not multiply three into nine runs.
The asserted evidence object must exactly bind id/terminal/fault/checks. All
LIFECYCLE_CHECKS below must be true. They include the actual fault observation,
owned-process identity, disconnection/input/no-replay, explicit recovery,
restoration and retirement. Missing observations fail, rather than launching
an unrequested live test.

Performance mode:
  python e2e/rust_frontend_acceptance.py --scenario performance \
    --baseline python-baseline.json --thresholds thresholds.json \
    --results rust-results.json
Each sample document: {schema_version:1,kind:"performance_samples",frontend:
"textual"|"rust",product_revision,completed_at:<aware ISO8601>,runs:[...]}
Run: {id,terminal,cache:"cold"|"warm",cache_basis:"observed",cache_procedure,
phase:"launch"|"steady",context,metrics,evidence:REF}. Its REF is a
performance_measurement assertion of the exact run excluding evidence itself.
Context requires hardware,fixture_sha256,fixture_messages=10000,
provider_boundary,terminal,background_load,workload,resource_scope=
"frontend_and_owned_python",shared_model_services. The terminal object records
host,version,columns,rows,font,dpi. Contexts must match within each host/cache
class and between frontends. Cache classification is observed, never inferred
from sample order. Five distinct launches and three distinct steady windows
per frontend/host TOTAL are required; both cold/warm strata must contain both
phases. One evidence pointer cannot be counted as multiple samples.
Launch metrics: first_editable_ms. Steady metrics: input_render_ms,
scroll_10000_ms,cpu_idle_percent,cpu_stream_percent,rss_peak_bytes. Each metric
is {value, ...}; timed metrics also have method=host_frame|decoded_host_cells,
start_ns,end_ns, and the value must equal elapsed milliseconds. Decoded cells
are not pixel timing. Shared model services are accounted separately.
Thresholds: {schema_version:1,kind:"performance_thresholds",baseline:{path,
sha256},limits:[{terminal,cache,metric,statistic:"median"|"max",max_value?,
max_ratio?}],approval:REF}. All host/cache/metric combinations are mandatory;
no numeric defaults exist. Approval is a human_decision/Ryan assertion of
{type:"performance_threshold_approval",observer:"Ryan",approved:true,
baseline_sha256,limits_sha256,approved_at}. limits_sha256 hashes canonical JSON
(sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False). Approval
must follow baseline completed_at and bind these exact limits. Results must
also contain one render_delay_control: {type:"render_delay_control",terminal,
cache,metric:"input_render_ms",injection_target:"frontend_render",injection:
{before:{path,sha256},after:{path,sha256},added_delay_ms,description},
baseline_sha256,limits_sha256,observation:<timed metric record>,gate_exit_code:1,
evidence:REF}. The assertion binds that whole control excluding evidence;
the pinned before/after injection files differ and measured latency must breach
the exact approved stratum. This is one control, not another host/run campaign.
Zero baseline ratios fail closed. A synthetic delayed numeric sample in unit tests proves
checker sensitivity only; it is not an actual frontend delay-injection run.

Exit codes: 0 accepted (or explicit incomplete matrix report), 1 incomplete or
failed acceptance, 2 invalid/missing input. JSON stdout is the only output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_IDS = tuple(f"B{i:02}" for i in range(1, 33)) + tuple(
    f"P{i}" for i in range(6)
)
HOSTS = ("litesuite_embedded", "windows_terminal")
STATUSES = {"passed", "pending", "blocked", "failed", "deferred", "skipped"}
KINDS = {
    "unit",
    "controlled_process",
    "package",
    "provider",
    "host_observation",
    "human_signoff",
    "human_decision",
    "performance_measurement",
    "lifecycle",
}
ROW_FIELDS = (
    "id",
    "source_revision",
    "source_paths",
    "entry_action",
    "preconditions",
    "request",
    "response",
    "events",
    "state_owner",
    "rust_surface",
    "positive_check",
    "negative_probe",
    "terminals",
    "status",
    "evidence",
)
METRICS = (
    "first_editable_ms",
    "input_render_ms",
    "scroll_10000_ms",
    "cpu_idle_percent",
    "cpu_stream_percent",
    "rss_peak_bytes",
)
FAULTS = {"kill_runtime_during_stream", "stderr_flood", "close_stdin"}
LIFECYCLE_CHECKS = (
    "real_executable",
    "owned_process_identity_verified",
    "fault_observed",
    "error_visible",
    "recoverable_input_retained",
    "no_automatic_resend",
    "raw_mode_restored",
    "alternate_screen_restored",
    "cursor_restored",
    "input_modes_restored",
    "no_surviving_owned_children",
    "shared_services_untouched",
    "explicit_relaunch",
    "persisted_state_visible",
)
MAX_JSON_BYTES = 64 * 1024 * 1024


class Invalid(ValueError):
    """Input cannot support an acceptance decision."""


class Pending(Invalid):
    """Explicitly retained incomplete acceptance record."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def populated(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return False


def typed_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            typed_equal(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            typed_equal(a, b) for a, b in zip(left, right)
        )
    return bool(left == right)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized(path: Path, *, strict: bool = True) -> Path:
    value = str(path.resolve(strict=strict))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    require(path.is_file(), f"Missing JSON evidence: {path}")
    require(
        path.stat().st_size <= MAX_JSON_BYTES,
        f"JSON evidence exceeds bounded size: {path}",
    )

    def nonfinite(value: str) -> None:
        raise Invalid(f"Nonfinite JSON number: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        require(math.isfinite(parsed), "Nonfinite JSON number")
        return parsed

    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=no_duplicate_keys,
        parse_constant=nonfinite,
        parse_float=finite_float,
    )


def pointer(document: Any, value: str) -> Any:
    require(
        isinstance(value, str) and (not value or value.startswith("/")),
        "Invalid JSON pointer",
    )
    if not value:
        return document
    for encoded in value[1:].split("/"):
        require(re.search(r"~(?![01])", encoded) is None, "Invalid JSON pointer escape")
        part = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(document, dict):
            require(part in document, f"Missing JSON pointer component: {part}")
            document = document[part]
        elif isinstance(document, list):
            require(
                re.fullmatch(r"0|[1-9][0-9]*", part) is not None,
                "Invalid JSON array index",
            )
            index = int(part)
            require(index < len(document), "JSON array index outside evidence")
            document = document[index]
        else:
            raise Invalid("JSON pointer traverses a scalar")
    return document


def revision(value: Any) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None,
        "product_revision must be the full lowercase product commit",
    )
    return value


def aware_time(value: Any) -> datetime:
    require(isinstance(value, str), "Timestamp must be an ISO8601 string")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "Timestamp must include a timezone")
    return parsed


class Evidence:
    def __init__(self, root: Path, product: str):
        self.root = normalized(root)
        self.product = revision(product)

    def file(self, ref: Any) -> Path:
        require(isinstance(ref, dict), "Evidence reference must be an object")
        name = ref.get("path")
        require(
            isinstance(name, str)
            and bool(name)
            and not Path(name).is_absolute()
            and not re.match(r"^[A-Za-z]:", name)
            and ".." not in name.replace("\\", "/").split("/"),
            "Evidence path must be relative and contained",
        )
        lexical = self.root / name
        current = self.root
        for part in lexical.relative_to(self.root).parts:
            current = current / part
            metadata = current.lstat()
            require(
                not current.is_symlink()
                and not getattr(metadata, "st_file_attributes", 0) & 0x400,
                "Evidence/source path contains a symlink or reparse point",
            )
        path = normalized(lexical)
        require(
            path.is_relative_to(self.root) and path.is_file(),
            "Evidence escapes root or is not a file",
        )
        digest = ref.get("sha256")
        require(
            isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            "Evidence needs a SHA256",
        )
        require(sha(path) == digest, f"Evidence hash changed: {name}")
        return path

    def assertion(
        self,
        ref: Any,
        cover: str,
        *,
        kinds: set[str] | None = None,
        host: str | None = None,
        observer: str | None = None,
    ) -> Any:
        path = self.file(ref)
        require(
            "pointer" in ref and "expected" in ref,
            "Evidence needs pointer and typed expected value",
        )
        provenance = ref.get("provenance")
        require(isinstance(provenance, dict), "Evidence needs explicit provenance")
        require(
            provenance.get("kind") in (kinds or KINDS), "Wrong evidence provenance kind"
        )
        require(
            provenance.get("product_revision") == self.product,
            "Evidence product revision differs",
        )
        covers = provenance.get("covers")
        require(
            isinstance(covers, list)
            and cover in covers
            and all(isinstance(c, str) for c in covers),
            f"Evidence does not cover {cover}",
        )
        require(
            populated(provenance.get("description")),
            "Evidence scope description missing",
        )
        if host is not None:
            require(provenance.get("host") == host, "Evidence host differs")
        if observer is not None:
            require(
                provenance.get("observer") == observer, "Named human record missing"
            )
        actual = pointer(read_json(path), ref["pointer"])
        require(
            typed_equal(actual, ref["expected"]),
            f"Evidence assertion differs: {ref['path']}#{ref['pointer']}",
        )
        return actual


def header(document: Any, kind: str) -> str:
    require(isinstance(document, dict), "Input must be an object")
    require(
        type(document.get("schema_version")) is int
        and document["schema_version"] == SCHEMA_VERSION,
        "Unknown acceptance schema version",
    )
    require(document.get("kind") == kind, f"Expected {kind}")
    product = revision(document.get("product_revision"))
    if document.get("status") in ("pending", "blocked", "deferred", "failed"):
        raise Pending(
            f"{kind}: {document['status']}: {document.get('pending_reason', 'acceptance evidence incomplete')}"
        )
    return product


def refs(value: Any) -> list[dict[str, Any]]:
    require(isinstance(value, list), "Evidence must be a list")
    return value


def validate_matrix(document: Any, root: Path) -> dict[str, Any]:
    product = header(document, "rust_frontend_acceptance")
    evidence = Evidence(root, product)
    evidence.file(document.get("ledger"))
    rows = document.get("rows")
    require(
        isinstance(rows, list) and all(isinstance(r, dict) for r in rows),
        "Matrix rows missing",
    )
    ids = [r.get("id") for r in rows]
    require(all(isinstance(i, str) for i in ids), "Row ID missing")
    require(len(ids) == len(set(ids)), "Duplicate required row")
    require(
        set(ids) == set(REQUIRED_IDS),
        f"Required row set differs; missing={sorted(set(REQUIRED_IDS) - set(ids))}; unknown={sorted(set(ids) - set(REQUIRED_IDS))}",
    )
    source_hashes = document.get("source_hashes")
    require(
        isinstance(source_hashes, dict) and bool(source_hashes),
        "Current source hashes missing",
    )
    for name, digest in source_hashes.items():
        evidence.file({"path": name, "sha256": digest})
    for row in rows:
        paths = row.get("source_paths")
        require(
            isinstance(paths, list)
            and all(isinstance(p, str) and p in source_hashes for p in paths),
            f"{row['id']}: source_paths missing from current source hashes",
        )
    by_id = {row["id"]: row for row in rows}
    pending: list[str] = []
    invalid: list[str] = []
    for row in rows:
        name = row["id"]
        try:
            require(
                all(field in row for field in ROW_FIELDS),
                f"{name}: missing required row fields",
            )
            require(
                row["source_revision"] == product, f"{name}: source revision differs"
            )
            status = row["status"]
            if status == "closed_by_user":
                require(name == "P0", "Only P0 permits retained user phase closure")
                decision = evidence.assertion(
                    row.get("decision"), name, kinds={"human_decision"}, observer="Ryan"
                )
                require(
                    isinstance(decision, dict)
                    and decision.get("type") == "phase_closure"
                    and decision.get("phase") == "P0"
                    and decision.get("observer") == "Ryan"
                    and decision.get("performance_accepted") is False,
                    "P0 decision is not a scoped phase closure",
                )
                require(
                    populated(decision.get("scope"))
                    and populated(decision.get("quote")),
                    "P0 closure scope/quote missing",
                )
                source = evidence.file(decision.get("source"))
                require(
                    decision["quote"] in source.read_text(encoding="utf-8-sig"),
                    "P0 closure quote absent from pinned source",
                )
                continue
            require(status in STATUSES, f"{name}: unknown status")
            if status != "passed":
                pending.append(f"{name}: {status}")
            else:
                for field in ROW_FIELDS[1:10]:
                    require(populated(row[field]), f"{name}: {field} is unknown")
                require(
                    isinstance(row["source_paths"], list)
                    and all(isinstance(p, str) and p for p in row["source_paths"]),
                    f"{name}: source_paths must name source files",
                )
            for extra in refs(row["evidence"]):
                evidence.assertion(extra, name)
            for field in ("positive_check", "negative_probe"):
                check = row[field]
                if check is None and status != "passed":
                    continue
                require(
                    isinstance(check, dict) and check.get("status") in STATUSES,
                    f"{name}: invalid {field}",
                )
                check_refs = refs(check.get("evidence"))
                for ref in check_refs:
                    evidence.assertion(ref, name)
                if check["status"] == "passed":
                    require(
                        populated(check.get("description")) and bool(check_refs),
                        f"{name}: {field} lacks observed evidence",
                    )
                elif status == "passed":
                    pending.append(f"{name}: {field} {check['status']}")
            terminals = row["terminals"]
            require(
                isinstance(terminals, dict) and set(terminals) == set(HOSTS),
                f"{name}: both terminal dispositions required",
            )
            for host, record in terminals.items():
                require(isinstance(record, dict), f"{name}: invalid host record")
                if record.get("status") == "shared":
                    require(
                        name != "P4" and record.get("gate") == "P4",
                        "Shared host coverage must refer directly to P4",
                    )
                    shared = by_id["P4"].get("terminals", {}).get(host, {})
                    if shared.get("status") != "passed":
                        pending.append(f"{name}: shared P4 {host} pending")
                    else:
                        observed = False
                        signed = False
                        for ref in refs(shared.get("evidence")):
                            provenance = ref.get("provenance", {})
                            if name not in provenance.get("covers", []):
                                continue
                            if provenance.get("kind") == "host_observation":
                                evidence.assertion(
                                    ref, name, kinds={"host_observation"}, host=host
                                )
                                observed = True
                            if provenance.get("kind") == "human_signoff":
                                decision = evidence.assertion(
                                    ref,
                                    name,
                                    kinds={"human_signoff"},
                                    host=host,
                                    observer="Ryan",
                                )
                                signed = isinstance(
                                    decision, dict
                                ) and name in decision.get("covered_ids", [])
                        require(
                            observed and signed,
                            f"{name}: shared P4 host observation/signoff does not cover this row",
                        )
                    continue
                require(
                    record.get("status") in STATUSES, f"{name}: invalid host status"
                )
                host_refs = refs(record.get("evidence"))
                for ref in host_refs:
                    evidence.assertion(
                        ref,
                        name,
                        kinds={"host_observation", "human_signoff"},
                        host=host,
                    )
                if record["status"] != "passed":
                    pending.append(f"{name}: {host} {record['status']}")
                    continue
                require(
                    any(
                        r["provenance"]["kind"] == "host_observation" for r in host_refs
                    ),
                    f"{name}: no actual host observation",
                )
                if name == "P4":
                    human = [
                        r
                        for r in host_refs
                        if r["provenance"]["kind"] == "human_signoff"
                    ]
                    require(bool(human), f"P4: {host} lacks Ryan visual signoff")
                    for ref in human:
                        decision = evidence.assertion(
                            ref,
                            name,
                            kinds={"human_signoff"},
                            host=host,
                            observer="Ryan",
                        )
                        require(
                            isinstance(decision, dict)
                            and decision.get("type") == "visual_signoff"
                            and decision.get("observer") == "Ryan"
                            and decision.get("approved") is True
                            and decision.get("terminal") == host
                            and decision.get("product_revision") == product
                            and populated(decision.get("scope"))
                            and isinstance(decision.get("covered_ids"), list)
                            and name in decision["covered_ids"],
                            "P4 requires an affirmative scoped visual decision",
                        )
        except (Invalid, OSError, ValueError, TypeError, KeyError) as error:
            invalid.append(f"{name}: {error}")
    return {
        "mode": "matrix",
        "passed": not pending and not invalid,
        "schema_valid": not invalid,
        "required_rows": len(REQUIRED_IDS),
        "pending": pending,
        "errors": invalid,
        "limitation": "Checks retained-record consistency; does not independently authenticate observers.",
    }


def validate_lifecycle(
    document: Any, root: Path, host: str, repeats: int
) -> dict[str, Any]:
    evidence = Evidence(root, header(document, "lifecycle_results"))
    require(
        host in HOSTS and type(repeats) is int and repeats >= 3,
        "Lifecycle requires a named host and at least three repeats",
    )
    runs = document.get("runs")
    require(isinstance(runs, list) and bool(runs), "Lifecycle results missing")
    ids: set[str] = set()
    identities: set[tuple[str, str]] = set()
    selected: list[dict[str, Any]] = []
    for run in runs:
        require(
            isinstance(run, dict)
            and populated(run.get("id"))
            and run.get("terminal") in HOSTS,
            "Invalid lifecycle run",
        )
        require(run["id"] not in ids, "Duplicate lifecycle run ID")
        ids.add(run["id"])
        if run["terminal"] != host:
            continue
        require(run.get("fault") in FAULTS, "Unknown lifecycle fault")
        checks = run.get("checks")
        require(
            isinstance(checks, dict)
            and all(checks.get(k) is True for k in LIFECYCLE_CHECKS),
            "Required lifecycle observation missing or failed",
        )
        ref = run.get("evidence")
        actual = evidence.assertion(ref, "lifecycle", kinds={"lifecycle"}, host=host)
        require(
            typed_equal(
                actual, {key: run[key] for key in ("id", "terminal", "fault", "checks")}
            ),
            "Lifecycle evidence does not bind this run",
        )
        identity = (str(evidence.file(ref)), ref["pointer"])
        require(identity not in identities, "Reused lifecycle observation")
        identities.add(identity)
        selected.append(run)
    require(
        len(selected) >= repeats and {r["fault"] for r in selected} == FAULTS,
        "Lifecycle repeats or required fault coverage missing",
    )
    return {
        "mode": "lifecycle",
        "passed": True,
        "terminal": host,
        "runs": len(selected),
        "faults": sorted(FAULTS),
        "execution": "retained evidence only",
    }


def number(value: Any, label: str, *, positive: bool = False) -> float:
    require(
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0),
        f"{label} must be a finite {'positive' if positive else 'nonnegative'} number",
    )
    return float(value)


def performance_samples(
    document: Any, root: Path, frontend: str
) -> tuple[dict[Any, Any], dict[Any, Any]]:
    evidence = Evidence(root, header(document, "performance_samples"))
    require(document.get("frontend") == frontend, f"Expected {frontend} samples")
    aware_time(document.get("completed_at"))
    runs = document.get("runs")
    require(isinstance(runs, list) and bool(runs), "Performance results missing")
    ids: set[str] = set()
    identities: set[tuple[str, str]] = set()
    contexts: dict[Any, Any] = {}
    values: dict[Any, Any] = {}
    counts: dict[Any, int] = {}
    for run in runs:
        require(
            isinstance(run, dict)
            and isinstance(run.get("id"), str)
            and bool(run["id"]),
            "Performance run ID missing",
        )
        require(run["id"] not in ids, "Duplicate performance run ID")
        ids.add(run["id"])
        host, cache, phase = run.get("terminal"), run.get("cache"), run.get("phase")
        require(
            host in HOSTS
            and cache in ("cold", "warm")
            and phase in ("launch", "steady"),
            "Unknown host/cache/phase",
        )
        require(
            run.get("cache_basis") == "observed"
            and populated(run.get("cache_procedure")),
            "Cache state is not an observed classification",
        )
        context = run.get("context")
        fields = (
            "hardware",
            "fixture_sha256",
            "provider_boundary",
            "terminal",
            "background_load",
            "workload",
            "shared_model_services",
        )
        require(
            isinstance(context, dict)
            and all(populated(context.get(k)) for k in fields),
            "Measurement context incomplete",
        )
        require(
            context.get("fixture_messages") == 10000
            and type(context.get("fixture_messages")) is int,
            "Performance requires the 10,000-message fixture",
        )
        require(
            isinstance(context["fixture_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", context["fixture_sha256"]) is not None,
            "Fixture hash missing",
        )
        require(
            context.get("resource_scope") == "frontend_and_owned_python",
            "Combined frontend/runtime resource scope required",
        )
        terminal = context["terminal"]
        require(
            isinstance(terminal, dict)
            and terminal.get("host") == host
            and all(populated(terminal.get(k)) for k in ("version", "font")),
            "Terminal identity incomplete",
        )
        for key in ("columns", "rows", "dpi"):
            number(terminal.get(key), f"terminal {key}", positive=True)
        group = host, cache
        if group in contexts:
            require(
                typed_equal(contexts[group], context),
                "Context changes within a performance stratum",
            )
        contexts[group] = context
        ref = run.get("evidence")
        actual = evidence.assertion(
            ref, "performance", kinds={"performance_measurement"}, host=host
        )
        require(
            typed_equal(actual, {k: v for k, v in run.items() if k != "evidence"}),
            "Performance evidence does not bind this sample",
        )
        identity = (str(evidence.file(ref)), ref["pointer"])
        require(identity not in identities, "Reused performance sample evidence")
        identities.add(identity)
        metrics = run.get("metrics")
        required = METRICS[:1] if phase == "launch" else METRICS[1:]
        require(
            isinstance(metrics, dict) and set(metrics) == set(required),
            "Required performance metric missing or unknown",
        )
        for metric in required:
            observation = metrics[metric]
            require(isinstance(observation, dict), "Metric needs an observation record")
            value = number(
                observation.get("value"), metric, positive=metric == "rss_peak_bytes"
            )
            if metric.endswith("_ms"):
                require(
                    observation.get("method") in ("host_frame", "decoded_host_cells"),
                    "Timing must observe rendered output, not command-send completion",
                )
                start, end = observation.get("start_ns"), observation.get("end_ns")
                require(
                    type(start) is int and type(end) is int and 0 <= start < end,
                    "Timing boundaries missing",
                )
                require(
                    math.isclose(
                        value, (end - start) / 1_000_000, rel_tol=1e-9, abs_tol=1e-9
                    ),
                    "Latency differs from observation interval",
                )
            values.setdefault((host, cache, metric), []).append(value)
        counts[(host, phase)] = counts.get((host, phase), 0) + 1
    for host in HOSTS:
        require(
            counts.get((host, "launch"), 0) >= 5
            and counts.get((host, "steady"), 0) >= 3,
            f"{frontend}/{host}: need five launches and three steady windows total",
        )
        for cache in ("cold", "warm"):
            require(
                all((host, cache, metric) in values for metric in METRICS),
                f"{frontend}/{host}/{cache}: separate complete cache stratum missing",
            )
    return contexts, values


def validate_performance(
    baseline: Any, results: Any, thresholds: Any, root: Path, baseline_path: Path
) -> dict[str, Any]:
    old_context, old = performance_samples(baseline, root, "textual")
    new_context, new = performance_samples(results, root, "rust")
    require(
        typed_equal(old_context, new_context),
        "Baseline/results fixture, hardware, host, load or cache context differs",
    )
    product = header(thresholds, "performance_thresholds")
    require(
        product == results["product_revision"],
        "Threshold product differs from measured Rust product",
    )
    evidence = Evidence(root, product)
    require(
        evidence.file(thresholds.get("baseline")) == normalized(baseline_path),
        "Thresholds bind a different baseline",
    )
    limits = thresholds.get("limits")
    require(
        isinstance(limits, list) and bool(limits), "Approved numeric thresholds missing"
    )
    approval = evidence.assertion(
        thresholds.get("approval"),
        "performance",
        kinds={"human_decision"},
        observer="Ryan",
    )
    require(
        isinstance(approval, dict)
        and approval.get("type") == "performance_threshold_approval"
        and approval.get("observer") == "Ryan"
        and approval.get("approved") is True,
        "Explicit numerical approval missing",
    )
    require(
        approval.get("baseline_sha256") == sha(normalized(baseline_path))
        and approval.get("limits_sha256")
        == hashlib.sha256(canonical(limits)).hexdigest(),
        "Approval does not bind this baseline and exact numeric limits",
    )
    require(
        aware_time(approval.get("approved_at")) >= aware_time(baseline["completed_at"]),
        "Threshold approval predates baseline",
    )
    seen: set[tuple[str, str, str]] = set()
    comparisons: list[dict[str, Any]] = []
    for limit in limits:
        require(isinstance(limit, dict), "Invalid threshold record")
        key = limit.get("terminal"), limit.get("cache"), limit.get("metric")
        require(
            key in new and key not in seen, "Unknown or duplicate threshold stratum"
        )
        seen.add(key)
        statistic = limit.get("statistic")
        require(
            statistic in ("median", "max"),
            "Threshold statistic must be explicit median or max",
        )
        aggregate = statistics.median if statistic == "median" else max
        previous, current = aggregate(old[key]), aggregate(new[key])
        require(
            "max_value" in limit or "max_ratio" in limit,
            "Threshold has no approved numeric cap",
        )
        checks = []
        if "max_value" in limit:
            checks.append(current <= number(limit["max_value"], "absolute cap"))
        if "max_ratio" in limit:
            ratio = number(limit["max_ratio"], "ratio cap", positive=True)
            require(previous > 0, "Cannot evaluate ratio against zero baseline")
            checks.append(current <= previous * ratio)
        comparisons.append(
            {
                "terminal": key[0],
                "cache": key[1],
                "metric": key[2],
                "statistic": statistic,
                "baseline": previous,
                "rust": current,
                "passed": all(checks),
            }
        )
    require(seen == set(new), "Threshold coverage missing; no default limits exist")
    control = results.get("render_delay_control")
    require(
        isinstance(control, dict),
        "Actual frontend render-delay negative control missing",
    )
    host, cache = control.get("terminal"), control.get("cache")
    require(
        control.get("type") == "render_delay_control"
        and host in HOSTS
        and cache in ("cold", "warm")
        and control.get("metric") == "input_render_ms"
        and control.get("injection_target") == "frontend_render",
        "Render-delay control must name the measured frontend stratum",
    )
    actual = evidence.assertion(
        control.get("evidence"),
        "performance",
        kinds={"performance_measurement"},
        host=host,
    )
    require(
        typed_equal(actual, {k: v for k, v in control.items() if k != "evidence"}),
        "Render-delay evidence does not bind the control",
    )
    require(
        control.get("baseline_sha256") == sha(normalized(baseline_path))
        and control.get("limits_sha256")
        == hashlib.sha256(canonical(limits)).hexdigest(),
        "Render-delay control uses different baseline or thresholds",
    )
    injection = control.get("injection")
    require(
        isinstance(injection, dict) and populated(injection.get("description")),
        "Exact render-delay injection missing",
    )
    before, after = (
        evidence.file(injection.get("before")),
        evidence.file(injection.get("after")),
    )
    require(
        sha(before) != sha(after),
        "Render-delay injection did not change its pinned source",
    )
    number(injection.get("added_delay_ms"), "injected delay", positive=True)
    observation = control.get("observation")
    require(isinstance(observation, dict), "Measured render-delay observation missing")
    delayed = number(observation.get("value"), "delayed render latency", positive=True)
    start, end = observation.get("start_ns"), observation.get("end_ns")
    require(
        observation.get("method") in ("host_frame", "decoded_host_cells")
        and type(start) is int
        and type(end) is int
        and 0 <= start < end
        and math.isclose(
            delayed, (end - start) / 1_000_000, rel_tol=1e-9, abs_tol=1e-9
        ),
        "Delay control does not measure rendered output",
    )
    limit = next(
        row
        for row in limits
        if (row["terminal"], row["cache"], row["metric"])
        == (host, cache, "input_render_ms")
    )
    aggregate = statistics.median if limit["statistic"] == "median" else max
    exceeded = "max_value" in limit and delayed > limit["max_value"]
    exceeded = exceeded or (
        "max_ratio" in limit
        and delayed
        > aggregate(old[(host, cache, "input_render_ms")]) * limit["max_ratio"]
    )
    require(
        exceeded
        and type(control.get("gate_exit_code")) is int
        and control["gate_exit_code"] == 1,
        "Render-delay negative control did not prove the approved latency gate fails",
    )
    return {
        "mode": "performance",
        "passed": all(row["passed"] for row in comparisons),
        "comparisons": comparisons,
        "execution": "retained evidence only",
        "limit": "Observed host cells are not a pixel timing claim; approval identity is retained-record evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--matrix", type=Path)
    mode.add_argument("--scenario", choices=("lifecycle", "performance"))
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--evidence-root", type=Path, default=Path.cwd())
    parser.add_argument("--results", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--thresholds", type=Path)
    parser.add_argument("--terminal", choices=(*HOSTS, "suite", "windows"))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        if args.matrix is not None:
            result = validate_matrix(read_json(args.matrix), args.evidence_root)
            code = (
                2
                if not result["schema_valid"]
                else 1
                if args.require_complete and not result["passed"]
                else 0
            )
        elif args.scenario == "lifecycle":
            require(
                args.results is not None and args.terminal is not None,
                "Lifecycle needs --results and --terminal retained evidence",
            )
            host = {"suite": HOSTS[0], "windows": HOSTS[1]}.get(
                args.terminal, args.terminal
            )
            result = validate_lifecycle(
                read_json(args.results), args.evidence_root, host, args.repeats
            )
            code = 0
        else:
            require(
                args.results is not None
                and args.baseline is not None
                and args.thresholds is not None,
                "Performance needs --baseline, --thresholds and --results; nothing is skipped",
            )
            result = validate_performance(
                read_json(args.baseline),
                read_json(args.results),
                read_json(args.thresholds),
                args.evidence_root,
                args.baseline,
            )
            code = 0 if result["passed"] else 1
    except Pending as error:
        result, code = (
            {
                "passed": False,
                "pending": str(error),
                "execution": "retained evidence only",
            },
            1,
        )
    except (Invalid, OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        result, code = (
            {"passed": False, "error": str(error), "execution": "no live execution"},
            2,
        )
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
