from __future__ import annotations

import ast
import math
import secrets
from pathlib import Path
from typing import Annotated, Literal, Optional

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _literal_assignments(name: str) -> dict[str, object]:
    assignments: dict[str, object] = {}
    for node in ast.parse(_source(name)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    assignments[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
    return assignments


def _load_function(name: str, function_name: str, namespace: dict[str, object]):
    tree = ast.parse(_source(name))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), name, "exec"), namespace)
    return namespace[function_name]


def _load_agent_models():
    wanted_assignments = {"ShortText", "MetadataKey", "MetadataString"}
    wanted_classes = {"AgentRequest", "AgentResponse"}
    selected = []
    for node in ast.parse(_source("server.py")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted_assignments
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in wanted_classes:
            selected.append(node)
    namespace = {
        "Annotated": Annotated,
        "Literal": Literal,
        "Optional": Optional,
        "BaseModel": BaseModel,
        "ConfigDict": ConfigDict,
        "Field": Field,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "server.py", "exec"), namespace)
    agent_request = namespace["AgentRequest"]
    agent_response = namespace["AgentResponse"]
    agent_request.model_rebuild(_types_namespace=namespace)
    agent_response.model_rebuild(_types_namespace=namespace)
    return agent_request, agent_response


def test_all_tracked_python_entrypoints_are_syntax_valid() -> None:
    for path in PROJECT_ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_decision_boundaries_match_returned_score() -> None:
    tree = ast.parse(_source("server.py"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "to_decision"
    )
    namespace = {
        "Literal": Literal,
        "BLOCK_THRESHOLD": 0.41,
        "REVIEW_THRESHOLD": 0.20,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "server.py", "exec"), namespace)
    to_decision = namespace["to_decision"]

    assert to_decision(0.1999) == "allow"
    assert to_decision(0.20) == "review"
    assert to_decision(0.4099) == "review"
    assert to_decision(0.41) == "block"

    source = _source("server.py")
    assert source.index("confidence_score = round(") < source.index(
        "decision = to_decision(confidence_score)"
    )


def test_public_tunnel_requires_auth_and_exposes_no_log_reader() -> None:
    source = _source("server.py")

    assert 'os.environ.get("AI_API_TOKEN", "").strip()' in source
    assert 'AI_API_TOKEN.encode("ascii")' in source
    assert "len(AI_API_TOKEN_BYTES) < 32" in source
    assert 'Depends(verify_ai_token)' in source
    assert '@app.get("/api/v1/agent-check")' in source
    assert '@app.get("/api/v1/debug/logs")' not in source
    assert "snippet_preview" not in source
    assert '"user_id": request.user_id' not in source
    assert '"user_id_hash": user_id_hash' in source
    assert '"snippet_length": len(request.snippet)' in source
    assert '"error": str(e)' not in source
    assert 'detail=f"AI 추론' not in source


def test_request_contract_rejects_non_finite_metadata_numbers() -> None:
    source = _source("server.py")

    assert 'ConfigDict(extra="forbid", allow_inf_nan=False)' in source


def test_request_models_enforce_runtime_collection_and_scalar_bounds() -> None:
    agent_request, agent_response = _load_agent_models()
    valid = {
        "event_id": "evt-1",
        "channel": "clipboard",
        "user_id": "device-1",
        "matched_patterns": ["rrn"],
        "snippet": "가상 테스트 900101-1234567",
        "metadata": {"app": "chrome.exe", "score": 0.5},
    }

    assert agent_request(**valid).event_id == "evt-1"
    for invalid in (
        {**valid, "snippet": "x" * 4001},
        {**valid, "matched_patterns": ["x"] * 101},
        {**valid, "matched_patterns": ["x" * 101]},
        {**valid, "metadata": {"score": float("nan")}},
        {**valid, "extra_field": "rejected"},
    ):
        with pytest.raises(ValidationError):
            agent_request(**invalid)

    with pytest.raises(ValidationError):
        agent_response(
            event_id="evt-1",
            decision="allow",
            confidence_score=float("nan"),
            model_version="koelectra-dlp-v7",
            latency_ms=1,
        )


