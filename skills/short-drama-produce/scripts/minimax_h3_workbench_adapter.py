#!/usr/bin/env python3
"""Run the registered MiniMax H3 route used by director-workbench.

The adapter deliberately reads the workbench's local .env.local at runtime.
It never copies credentials into a project, a job, a log, or its JSON output.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any


PROVIDER = "minimax-h3-video"
WORKBENCH_ENV = Path("/Users/mac/Documents/ChatGPT/AI短视频导演工作台/.env.local")
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
POLL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 3600.0


class H3Failure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_response",
        code: str = "provider_error",
        http_status: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.http_status = http_status
        self.request_id = request_id
        self.retryable = retryable

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": PROVIDER,
            "category": self.category,
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.request_id:
            result["request_id"] = self.request_id
        return result


def _dotenv() -> dict[str, str]:
    if not WORKBENCH_ENV.is_file():
        raise H3Failure(
            "registered workbench environment is missing",
            category="configuration",
            code="missing_workbench_env",
        )
    values: dict[str, str] = {}
    for raw_line in WORKBENCH_ENV.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _configuration() -> tuple[str, str, str]:
    values = _dotenv()
    api_key = values.get("MINIMAX_H3_API_KEY", "")
    raw_base = values.get("MINIMAX_H3_BASE_URL", "").rstrip("/")
    raw_path = values.get("MINIMAX_H3_PATH", "")
    if not api_key or not raw_base:
        raise H3Failure(
            "MiniMax H3 configuration is incomplete",
            category="configuration",
            code="incomplete_h3_config",
        )
    parsed = urllib.parse.urlparse(raw_base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise H3Failure(
            "MiniMax H3 base URL is invalid",
            category="configuration",
            code="invalid_base_url",
        )
    base = raw_base if raw_base.endswith("/api/minimax") else f"{raw_base}/api/minimax"
    path = raw_path or "/v2/video_generation"
    if path.startswith("/api/minimax"):
        path = path[len("/api/minimax") :]
    if not path.startswith("/"):
        path = f"/{path}"
    return api_key, base, path


def _safe_request_id(document: object) -> str | None:
    if not isinstance(document, Mapping):
        return None
    for key in ("trace_id", "request_id"):
        value = document.get(key)
        if isinstance(value, str) and 1 <= len(value) <= 200 and value.replace("-", "").replace("_", "").isalnum():
            return value
    return None


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise H3Failure("MiniMax H3 returned an oversized response", code="response_too_large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise H3Failure("MiniMax H3 returned invalid JSON", code="invalid_json") from exc
    if not isinstance(document, dict):
        raise H3Failure("MiniMax H3 returned an invalid response", code="invalid_response")
    return document


def _request_json(
    url: str,
    *,
    api_key: str,
    method: str = "GET",
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {api_key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            if not 200 <= response.status < 300:
                raise H3Failure(
                    "MiniMax H3 request failed",
                    category="provider_response",
                    code=f"http_{response.status}",
                    http_status=response.status,
                )
            return _read_json_response(response)
    except urllib.error.HTTPError as exc:
        category = "authentication" if exc.code == 401 else "permission" if exc.code == 403 else "rate_limit" if exc.code == 429 else "server" if exc.code >= 500 else "invalid_request"
        try:
            document = _read_json_response(exc)
        except H3Failure:
            document = {}
        raise H3Failure(
            "MiniMax H3 HTTP request failed",
            category=category,
            code=f"http_{exc.code}",
            http_status=exc.code,
            request_id=_safe_request_id(document),
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except H3Failure:
        raise
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise H3Failure(
            "MiniMax H3 network request failed",
            category="network",
            code="network_error",
            retryable=True,
        ) from exc


def _multipart_upload(path: Path, *, api_key: str, base: str) -> str:
    size = path.stat().st_size
    if size > MAX_REFERENCE_BYTES:
        raise H3Failure("reference exceeds the H3 input limit", category="invalid_request", code="reference_too_large")
    data = path.read_bytes()
    boundary = f"----codex-h3-{secrets.token_hex(12)}".encode("ascii")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [
        b"--" + boundary + b"\r\n",
        b'Content-Disposition: form-data; name="purpose"\r\n\r\n',
        b"video_generation_input\r\n",
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
        data,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    request = urllib.request.Request(f"{base}/v1/files/upload", data=b"".join(parts), method="POST")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary.decode('ascii')}")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            document = _read_json_response(response)
    except urllib.error.HTTPError as exc:
        raise H3Failure(
            "MiniMax H3 reference upload failed",
            category="invalid_request" if exc.code < 500 else "server",
            code=f"upload_http_{exc.code}",
            http_status=exc.code,
            retryable=exc.code >= 500 or exc.code == 429,
        ) from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise H3Failure("MiniMax H3 reference upload failed", category="network", code="upload_network_error", retryable=True) from exc
    file_id = document.get("file_id")
    nested = document.get("file")
    if isinstance(nested, Mapping):
        file_id = nested.get("file_id", file_id)
        download_url = nested.get("download_url")
    else:
        download_url = document.get("download_url")
    if isinstance(file_id, (str, int)) and str(file_id):
        return f"mm_file://{file_id}"
    if isinstance(download_url, str) and download_url.startswith("https://"):
        return download_url
    raise H3Failure("MiniMax H3 upload returned no usable reference", code="missing_file_reference")


def _reference_contract(prompt: str, job: Mapping[str, Any]) -> str:
    bindings = job.get("reference_bindings")
    references = job.get("references")
    if not isinstance(bindings, list) or not isinstance(references, list) or len(bindings) != len(references):
        raise H3Failure("reference bindings do not match references", category="invalid_request", code="reference_binding_mismatch")
    lines: list[str] = []
    for index, binding in enumerate(bindings, 1):
        if not isinstance(binding, Mapping):
            raise H3Failure("reference binding is invalid", category="invalid_request", code="invalid_reference_binding")
        label = str(binding.get("label", "")).strip()
        role = str(binding.get("role", "")).strip()
        may = binding.get("may_control")
        must = binding.get("must_not_control")
        if not label or not role or not isinstance(may, list) or not may or not isinstance(must, list) or not must:
            raise H3Failure("reference binding semantics are invalid", category="invalid_request", code="invalid_reference_semantics")
        lines.append(
            f"Reference <Picture {index}> ({label}), role {role}. "
            f"May control: {', '.join(str(item).strip() for item in may)}. "
            f"Must not control: {', '.join(str(item).strip() for item in must)}."
        )
    return f"{prompt}\n\nReference contract:\n" + "\n".join(lines)


def _project_input(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise H3Failure("referenced project file is missing", category="invalid_request", code="missing_reference")
    return path


def _write_handle(path_value: object, task_id: str) -> None:
    if not isinstance(path_value, str) or not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps({"provider_job_id": task_id}, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def _task_id(document: Mapping[str, Any]) -> str:
    value = document.get("task_id") or document.get("taskId") or document.get("id")
    if isinstance(value, (str, int)) and str(value):
        return str(value)
    task = document.get("task")
    if isinstance(task, Mapping):
        value = task.get("task_id") or task.get("taskId") or task.get("id")
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    raise H3Failure("MiniMax H3 did not return a task id", code="missing_task_id")


def _task_status(document: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    task = document.get("task")
    task = task if isinstance(task, Mapping) else document
    status = task.get("status") or task.get("task_status") or document.get("status") or document.get("task_status")
    return str(status).casefold(), task


def _video_url(task: Mapping[str, Any]) -> str | None:
    content = task.get("content")
    if isinstance(content, Mapping) and isinstance(content.get("url"), str):
        return content["url"]
    for key in ("video_url", "videoUrl", "url"):
        if isinstance(task.get(key), str):
            return task[key]
    return None


def _poll(task_id: str, *, api_key: str, base: str) -> str:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    terminal_success = {"succeeded", "success", "completed", "complete", "finished", "done"}
    terminal_failure = {"failed", "error", "cancelled", "canceled", "timeout", "expired", "rejected"}
    active = {"queued", "running", "processing", "pending", "in_progress", "created", "submitted", "waiting"}
    while time.monotonic() < deadline:
        document = _request_json(
            f"{base}/v2/query/video_generation/{urllib.parse.quote(task_id, safe='')}",
            api_key=api_key,
        )
        status, task = _task_status(document)
        if status in terminal_success:
            url = _video_url(task)
            if not url:
                raise H3Failure("MiniMax H3 succeeded without a video URL", code="missing_video_url", request_id=task_id)
            return url
        if status in terminal_failure:
            raise H3Failure("MiniMax H3 video task failed", code=f"task_{status}", request_id=task_id)
        if status not in active:
            raise H3Failure("MiniMax H3 returned an unknown task status", code="unknown_task_status", request_id=task_id)
        time.sleep(POLL_SECONDS)
    raise H3Failure("MiniMax H3 polling timed out", category="timeout", code="task_poll_timeout", request_id=task_id, retryable=True)


def _retrieve_file(file_id: str, *, api_key: str, base: str) -> str:
    document = _request_json(
        f"{base}/v1/files/retrieve?file_id={urllib.parse.quote(file_id, safe='')}",
        api_key=api_key,
    )
    file_data = document.get("file") if isinstance(document.get("file"), Mapping) else document
    url = file_data.get("download_url") if isinstance(file_data, Mapping) else None
    if isinstance(url, str) and url.startswith("https://"):
        return url
    raise H3Failure("MiniMax H3 video file has no download URL", code="missing_download_url")


def _download(url: str, target: Path, *, api_key: str, base: str) -> None:
    if url.startswith("mm_file://"):
        url = _retrieve_file(url[len("mm_file://") :], api_key=api_key, base=base)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise H3Failure("MiniMax H3 returned an invalid video URL", code="invalid_video_url")
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_OUTPUT_BYTES:
                raise H3Failure("generated video exceeds the output limit", code="output_too_large")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_OUTPUT_BYTES:
                        raise H3Failure("generated video exceeds the output limit", code="output_too_large")
                    handle.write(chunk)
    except H3Failure:
        target.unlink(missing_ok=True)
        raise
    except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        raise H3Failure("MiniMax H3 video download failed", category="network", code="download_failed", retryable=True) from exc


def _run(job: Mapping[str, Any]) -> dict[str, Any]:
    if job.get("modality") != "video":
        raise H3Failure("H3 adapter requires a video job", category="invalid_request", code="wrong_modality")
    outputs = job.get("outputs")
    if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], str) or not outputs[0].lower().endswith(".mp4"):
        raise H3Failure("H3 adapter requires exactly one MP4 output", category="invalid_request", code="invalid_output")
    api_key, base, submit_path = _configuration()
    collect_id = job.get("collect_provider_job_id")
    if isinstance(collect_id, str) and collect_id:
        task_id = collect_id
    else:
        parameters = job.get("parameters")
        if not isinstance(parameters, Mapping):
            raise H3Failure("video parameters are invalid", category="invalid_request", code="invalid_parameters")
        duration = parameters.get("duration")
        if not isinstance(duration, int) or isinstance(duration, bool) or not 4 <= duration <= 15:
            raise H3Failure("H3 duration must be 4 to 15 seconds", category="invalid_request", code="invalid_duration")
        resolution = parameters.get("resolution", "768P")
        ratio = parameters.get("ratio", "9:16")
        if resolution not in {"480P", "768P", "2K"} or ratio not in {"9:16", "16:9"}:
            raise H3Failure("H3 video parameters are unsupported", category="invalid_request", code="unsupported_parameters")
        prompt = job.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise H3Failure("H3 prompt is empty", category="invalid_request", code="empty_prompt")
        prompt = _reference_contract(prompt, job)
        if len(prompt) > 7000:
            raise H3Failure("H3 prompt exceeds 7000 characters", category="invalid_request", code="prompt_too_long")
        project_root = Path(str(job.get("project_root", ""))).resolve()
        references = job.get("references")
        if not isinstance(references, list) or not 2 <= len(references) <= 9:
            raise H3Failure("H3 requires 2 to 9 reference images", category="invalid_request", code="invalid_reference_count")
        uploaded = [_multipart_upload(_project_input(project_root, str(reference)), api_key=api_key, base=base) for reference in references]
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": reference}, "role": "reference_image"} for reference in uploaded)
        created = _request_json(
            f"{base}{submit_path}",
            api_key=api_key,
            method="POST",
            body={"model": "MiniMax-H3", "duration": duration, "resolution": resolution, "ratio": ratio, "content": content},
        )
        task_id = _task_id(created)
        _write_handle(job.get("handle_path"), task_id)
    url = _poll(task_id, api_key=api_key, base=base)
    output_root = Path(str(job.get("output_root", ""))).resolve()
    target = output_root / Path(str(outputs[0])).name
    _download(url, target, api_key=api_key, base=base)
    return {"provider_job_id": task_id, "outputs": [{"target": outputs[0], "source": str(target)}]}


def main() -> int:
    try:
        job = json.load(sys.stdin.buffer)
        if not isinstance(job, Mapping):
            raise H3Failure("adapter input must be an object", category="invalid_request", code="invalid_job")
        json.dump(_run(job), sys.stdout, ensure_ascii=True)
        return 0
    except H3Failure as exc:
        json.dump({"error": exc.public()}, sys.stdout, ensure_ascii=True)
        print("minimax h3 adapter failed safely", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError) as exc:
        json.dump({"error": {"provider": PROVIDER, "category": "invalid_request", "code": "invalid_job", "retryable": False}}, sys.stdout, ensure_ascii=True)
        print(f"minimax h3 adapter failed safely: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
