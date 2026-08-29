from __future__ import annotations

import csv
import hashlib
import json
from http.client import IncompleteRead
from pathlib import Path

import pytest

import eval_ood_final as gate


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _lock(**overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "locked",
        "model_version": gate.MODEL_VERSION,
        "checkpoint_sha256": _digest(b"test checkpoint"),
        "ood_csv_sha256": _digest(b"test ood csv"),
        "block_threshold": 0.41,
        "review_threshold": 0.20,
        "minimum_f1": 0.90,
        "minimum_recall": 0.90,
        "expected_rows": 16,
    }
    value.update(overrides)
    return value


def _write_csv(
    path: Path,
    rows: list[dict[str, object]],
    columns: tuple[str, ...] = gate.OOD_COLUMNS,
) -> None:
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _valid_rows() -> list[dict[str, object]]:
    return [
        {
            "case_id": "case-1",
            "text": "synthetic unique positive",
            "label": "1",
            "domain": "test",
            "style": "plain",
        },
        {
            "case_id": "case-2",
            "text": "synthetic unique negative",
            "label": "0",
            "domain": "test",
            "style": "plain",
        },
    ]


def test_example_manifest_is_deliberately_invalid_and_contains_no_fake_hash() -> None:
    example_path = gate.PROJECT_ROOT / "ood_eval_manifest.example.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))

    assert example["checkpoint_sha256"] == ""
    assert example["ood_csv_sha256"] == ""
    with pytest.raises(gate.GateError):
        gate.validate_lock(example)
    assert not list(gate.PROJECT_ROOT.glob("**/ood_final*.csv"))


def test_lock_rejects_hash_or_threshold_mismatch() -> None:
    assert gate.validate_lock(_lock())["block_threshold"] == 0.41

    for invalid in (
        _lock(checkpoint_sha256=""),
        _lock(ood_csv_sha256="not-a-hash"),
        _lock(block_threshold=0.20, review_threshold=0.20),
        _lock(block_threshold=True),
        _lock(minimum_f1=0.899999),
        _lock(minimum_f1=True),
        _lock(minimum_recall=0.899999),
        _lock(minimum_recall=1.01),
        _lock(expected_rows=15),
        _lock(expected_rows=True),
        _lock(status="draft"),
    ):
        with pytest.raises(gate.GateError):
            gate.validate_lock(invalid)


def test_file_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"actual artifact")

    gate.verify_file_hash(artifact, _digest(b"actual artifact"), "artifact")
    with pytest.raises(gate.GateError, match="SHA-256"):
        gate.verify_file_hash(artifact, _digest(b"different artifact"), "artifact")


def test_ood_csv_requires_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "ood.csv"
    _write_csv(path, _valid_rows(), columns=("case_id", "text", "label", "domain"))

    with pytest.raises(gate.GateError, match="헤더"):
        gate.load_ood_rows(path)


@pytest.mark.parametrize(
    "rows",
    [
        [
            {**_valid_rows()[0], "label": "2"},
            _valid_rows()[1],
        ],
        [
            _valid_rows()[0],
            {**_valid_rows()[1], "label": "1"},
        ],
    ],
)
def test_ood_csv_rejects_invalid_labels_or_imbalance(
    tmp_path: Path,
    rows: list[dict[str, object]],
) -> None:
    path = tmp_path / "ood.csv"
    _write_csv(path, rows)

    with pytest.raises(gate.GateError):
        gate.load_ood_rows(path)


