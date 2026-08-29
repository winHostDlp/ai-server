"""Run the one-shot blind OOD quality gate against the live v7 API.

The OOD CSV and the completed lock manifest must stay outside this repository.
Only aggregate metrics are written; source text and per-row predictions are not.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from model_bundle import ModelBundleError, model_bundle_sha256


PROJECT_ROOT = Path(__file__).resolve().parent
TRACKED_DATASETS = (
    PROJECT_ROOT / "data/train_dataset_v6.csv",
    PROJECT_ROOT / "data/hard_test_set_v1.csv",
)
MODEL_VERSION = "koelectra-dlp-v7"
OOD_COLUMNS = ("case_id", "text", "label", "domain", "style")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_RESPONSE_BYTES = 1024 * 1024


class GateError(RuntimeError):
    """A fail-closed local validation error."""


class TransportError(GateError):
    """The live API could not return one complete HTTP response."""


class ContractError(GateError):
    """The live API response did not match the locked contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_external_file(path: Path, description: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise GateError(f"{description} 파일을 읽을 수 없습니다.") from exc
    if not resolved.is_file():
        raise GateError(f"{description} 경로가 파일이 아닙니다.")
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return resolved
    raise GateError(f"{description} 파일은 저장소 밖에 보관해야 합니다.")


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError(f"{name}은 숫자여야 합니다.")
    number = float(value)
    if not math.isfinite(number):
        raise GateError(f"{name}은 유한한 숫자여야 합니다.")
    return number


def validate_lock(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GateError("lock manifest 최상위 값은 JSON 객체여야 합니다.")
    if value.get("status") != "locked":
        raise GateError("lock manifest가 locked 상태가 아닙니다.")
    if value.get("model_version") != MODEL_VERSION:
        raise GateError(f"model_version은 {MODEL_VERSION}이어야 합니다.")

    for name in ("checkpoint_sha256", "model_bundle_sha256", "ood_csv_sha256"):
        digest = value.get(name)
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise GateError(f"{name}은 실제 파일의 소문자 SHA-256이어야 합니다.")

    block = _finite_number(value.get("block_threshold"), "block_threshold")
    review = _finite_number(value.get("review_threshold"), "review_threshold")
    if not 0 <= review < block <= 1:
        raise GateError("threshold는 0 <= review < block <= 1이어야 합니다.")
    minimum_f1 = _finite_number(value.get("minimum_f1"), "minimum_f1")
    minimum_recall = _finite_number(value.get("minimum_recall"), "minimum_recall")
    if not 0.90 <= minimum_f1 <= 1:
        raise GateError("minimum_f1은 0.90 이상 1 이하여야 합니다.")
    if not 0.90 <= minimum_recall <= 1:
        raise GateError("minimum_recall은 0.90 이상 1 이하여야 합니다.")
    expected_rows = value.get("expected_rows")
    if isinstance(expected_rows, bool) or not isinstance(expected_rows, int):
        raise GateError("expected_rows는 정수여야 합니다.")
    if expected_rows < 16:
        raise GateError("expected_rows는 최소 16이어야 합니다.")

    return {
        "status": "locked",
        "model_version": MODEL_VERSION,
        "checkpoint_sha256": value["checkpoint_sha256"],
        "model_bundle_sha256": value["model_bundle_sha256"],
        "ood_csv_sha256": value["ood_csv_sha256"],
        "block_threshold": block,
        "review_threshold": review,
        "minimum_f1": minimum_f1,
        "minimum_recall": minimum_recall,
        "expected_rows": expected_rows,
    }


def load_lock(path: Path) -> dict[str, object]:
    manifest_path = require_external_file(path, "lock manifest")
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise GateError("lock manifest를 읽을 수 없습니다.") from exc
    return parse_lock(raw)


def parse_lock(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("lock manifest를 유효한 UTF-8 JSON으로 읽을 수 없습니다.") from exc
    return validate_lock(value)


def verify_file_hash(path: Path, expected: str, description: str) -> None:
    if sha256_file(path) != expected:
        raise GateError(f"{description} SHA-256이 lock manifest와 다릅니다.")


def verify_model_bundle_hash(path: Path, expected: str) -> str:
    try:
        actual = model_bundle_sha256(path)
    except ModelBundleError as exc:
        raise GateError(str(exc)) from exc
    if actual != expected:
        raise GateError("model bundle SHA-256이 lock manifest와 다릅니다.")
    return actual


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def load_ood_rows(path: Path) -> list[dict[str, object]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateError("OOD CSV를 읽을 수 없습니다.") from exc
    return parse_ood_rows(raw)


def parse_ood_rows(raw: bytes) -> list[dict[str, object]]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if tuple(reader.fieldnames or ()) != OOD_COLUMNS:
            raise GateError(
                "OOD CSV 헤더는 case_id,text,label,domain,style 순서여야 합니다."
            )
        raw_rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise GateError("OOD CSV를 읽을 수 없습니다.") from exc

    if not raw_rows:
        raise GateError("OOD CSV가 비어 있습니다.")

    rows: list[dict[str, object]] = []
    case_ids: set[str] = set()
    texts: set[str] = set()
    label_counts = {0: 0, 1: 0}
    for index, raw in enumerate(raw_rows, start=2):
        if None in raw:
            raise GateError(f"OOD CSV {index}행의 열 수가 header와 다릅니다.")
        values = {name: (raw.get(name) or "").strip() for name in OOD_COLUMNS}
        if any(not value for value in values.values()):
            raise GateError(f"OOD CSV {index}행에 빈 필수 값이 있습니다.")
        if len(values["text"]) > 4000:
            raise GateError(f"OOD CSV {index}행 text가 API 제한 4000자를 넘습니다.")
        if values["label"] not in {"0", "1"}:
            raise GateError(f"OOD CSV {index}행 label은 0 또는 1이어야 합니다.")
        if values["case_id"] in case_ids:
            raise GateError("OOD CSV 내부에 중복 case_id가 있습니다.")

        normalized = normalize_text(values["text"])
        if normalized in texts:
            raise GateError("OOD CSV 내부에 중복 text가 있습니다.")

        label = int(values["label"])
        case_ids.add(values["case_id"])
        texts.add(normalized)
        label_counts[label] += 1
        rows.append({**values, "label": label, "normalized_text": normalized})

    if label_counts[0] == 0 or label_counts[0] != label_counts[1]:
        raise GateError("OOD CSV는 label 0과 1을 같은 수로 포함해야 합니다.")
    return rows


def ensure_no_tracked_overlap(
    rows: list[dict[str, object]],
    tracked_paths: tuple[Path, ...] = TRACKED_DATASETS,
) -> None:
    tracked_texts: set[str] = set()
    for path in tracked_paths:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
                reader = csv.DictReader(file_obj)
                if "text" not in (reader.fieldnames or ()):
                    raise GateError(f"추적 데이터 {path.name}에 text 열이 없습니다.")
                tracked_texts.update(
                    normalize_text(row.get("text") or "")
                    for row in reader
                    if (row.get("text") or "").strip()
                )
        except (OSError, UnicodeError, csv.Error) as exc:
            raise GateError(f"추적 데이터 {path.name}을 읽을 수 없습니다.") from exc

    overlap_count = sum(
        row["normalized_text"] in tracked_texts
        for row in rows
    )
    if overlap_count:
        raise GateError(
            "OOD CSV가 train_dataset_v6 또는 hard_test_set_v1과 exact-overlap됩니다."
        )


def validate_ood_design(rows: list[dict[str, object]], expected_rows: int) -> None:
    if len(rows) != expected_rows:
        raise GateError("OOD CSV 행 수가 lock manifest의 expected_rows와 다릅니다.")

    combinations: dict[str, dict[str, set[int]]] = {}
    for row in rows:
        domain = normalize_text(str(row["domain"]))
        style = normalize_text(str(row["style"]))
        combinations.setdefault(domain, {}).setdefault(style, set()).add(
            int(row["label"])
        )
    if len(combinations) < 4:
        raise GateError("OOD CSV는 최소 4개 domain을 포함해야 합니다.")
    for styles in combinations.values():
        if len(styles) < 2:
            raise GateError("각 OOD domain은 최소 2개 style을 포함해야 합니다.")
        if any(labels != {0, 1} for labels in styles.values()):
            raise GateError("각 OOD domain×style 조합에 label 0과 1이 모두 필요합니다.")


def require_base_url(value: str | None) -> str:
    base_url = (value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise GateError("AI_BASE_URL 형식이 잘못되었습니다.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise GateError("AI_BASE_URL은 경로 없는 HTTPS 서버 주소여야 합니다.")
    return base_url


def require_token(value: str | None) -> str:
    token = (value or "").strip()
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise GateError("AI_API_TOKEN은 ASCII여야 합니다.") from exc
    if len(encoded) < 32:
        raise GateError("AI_API_TOKEN은 최소 32자여야 합니다.")
    return token


def request_json(
    url: str,
    *,
    timeout: float,
    token: str | None = None,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    headers = {"Accept": "application/json"}
    body = None
    method = "GET"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError, IncompleteRead) as exc:
        raise TransportError("live API 통신에 실패했습니다.") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ContractError("live API 응답이 허용 크기를 넘습니다.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("live API 응답이 UTF-8 JSON이 아닙니다.") from exc
    if not isinstance(value, dict):
        raise ContractError("live API 응답 최상위 값이 JSON 객체가 아닙니다.")
    return value


def expected_decision(score: float, block: float, review: float) -> str:
    if score >= block:
        return "block"
    if score >= review:
        return "review"
    return "allow"


def validate_health(response: dict[str, object], lock: dict[str, object]) -> None:
    try:
        block = _finite_number(response.get("block_threshold"), "health block_threshold")
        review = _finite_number(response.get("review_threshold"), "health review_threshold")
    except GateError as exc:
        raise ContractError(str(exc)) from exc
    if (
        response.get("status") != "running"
        or response.get("engine") != lock["model_version"]
        or response.get("checkpoint_sha256") != lock["checkpoint_sha256"]
        or response.get("model_bundle_sha256") != lock["model_bundle_sha256"]
        or response.get("auth_required") is not True
        or block != lock["block_threshold"]
        or review != lock["review_threshold"]
    ):
        raise ContractError(
            "live API checkpoint/model bundle, 모델 버전, 인증 또는 threshold가 lock과 다릅니다."
        )


def validate_analysis_response(
    response: dict[str, object],
    event_id: str,
    lock: dict[str, object],
) -> tuple[float, float]:
    try:
        score = _finite_number(response.get("confidence_score"), "confidence_score")
        latency = _finite_number(response.get("latency_ms"), "latency_ms")
    except GateError as exc:
        raise ContractError(str(exc)) from exc
    if not 0 <= score <= 1 or not 0 <= latency <= 600000:
        raise ContractError("live API 점수 또는 지연시간 범위가 잘못되었습니다.")
    decision = expected_decision(
        score,
        float(lock["block_threshold"]),
        float(lock["review_threshold"]),
    )
    if (
        response.get("event_id") != event_id
        or response.get("model_version") != lock["model_version"]
        or response.get("decision") != decision
    ):
        raise ContractError("live API 응답 계약이 lock 또는 요청과 다릅니다.")
    return score, latency


def compute_metrics(labels: list[int], predictions: list[int]) -> dict[str, object]:
    if not labels or len(labels) != len(predictions):
        raise GateError("metrics 입력 길이가 잘못되었습니다.")
    tn = fp = fn = tp = 0
    for label, prediction in zip(labels, predictions):
        if label not in {0, 1} or prediction not in {0, 1}:
            raise GateError("metrics label과 prediction은 0 또는 1이어야 합니다.")
        if label == 1 and prediction == 1:
            tp += 1
        elif label == 1:
            fn += 1
        elif prediction == 1:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def compute_group_metrics(
    rows: list[dict[str, object]],
    predictions: list[int],
    fields: tuple[str, ...],
) -> list[dict[str, object]]:
    if len(rows) != len(predictions):
        raise GateError("subgroup metrics 입력 길이가 잘못되었습니다.")
    groups: dict[tuple[str, ...], tuple[list[int], list[int]]] = {}
    for row, prediction in zip(rows, predictions):
        key = tuple(normalize_text(str(row[field])) for field in fields)
        labels, group_predictions = groups.setdefault(key, ([], []))
        labels.append(int(row["label"]))
        group_predictions.append(prediction)

    result = []
    for key in sorted(groups):
        labels, group_predictions = groups[key]
        result.append(
            {
                **dict(zip(fields, key)),
                "count": len(labels),
                **compute_metrics(labels, group_predictions),
            }
        )
    return result


def summarize_model_latency(values: list[float]) -> dict[str, object]:
    numbers = [_finite_number(value, "model latency_ms") for value in values]
    if not numbers:
        raise GateError("model latency_ms 집계에 유효한 값이 없습니다.")
    if any(not 0 <= value <= 600000 for value in numbers):
        raise GateError("model latency_ms는 0~600000 범위여야 합니다.")

    numbers.sort()
    count = len(numbers)

    def nearest_rank(percent: int) -> float:
        return numbers[(percent * count + 99) // 100 - 1]

    return {
        "count": count,
        "min": numbers[0],
        "mean": math.fsum(numbers) / count,
        "p50": nearest_rank(50),
        "p95": nearest_rank(95),
        "max": numbers[-1],
    }


def invalid_outcome(reason: str, completed: int) -> dict[str, object]:
    return {
        "status": "invalid",
        "reason": reason,
        "evaluated_rows": completed,
        "transport_errors": int(reason == "transport_error"),
        "contract_errors": int(reason == "contract_error"),
        "quality_passed": None,
        "metrics": None,
        "metrics_by_domain": None,
        "metrics_by_domain_style": None,
        "model_latency_ms": None,
    }


def evaluate_rows(rows, lock, request_case) -> dict[str, object]:
    labels: list[int] = []
    predictions: list[int] = []
    latencies: list[float] = []
    for index, row in enumerate(rows, start=1):
        case_hash = hashlib.sha256(str(row["case_id"]).encode("utf-8")).hexdigest()
        event_id = f"ood-final-{index:04d}-{case_hash[:12]}"
        try:
            response = request_case(row, event_id)
            score, latency = validate_analysis_response(response, event_id, lock)
        except TransportError:
            return invalid_outcome("transport_error", index - 1)
        except (ContractError, GateError):
            return invalid_outcome("contract_error", index - 1)
        labels.append(int(row["label"]))
        predictions.append(int(score >= float(lock["block_threshold"])))
        latencies.append(latency)
    metrics = compute_metrics(labels, predictions)
    metrics_by_domain = compute_group_metrics(rows, predictions, ("domain",))
    metrics_by_domain_style = compute_group_metrics(
        rows, predictions, ("domain", "style")
    )
    quality_passed = (
        float(metrics["f1"]) >= float(lock["minimum_f1"])
        and float(metrics["recall"]) >= float(lock["minimum_recall"])
    )
    return {
        "status": "passed" if quality_passed else "quality_failed",
        "reason": None if quality_passed else "minimum_quality_not_met",
        "evaluated_rows": len(rows),
        "transport_errors": 0,
        "contract_errors": 0,
        "quality_passed": quality_passed,
        "metrics": metrics,
        "metrics_by_domain": metrics_by_domain,
        "metrics_by_domain_style": metrics_by_domain_style,
        "model_latency_ms": summarize_model_latency(latencies),
    }


def reserve_result(path: Path, result: dict[str, object]) -> None:
    serialized = json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    try:
        with path.expanduser().open("x", encoding="utf-8") as file_obj:
            file_obj.write(serialized)
            file_obj.flush()
            os.fsync(file_obj.fileno())
    except FileExistsError as exc:
        raise GateError("결과 파일이 이미 있어 1회 평가를 다시 실행하지 않습니다.") from exc
    except OSError as exc:
        raise GateError("결과 파일을 평가 전에 예약할 수 없습니다.") from exc


def finalize_result(path: Path, result: dict[str, object]) -> None:
    serialized = json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    output_path = path.expanduser()
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            file_obj.write(serialized)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, output_path)
    except OSError as exc:
        recovery = f" 임시 결과: {temp_path}" if temp_path is not None else ""
        raise GateError(
            f"완료 결과를 원자적으로 기록할 수 없습니다.{recovery}"
        ) from exc


def run(args: argparse.Namespace) -> int:
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        raise GateError("timeout은 0초 초과 60초 이하여야 합니다.")
    output_path = Path(args.output)

    manifest_path = require_external_file(Path(args.manifest), "lock manifest")
    ood_path = require_external_file(Path(args.ood_csv), "OOD CSV")
    checkpoint_path = require_external_file(Path(args.checkpoint_file), "checkpoint")

    try:
        manifest_bytes = manifest_path.read_bytes()
        ood_bytes = ood_path.read_bytes()
    except OSError as exc:
        raise GateError("lock manifest 또는 OOD CSV를 읽을 수 없습니다.") from exc
    lock = parse_lock(manifest_bytes)
    if sha256_bytes(ood_bytes) != lock["ood_csv_sha256"]:
        raise GateError("OOD CSV SHA-256이 lock manifest와 다릅니다.")
    verify_file_hash(checkpoint_path, str(lock["checkpoint_sha256"]), "checkpoint")
    model_bundle_digest = verify_model_bundle_hash(
        checkpoint_path.parent, str(lock["model_bundle_sha256"])
    )
    rows = parse_ood_rows(ood_bytes)
    validate_ood_design(rows, int(lock["expected_rows"]))
    ensure_no_tracked_overlap(rows)
    base_url = require_base_url(args.base_url or os.environ.get("AI_BASE_URL"))
    token = require_token(os.environ.get("AI_API_TOKEN"))

    result = {
        "gate": "blind_ood_final_v1",
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": lock["model_version"],
        "checkpoint_sha256": lock["checkpoint_sha256"],
        "model_bundle_sha256": model_bundle_digest,
        "ood_csv_sha256": lock["ood_csv_sha256"],
        "lock_manifest_sha256": sha256_bytes(manifest_bytes),
        "block_threshold": lock["block_threshold"],
        "review_threshold": lock["review_threshold"],
        "minimum_f1": lock["minimum_f1"],
        "minimum_recall": lock["minimum_recall"],
        "row_count": len(rows),
        "label_counts": {
            "0": sum(row["label"] == 0 for row in rows),
            "1": sum(row["label"] == 1 for row in rows),
        },
    }
    result.update(invalid_outcome("evaluation_interrupted", 0))
    reserve_result(output_path, result)

    try:
        validate_health(
            request_json(f"{base_url}/", timeout=args.timeout),
            lock,
        )

        def request_case(row, event_id):
            return request_json(
                f"{base_url}/api/v1/analyze",
                timeout=args.timeout,
                token=token,
                payload={
                    "event_id": event_id,
                    "channel": "clipboard",
                    "user_id": "blind-ood-final",
                    "matched_patterns": [],
                    "snippet": row["text"],
                    "metadata": {
                        "evaluation": "blind_ood_final",
                        "case_id_hash": hashlib.sha256(
                            str(row["case_id"]).encode("utf-8")
                        ).hexdigest(),
                    },
                },
            )

        outcome = evaluate_rows(rows, lock, request_case)
    except TransportError:
        outcome = invalid_outcome("transport_error", 0)
    except (ContractError, GateError):
        outcome = invalid_outcome("contract_error", 0)

    result.update(outcome)
    finalize_result(output_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="저장소 밖의 locked JSON")
    parser.add_argument("--ood-csv", required=True, help="저장소 밖의 blind OOD CSV")
    parser.add_argument("--checkpoint-file", required=True, help="실행할 v7 weights 파일")
    parser.add_argument("--output", required=True, help="새 aggregate result JSON")
    parser.add_argument("--base-url", help="기본값: AI_BASE_URL 환경변수")
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except GateError as exc:
        print(f"OOD FINAL GATE FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
