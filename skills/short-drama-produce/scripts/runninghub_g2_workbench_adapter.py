#!/usr/bin/env python3
"""Local RunningHub G2 adapter using the registered workbench environment.

This file contains no credential value. It loads the international RunningHub
configuration from the fixed workbench .env.local path at runtime, then uses
the shared RunningHub helper for submission, polling, and download.
"""

import contextlib
import json
import os
import sys
import uuid
from pathlib import Path


MINIMUM_PYTHON = (3, 9)
if sys.version_info < MINIMUM_PYTHON:
    raise RuntimeError("RunningHub G2 adapter needs Python 3.9 or newer")

WORKBENCH_ENV_VARIABLE = "DRAMA_WORKBENCH_ENV"
RUNNINGHUB_HELPER_VARIABLE = "RUNNINGHUB_HELPER_SCRIPTS"
ENDPOINT = "rhart-image-g-2/image-to-image"


def load_workbench_runninghub_env() -> None:
    raw_path = os.environ.get(WORKBENCH_ENV_VARIABLE, "").strip()
    if not raw_path:
        raise RuntimeError("registered workbench environment is not configured")
    workbench_env = Path(raw_path).expanduser()
    if not workbench_env.is_file():
        raise RuntimeError("registered workbench environment is missing")
    for line in workbench_env.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        raw = raw.strip().strip("\"'")
        if key == "RUNNINGHUB_INTL_BASE_URL":
            os.environ["RUNNINGHUB_API_ORIGIN"] = raw.rstrip("/")
        elif key == "RUNNINGHUB_INTL_API_KEY":
            os.environ["RUNNINGHUB_API_KEY"] = raw

def load_runninghub_helper():
    raw_path = os.environ.get(RUNNINGHUB_HELPER_VARIABLE, "").strip()
    if not raw_path:
        raise RuntimeError("RunningHub helper scripts are not configured")
    helper = Path(raw_path).expanduser()
    if not helper.is_dir():
        raise RuntimeError("RunningHub helper scripts are missing")
    sys.path.insert(0, str(helper))
    import importlib

    return importlib.import_module("runninghub")


def write_handle(path: str, task_id: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps({"provider_job_id": str(task_id)}), encoding="utf-8"
    )
    os.replace(temporary, target)


def main() -> int:
    load_workbench_runninghub_env()
    rh = load_runninghub_helper()
    job = json.load(sys.stdin.buffer)
    project_root = Path(job["project_root"])
    output_root = Path(job["output_root"])
    target = job["outputs"][0]
    output = output_root / Path(target).name
    api_key = rh.require_api_key(None)
    endpoint_def = rh.find_endpoint(ENDPOINT)
    if endpoint_def is None:
        raise RuntimeError(f"endpoint unavailable: {ENDPOINT}")

    task_id = job.get("collect_provider_job_id")
    if not task_id:
        refs = []
        for ref in job.get("reference_bindings", []):
            refs.append(str(project_root / ref["path"]))
        contract_lines = ["参考图契约："]
        for ref in sorted(
            job.get("reference_bindings", []), key=lambda item: item["order"]
        ):
            contract_lines.append(
                f"图片{ref['order']}《{ref['label']}》，用途：{ref['role']}；"
                f"可控制：{'、'.join(ref['may_control'])}；"
                f"不得控制：{'、'.join(ref['must_not_control'])}。"
            )
        prompt = job["prompt"] + "\n\n" + "\n".join(contract_lines)
        payload = {
            "prompt": prompt,
            "imageUrls": [
                rh.resolve_media(api_key, ref, force_upload=False) for ref in refs
            ],
            "aspectRatio": job.get("parameters", {}).get("aspectRatio", "9:16"),
            "resolution": job.get("parameters", {}).get("resolution", "4k"),
        }
        with contextlib.redirect_stdout(sys.stderr):
            response = rh.api_post(api_key, f"{rh.BASE_URL}/{ENDPOINT}", payload)
        task_id = response.get("taskId")
        if not task_id:
            raise RuntimeError("RunningHub did not return a taskId")
        try:
            write_handle(job["handle_path"], str(task_id))
        except Exception as exc:
            print(f"handle write warning: {exc}", file=sys.stderr)

    with contextlib.redirect_stdout(sys.stderr):
        final = rh.poll_task(api_key, str(task_id))
    results = final.get("results") or []
    if not results:
        raise RuntimeError("RunningHub returned no image result")
    result_url = results[0].get("url") or results[0].get("outputUrl")
    if not result_url:
        raise RuntimeError("RunningHub result has no downloadable URL")
    with contextlib.redirect_stdout(sys.stderr):
        rh.download_file(str(result_url), str(output))
    print(
        json.dumps(
            {
                "outputs": [{"target": target, "source": str(output)}],
                "provider_job_id": str(task_id),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "provider": "runninghub-g2-image",
                        "category": "provider_response",
                        "code": type(exc).__name__,
                        "retryable": False,
                    }
                },
                ensure_ascii=True,
            )
        )
        raise