def test_bearer_verifier_accepts_only_the_exact_token() -> None:
    class FakeHttpException(Exception):
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self.detail = detail

    namespace = {
        "Optional": Optional,
        "Header": lambda default=None: default,
        "HTTPException": FakeHttpException,
        "AI_API_TOKEN_BYTES": b"a" * 32,
        "secrets": secrets,
    }
    verify = _load_function("server.py", "verify_ai_token", namespace)

    assert verify("Bearer " + "a" * 32) is None
    for authorization in (
        None,
        "",
        "Basic " + "a" * 32,
        "Bearer wrong",
        "Bearer " + "가" * 32,
    ):
        with pytest.raises(FakeHttpException) as error:
            verify(authorization)
        assert error.value.status_code == 401


def test_cloudflared_is_pinned_verified_and_runtime_cleanup_is_scoped() -> None:
    assignments = _literal_assignments("server.py")
    source = _source("server.py")

    assert assignments["CLOUDFLARED_VERSION"] == "2026.8.2"
    assert assignments["CLOUDFLARED_SHA256"] == (
        "fcfb02b575a52ca1af2e3267af4e1517bcdeb30ac48c834c69abaed3c0576ad2"
    )
    assert "secrets.compare_digest(actual_sha256, CLOUDFLARED_SHA256)" in source
    assert 'os.system("pkill' not in source
    assert "_stop_process(tunnel_process)" in source
    assert "_server_thread.join(timeout=1)" in source
    assert "target=_watch_tunnel" in source
    assert 'if __name__ == "__main__":' in source


def test_tunnel_exit_stops_server_and_intentional_cleanup_is_marked_first() -> None:
    class Server:
        should_exit = False

    class Process:
        @staticmethod
        def wait() -> int:
            return 7

    server = Server()
    watch_tunnel = _load_function("server.py", "_watch_tunnel", {})
    watch_tunnel(Process(), server)
    assert server.should_exit is True

    server.should_exit = False
    order = []

    def stop_process(process) -> None:
        assert server.should_exit is True
        order.append(process)

    stop_runtime = _load_function(
        "server.py",
        "_stop_runtime",
        {"_stop_process": stop_process},
    )
    stop_runtime(server, None, "tunnel")
    assert order == ["tunnel"]


def test_runtime_dependency_major_versions_are_bounded() -> None:
    source = _source("server.py")

    for requirement in (
        '"fastapi>=0.100,<1"',
        '"uvicorn>=0.23,<1"',
        '"pydantic>=2,<3"',
        '"torch>=2,<3"',
        '"transformers>=4.30,<5"',
    ):
        assert requirement in source


def test_batch_response_contract_rejects_semantic_mismatches() -> None:
    validate = _load_function(
        "batch_test_live_api.py",
        "validate_api_response",
        {"math": math},
    )
    valid = {
        "event_id": "batch-12345678",
        "decision": "block",
        "confidence_score": 0.41,
        "model_version": "koelectra-dlp-v7",
        "latency_ms": 12.5,
        "reason": "test",
    }

    assert validate(valid, "batch-12345678") is valid

    invalid_responses = [
        {**valid, "event_id": "wrong"},
        {**valid, "decision": "allow"},
        {**valid, "decision": []},
        {**valid, "confidence_score": True},
        {**valid, "confidence_score": float("nan")},
        {**valid, "model_version": "koelectra-dlp-v6"},
        {**valid, "latency_ms": -1},
    ]
    for response in invalid_responses:
        with pytest.raises(ValueError):
            validate(response, "batch-12345678")


def test_v7_evaluation_scripts_use_tracked_inputs_and_current_thresholds() -> None:
    train = _literal_assignments("train_koelectra_v7.py")
    hard = _literal_assignments("eval_hard_cases.py")
    threshold = _literal_assignments("threshold_analysis.py")
    batch = _literal_assignments("batch_test_live_api.py")

    assert train["DATA_PATH"] == "data/train_dataset_v6.csv"
    assert (PROJECT_ROOT / str(train["DATA_PATH"])).is_file()
    assert hard["MODEL_DIR"] == "./koelectra-dlp-v7/final"
    assert hard["HARD_SET_PATH"] == "data/hard_test_set_v1.csv"
    assert hard["THRESHOLD"] == 0.41
    assert threshold["TEST_PRED_PATH"] == "test_predictions_v7.csv"
    assert threshold["HARD_PRED_PATH"] == "hard_test_results_v7.csv"
    assert threshold["CURRENT_THRESHOLD"] == 0.41
    assert threshold["REVIEW_THRESHOLD"] == 0.20
    assert str(batch["TRAIN_DATA_PATH"]).endswith("train_dataset_v6.csv")
    assert '"channel": "clipboard"' in _source("batch_test_live_api.py")
