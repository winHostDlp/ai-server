# =====================================================================
# Sentry AI DLP 서버 (v8 - Colab 셀 붙여넣기 최종본)
# =====================================================================
print("⏳ 1. 필수 패키지 및 Cloudflare 터널링 도구 설치 중...")
import os
import sys
import time
import re
import json
import hashlib
import queue
import secrets
import shutil
import threading
import subprocess
import traceback
import urllib.request
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

AI_API_TOKEN = os.environ.get("AI_API_TOKEN", "").strip()
try:
    AI_API_TOKEN_BYTES = AI_API_TOKEN.encode("ascii")
except UnicodeEncodeError as exc:
    raise RuntimeError("AI_API_TOKEN은 ASCII 무작위 문자열이어야 합니다.") from exc
if len(AI_API_TOKEN_BYTES) < 32:
    raise RuntimeError(
        "AI_API_TOKEN을 32자 이상의 ASCII 무작위 환경변수로 설정하세요. "
        "Cloudflare 공개 터널은 인증 없이 열지 않습니다."
    )

subprocess.run(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "fastapi>=0.100,<1",
        "uvicorn>=0.23,<1",
        "pydantic>=2,<3",
        "torch>=2,<3",
        "transformers>=4.41,<5",
    ],
    check=True,
)

# 드라이브(FUSE 마운트)는 chmod +x가 제대로 안 먹으므로 /content에 받습니다.
# 공식 release의 버전과 SHA-256을 함께 고정해 임의 최신 바이너리를 실행하지 않습니다.
CLOUDFLARED_PATH = "/content/cloudflared-linux-amd64"
CLOUDFLARED_VERSION = "2026.8.2"
CLOUDFLARED_SHA256 = "fcfb02b575a52ca1af2e3267af4e1517bcdeb30ac48c834c69abaed3c0576ad2"
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cloudflared() -> None:
    if os.path.exists(CLOUDFLARED_PATH) and sha256_file(CLOUDFLARED_PATH) == CLOUDFLARED_SHA256:
        os.chmod(CLOUDFLARED_PATH, 0o755)
        return

    download_path = CLOUDFLARED_PATH + ".download"
    try:
        with urllib.request.urlopen(CLOUDFLARED_URL, timeout=60) as response:
            with open(download_path, "wb") as file_obj:
                shutil.copyfileobj(response, file_obj)
        actual_sha256 = sha256_file(download_path)
        if not secrets.compare_digest(actual_sha256, CLOUDFLARED_SHA256):
            raise RuntimeError("cloudflared SHA-256 검증에 실패했습니다.")
        os.replace(download_path, CLOUDFLARED_PATH)
        os.chmod(CLOUDFLARED_PATH, 0o755)
    finally:
        if os.path.exists(download_path):
            os.remove(download_path)


import torch
import torch.nn.functional as F
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# =====================================================================
# 2. 설정값
# =====================================================================
MODEL_PATH = os.environ.get(
    "SENTRY_MODEL_PATH",
    "/content/drive/MyDrive/2026_졸업과제/도메인편향개선2/koelectra-dlp-v7/final",
)
MODEL_VERSION = "koelectra-dlp-v7"
# 현재 배포 설정. 저장소에 가중치·평가 산출물이 포함되지 않으므로
# eval_hard_cases.py와 threshold_analysis.py를 재실행하기 전에는 재현 검증 완료로 표현하지 않는다.
BLOCK_THRESHOLD = 0.41
REVIEW_THRESHOLD = 0.20

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# =====================================================================
# 2-1. 요청/응답 로깅 설정
# =====================================================================
# 드라이브에 저장해서 Colab 세션이 끊겨도 로그가 남도록 함
LOG_DIR = os.environ.get(
    "SENTRY_LOG_DIR",
    "/content/drive/MyDrive/2026_졸업과제/도메인편향개선2/logs",
)
LOG_PATH = os.path.join(LOG_DIR, "analyze_log.jsonl")
os.makedirs(LOG_DIR, exist_ok=True)
MAX_ANALYZE_BODY_BYTES = 256 * 1024
_LOG_LOCK = threading.Lock()