@pytest.mark.parametrize("duplicate_field", ["case_id", "text"])
def test_ood_csv_rejects_internal_duplicates(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    rows = _valid_rows()
    rows[1][duplicate_field] = rows[0][duplicate_field]
    if duplicate_field == "text":
        rows[1]["text"] = "  SYNTHETIC   UNIQUE POSITIVE  "
    path = tmp_path / "ood.csv"
    _write_csv(path, rows)

    with pytest.raises(gate.GateError, match="중복"):
        gate.load_ood_rows(path)


def test_ood_csv_rejects_tracked_train_or_hard_overlap(tmp_path: Path) -> None:
    ood_path = tmp_path / "ood.csv"
    tracked_path = tmp_path / "tracked.csv"
    _write_csv(ood_path, _valid_rows())
    _write_csv(
        tracked_path,
        [{"text": "SYNTHETIC  UNIQUE POSITIVE", "label": "1"}],
        columns=("text", "label"),
    )
    rows = gate.load_ood_rows(ood_path)

    with pytest.raises(gate.GateError, match="exact-overlap"):
        gate.ensure_no_tracked_overlap(rows, (tracked_path,))


def _design_rows(domains: int = 4, styles: int = 2) -> list[dict[str, object]]:
    return [
        {
            "case_id": f"case-{domain}-{style}-{label}",
            "domain": f"domain-{domain}",
            "style": f"style-{style}",
            "label": label,
        }
        for domain in range(domains)
        for style in range(styles)
        for label in (0, 1)
    ]


def test_ood_design_locks_rows_domains_styles_and_pair_labels() -> None:
    valid = _design_rows()
    gate.validate_ood_design(valid, 16)

    invalid_cases = [
        (valid, 17),
        (_design_rows(domains=3, styles=3), 18),
    ]
    one_style = _design_rows()
    for row in one_style:
        if row["domain"] == "domain-0":
            row["style"] = "style-0"
    invalid_cases.append((one_style, 16))
    missing_pair_label = _design_rows()
    missing_pair_label[1]["label"] = 0
    invalid_cases.append((missing_pair_label, 16))

    for rows, expected_rows in invalid_cases:
        with pytest.raises(gate.GateError):
            gate.validate_ood_design(rows, expected_rows)


def test_metrics_report_binary_confusion_precision_recall_and_f1() -> None:
    metrics = gate.compute_metrics([1, 1, 0, 0], [1, 0, 1, 0])

    assert metrics == {
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
        "confusion": {"tn": 1, "fp": 1, "fn": 1, "tp": 1},
    }


def test_complete_evaluation_passes_or_preserves_failed_quality_metrics() -> None:
    lock = gate.validate_lock(_lock())
    rows = [
        {"case_id": "positive", "label": 1},
        {"case_id": "negative", "label": 0},
    ]

    def evaluate(scores):
        remaining = iter(scores)

        def request_case(row, event_id):
            score = next(remaining)
            return {
                "event_id": event_id,
                "decision": gate.expected_decision(score, 0.41, 0.20),
                "confidence_score": score,
                "model_version": gate.MODEL_VERSION,
                "latency_ms": 1.0,
            }

        return gate.evaluate_rows(rows, lock, request_case)

    passed = evaluate([0.9, 0.1])
    assert passed["status"] == "passed"
    assert passed["quality_passed"] is True
    assert passed["metrics"]["f1"] == 1.0

    failed = evaluate([0.1, 0.1])
    assert failed["status"] == "quality_failed"
    assert failed["quality_passed"] is False
    assert failed["reason"] == "minimum_quality_not_met"
    assert failed["metrics"]["f1"] == 0.0
    assert failed["metrics"]["recall"] == 0.0


def test_live_health_must_match_locked_model_and_thresholds() -> None:
    lock = gate.validate_lock(_lock())
    valid = {
        "status": "running",
        "engine": gate.MODEL_VERSION,
        "checkpoint_sha256": lock["checkpoint_sha256"],
        "block_threshold": 0.41,
        "review_threshold": 0.20,
    }
    gate.validate_health(valid, lock)

    for invalid in (
        {**valid, "engine": "koelectra-dlp-v6"},
        {**valid, "checkpoint_sha256": _digest(b"other checkpoint")},
        {**valid, "block_threshold": 0.42},
        {**valid, "status": "model_load_failed"},
    ):
        with pytest.raises(gate.ContractError):
            gate.validate_health(invalid, lock)


def test_one_transport_error_invalidates_all_metrics() -> None:
    lock = gate.validate_lock(_lock())
    rows = [
        {"case_id": "one", "label": 1},
        {"case_id": "two", "label": 0},
    ]
    calls = 0

    def request_case(row, event_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise gate.TransportError("synthetic failure")
        return {
            "event_id": event_id,
            "decision": "block",
            "confidence_score": 0.9,
            "model_version": gate.MODEL_VERSION,
            "latency_ms": 1.0,
        }

    result = gate.evaluate_rows(rows, lock, request_case)

    assert result["status"] == "invalid"
    assert result["reason"] == "transport_error"
    assert result["transport_errors"] == 1
    assert result["evaluated_rows"] == 1
    assert result["quality_passed"] is None
    assert result["metrics"] is None


def test_incomplete_http_body_is_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incomplete_response(*args, **kwargs):
        raise IncompleteRead(b"partial", 100)

    monkeypatch.setattr(gate, "urlopen", incomplete_response)
    with pytest.raises(gate.TransportError):
        gate.request_json("https://example.test/", timeout=1)


def test_analysis_contract_error_also_invalidates_metrics() -> None:
    lock = gate.validate_lock(_lock())
    rows = [{"case_id": "one", "label": 1}]

    def request_case(row, event_id):
        return {
            "event_id": event_id,
            "decision": "allow",
            "confidence_score": 0.9,
            "model_version": gate.MODEL_VERSION,
            "latency_ms": 1.0,
        }

    result = gate.evaluate_rows(rows, lock, request_case)

    assert result["status"] == "invalid"
    assert result["reason"] == "contract_error"
    assert result["metrics"] is None


def test_result_is_reserved_then_atomically_finalized(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    gate.reserve_result(output, {"status": "invalid", "metrics": None})

    with pytest.raises(gate.GateError, match="이미"):
        gate.reserve_result(output, {"status": "quality_failed"})
    gate.finalize_result(output, {"status": "passed", "metrics": {"f1": 1.0}})
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_api_route_uses_same_locked_bytes_and_reserves_output_first() -> None:
    source = (gate.PROJECT_ROOT / "eval_ood_final.py").read_text(encoding="utf-8")
    run_source = source[source.index("def run(") :]

    assert 'f"{base_url}/api/v1/analyze"' in source
    assert "sha256_bytes(ood_bytes)" in run_source
    assert "parse_ood_rows(ood_bytes)" in run_source
    assert run_source.index("reserve_result(output_path, result)") < run_source.index(
        "request_json(f\"{base_url}/\""
    )
    assert 'return 0 if result["status"] == "passed" else 2' in run_source