def log_event(record: dict) -> None:
    """요청/응답 1건을 JSONL 파일에 한 줄씩 append. 로깅 실패가 API 응답을 막지 않도록 예외 흡수."""
    record["logged_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with _LOG_LOCK, open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        traceback.print_exc()


def pseudonymize_user_id(user_id: str) -> str:
    """로그에 사용자 식별자 원문을 남기지 않는다."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


# =====================================================================
# 3. 요청/응답 스키마 (중간보고서 2.2절 공식 스키마 기준)
# =====================================================================
ShortText = Annotated[str, Field(min_length=1, max_length=100)]
MetadataKey = Annotated[str, Field(min_length=1, max_length=100)]
MetadataString = Annotated[str, Field(max_length=500)]


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    event_id: str = Field(..., min_length=1, max_length=120)
    channel: Literal[
        "clipboard",
        "outlook",
        "smtp",
        "web_mail",
        "drive_upload",
        "file_guard",
        "http",
        "usb",
        "web_upload",
        "email_attachment",
        "print",
        "messenger",
    ]
    user_id: str = Field(..., min_length=1, max_length=100)
    matched_patterns: list[ShortText] = Field(default_factory=list, max_length=100)
    snippet: str = Field(..., min_length=1, max_length=4000)
    metadata: dict[
        MetadataKey,
        MetadataString | int | float | bool | None,
    ] = Field(default_factory=dict, max_length=50)


class AgentResponse(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=120)
    decision: Literal["allow", "review", "block"]
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    model_version: str = Field(..., min_length=1, max_length=100)
    latency_ms: float = Field(..., ge=0.0, le=600000.0)
    reason: Optional[Annotated[str, Field(max_length=500)]] = None

def to_decision(confidence_score: float) -> Literal["allow", "review", "block"]:
    if confidence_score >= BLOCK_THRESHOLD:
        return "block"
    elif confidence_score >= REVIEW_THRESHOLD:
        return "review"
    return "allow"


def verify_ai_token(
    authorization: Optional[str] = Header(default=None),
) -> None:
    if not is_valid_ai_token(authorization):
        raise HTTPException(status_code=401, detail="Invalid or missing AI token.")


def is_valid_ai_token(authorization: Optional[str]) -> bool:
    scheme, _, provided_token = (authorization or "").partition(" ")
    try:
        provided_token_bytes = provided_token.encode("ascii")
    except UnicodeEncodeError:
        provided_token_bytes = b""
    return not (
        scheme.lower() != "bearer"
        or not provided_token
        or not secrets.compare_digest(provided_token_bytes, AI_API_TOKEN_BYTES)
    )


def validate_model_config(model_obj) -> None:
    config = model_obj.config
    if getattr(config, "sentry_model_version", "") != MODEL_VERSION:
        raise RuntimeError(
            f"모델 체크포인트 버전이 {MODEL_VERSION}으로 확인되지 않았습니다."
        )
    if int(getattr(config, "num_labels", 0)) != 2:
        raise RuntimeError("모델 분류 헤드는 2개 label이어야 합니다.")
    if getattr(config, "id2label", {}).get(1) != "sensitive":
        raise RuntimeError("모델 label 1은 sensitive여야 합니다.")


class _RequestBodyTooLarge(Exception):
    pass


class AnalyzeRequestGuard:
    """Authenticate and cap analyze bodies before FastAPI parses JSON."""

    def __init__(self, app_obj) -> None:
        self.app = app_obj

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/api/v1/analyze":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        try:
            authorization = headers.get(b"authorization", b"").decode("ascii")
        except UnicodeDecodeError:
            authorization = ""
        if not is_valid_ai_token(authorization):
            await JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing AI token."},
            )(scope, receive, send)
            return

        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse(
                    status_code=400,
                    content={"detail": "Content-Length must be an integer."},
                )(scope, receive, send)
                return
            if declared_size < 0 or declared_size > MAX_ANALYZE_BODY_BYTES:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Analysis request body is too large."},
                )(scope, receive, send)
                return

        received_size = 0

        async def receive_limited():
            nonlocal received_size
            message = await receive()
            if message.get("type") == "http.request":
                received_size += len(message.get("body", b""))
                if received_size > MAX_ANALYZE_BODY_BYTES:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, receive_limited, send)
        except _RequestBodyTooLarge:
            await JSONResponse(
                status_code=413,
                content={"detail": "Analysis request body is too large."},
            )(scope, receive, send)

# =====================================================================
# 4. FastAPI 앱 초기화 및 모델 로드
# =====================================================================
app = FastAPI(title="Sentry AI DLP Server", version=MODEL_VERSION)
app.add_middleware(AnalyzeRequestGuard)

print(f"\n=======================================================")
print(f"🔥 현재 AI 연산 환경: [{DEVICE.upper()}] 🔥")
print(f"=======================================================\n")
print(f"⏳ 2. AI 판별 엔진 가중치 로드 중... ({MODEL_PATH})")

model_loaded = False
model = None
tokenizer = None

try:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH).to(DEVICE)
    validate_model_config(model)
    model.eval()
    warmup_inputs = tokenizer(
        "DLP 모델 준비 상태 확인",
        return_tensors="pt",
        truncation=True,
        max_length=128,
        padding="max_length",
    ).to(DEVICE)
    with torch.inference_mode():
        warmup_logits = model(**warmup_inputs).logits
    if tuple(warmup_logits.shape) != (1, 2) or not torch.isfinite(warmup_logits).all().item():
        raise RuntimeError("모델 준비 추론 결과가 유효하지 않습니다.")
    model_loaded = True
    print("✅ 분석 엔진 탑재 성공! 실시간 탐지 대기 중입니다.")
except Exception as e:
    print(f"❌ 모델 로드 실패: {e}\n경로 또는 모델 파일을 다시 확인해주세요.")
    traceback.print_exc()

# =====================================================================
# 5. 실시간 판별 API 핵심 로직
# =====================================================================
@app.post("/api/v1/analyze", response_model=AgentResponse)
def analyze_text(
    request: AgentRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_ai_token),
):
    user_id_hash = pseudonymize_user_id(request.user_id)
    if not model_loaded:
        log_event({
            "event_id": request.event_id, "channel": request.channel,
            "user_id_hash": user_id_hash, "status": "error",
            "error": "model_not_loaded",
        })
        raise HTTPException(status_code=503, detail="AI 모델이 로드되지 않았습니다. 서버 로그를 확인하세요.")
    if not request.snippet.strip():
        log_event({
            "event_id": request.event_id, "channel": request.channel,
            "user_id_hash": user_id_hash, "status": "error",
            "error": "empty_snippet",
        })
        raise HTTPException(status_code=400, detail="분석할 콘텐츠(snippet)가 비어 있습니다.")

    try:
        start_time = time.perf_counter()
        inputs = tokenizer(
            request.snippet,
            return_tensors="pt",
            truncation=True,
            max_length=128,   # 학습 시 MAX_LENGTH(128)와 통일
            padding="max_length",
        ).to(DEVICE)

        with torch.no_grad():
            outputs = model(**inputs)
            probs = F.softmax(outputs.logits, dim=-1)
            confidence_score = round(probs[0][1].item(), 4)

        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        decision = to_decision(confidence_score)

        reason_map = {
            "block": "AI 문맥 분석 결과 기밀 가능성이 높아 차단 조치",
            "review": "기밀 여부가 애매하여 검토(review) 대상으로 분류",
            "allow": "정상 비즈니스 문맥으로 판정",
        }

        background_tasks.add_task(log_event, {
            "event_id": request.event_id,
            "channel": request.channel,
            "user_id_hash": user_id_hash,
            "snippet_length": len(request.snippet),
            "matched_patterns": request.matched_patterns,
            "status": "ok",
            "decision": decision,
            "confidence_score": round(confidence_score, 4),
            "latency_ms": latency_ms,
        })

        return AgentResponse(
            event_id=request.event_id,
            decision=decision,
            confidence_score=confidence_score,
            model_version=MODEL_VERSION,
            latency_ms=latency_ms,
            reason=reason_map[decision],
        )
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        log_event({
            "event_id": request.event_id, "channel": request.channel,
            "user_id_hash": user_id_hash, "status": "error",
            "error_type": type(e).__name__,
        })
        raise HTTPException(status_code=500, detail="AI 추론 엔진 내부 오류.") from e

@app.get("/")
def health_check():
    return {
        "status": "running" if model_loaded else "model_load_failed",
        "engine": MODEL_VERSION,
        "device": DEVICE,
        "block_threshold": BLOCK_THRESHOLD,
        "review_threshold": REVIEW_THRESHOLD,
        "auth_required": True,
    }


@app.get("/api/v1/agent-check")
def agent_connection_check(_: None = Depends(verify_ai_token)):
    """모델 추론을 실행하지 않고 Host/Web의 AI 토큰만 확인한다."""
    return {"status": "ok", "ai_token": "accepted"}

# =====================================================================
# 6. Cloudflare 백그라운드 서버 가동
# =====================================================================
TUNNEL_TIMEOUT_SEC = 30


def run_server(server_obj) -> None:
    server_obj.run()


def _stop_process(process_obj) -> None:
    if process_obj is None or process_obj.poll() is not None:
        return
    process_obj.terminate()
    try:
        process_obj.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process_obj.kill()
        process_obj.wait(timeout=5)


def _stop_runtime(server_obj, server_thread, tunnel_process) -> None:
    if server_obj is not None:
        server_obj.should_exit = True
    _stop_process(tunnel_process)
    if server_thread is not None and server_thread.is_alive():
        server_thread.join(timeout=5)


def _stop_previous_runtime() -> None:
    previous_server = globals().get("_server")
    previous_thread = globals().get("_server_thread")
    previous_tunnel = globals().get("_tunnel_process")
    if previous_server is not None or previous_tunnel is not None:
        print("♻️  이 셀이 시작한 이전 서버와 터널을 종료합니다...")
        _stop_runtime(previous_server, previous_thread, previous_tunnel)


def _capture_tunnel_url(process_obj) -> Optional[str]:
    if process_obj.stdout is None:
        return None
    output_queue: queue.Queue[Optional[str]] = queue.Queue()
    capture_done = threading.Event()

    def drain_output() -> None:
        try:
            for line in process_obj.stdout:
                if not capture_done.is_set():
                    output_queue.put(line)
        finally:
            if not capture_done.is_set():
                output_queue.put(None)

    threading.Thread(target=drain_output, name="cloudflared-output", daemon=True).start()
    deadline = time.monotonic() + TUNNEL_TIMEOUT_SEC
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = output_queue.get(timeout=min(0.5, max(remaining, 0.01)))
            except queue.Empty:
                if process_obj.poll() is not None:
                    return None
                continue
            if line is None:
                return None
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                return match.group()
        return None
    finally:
        capture_done.set()


def _watch_tunnel(process_obj, server_obj) -> None:
    return_code = process_obj.wait()
    if not server_obj.should_exit:
        print(f"\n❌ Cloudflare 터널이 예기치 않게 종료되었습니다. (exit={return_code})")
        server_obj.should_exit = True


def _start_runtime():
    ensure_cloudflared()
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="error")
    server_obj = uvicorn.Server(config)
    server_thread = threading.Thread(
        target=run_server,
        args=(server_obj,),
        name="sentry-ai-uvicorn",
        daemon=True,
    )
    tunnel_process = None
    try:
        server_thread.start()
        deadline = time.monotonic() + 10
        while not server_obj.started and time.monotonic() < deadline:
            if not server_thread.is_alive():
                raise RuntimeError("Uvicorn 서버가 시작 중 종료되었습니다.")
            time.sleep(0.1)
        if not server_obj.started:
            raise RuntimeError("Uvicorn 서버 시작 시간이 초과되었습니다.")

        print("\n🌐 Cloudflare 안전 터널을 뚫고 있습니다...")
        tunnel_process = subprocess.Popen(
            [CLOUDFLARED_PATH, "tunnel", "--url", "http://127.0.0.1:8000"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        public_url = _capture_tunnel_url(tunnel_process)
        if not public_url:
            raise RuntimeError("Cloudflare 터널 URL 확인 시간이 초과되었습니다.")
        threading.Thread(
            target=_watch_tunnel,
            args=(tunnel_process, server_obj),
            name="cloudflared-watchdog",
            daemon=True,
        ).start()
        return server_obj, server_thread, tunnel_process, public_url
    except Exception:
        _stop_runtime(server_obj, server_thread, tunnel_process)
        raise


def _running_in_notebook() -> bool:
    try:
        from IPython import get_ipython
    except ImportError:
        return False
    return get_ipython() is not None


def main() -> None:
    global _server, _server_thread, _tunnel_process

    _stop_previous_runtime()
    _server, _server_thread, _tunnel_process, public_url = _start_runtime()

    print("\n=======================================================")
    print("🎉 [최종 성공] Cloudflare 터널 연결 완료!")
    print(f"🔗 에이전트 연동 엔드포인트: {public_url}/api/v1/analyze")
    print(f"🔗 Swagger UI 웹 테스트  : {public_url}/docs")
    print("=======================================================\n")
    print(f"⚙️  현재 적용된 threshold: block>={BLOCK_THRESHOLD}, review>={REVIEW_THRESHOLD}")

    if _running_in_notebook():
        print("👉 Colab 셀이 끝나도 현재 커널에서 서버와 터널이 계속 실행됩니다.")
        return

    print("👉 서버 실행 중입니다. 종료하려면 Ctrl+C를 누르세요.")
    tunnel_failed = False
    try:
        while _server_thread.is_alive():
            if _tunnel_process.poll() is not None:
                tunnel_failed = True
                break
            _server_thread.join(timeout=1)
        if _tunnel_process.poll() is not None:
            tunnel_failed = True
    except KeyboardInterrupt:
        print("\n🛑 서버 종료 요청을 받았습니다.")
    finally:
        _stop_runtime(_server, _server_thread, _tunnel_process)
    if tunnel_failed:
        raise RuntimeError("Cloudflare 터널이 종료되어 AI 서버도 중단했습니다.")


if __name__ == "__main__":
    main()
