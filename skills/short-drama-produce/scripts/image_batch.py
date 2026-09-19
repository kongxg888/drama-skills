#!/usr/bin/env python3
"""Prepare, confirm, and run a bounded parallel batch of image jobs.

The existing production tool deliberately keeps one job small and auditable.
This wrapper adds the reusable batch behavior the image workflow needs:
actual job count only, one batch preview/confirmation, and parallel child jobs.
Each child still uses the normal production tool and adapter contract.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    import production_tool
except ModuleNotFoundError:  # pragma: no cover - supports direct module loading
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import production_tool


BATCH_SCHEMA = "1.0"
MAX_BATCH_JOBS = 80
BATCH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
MAX_BATCH_BYTES = 20 * 1024 * 1024
MAX_BATCH_RECORD_BYTES = 20 * 1024 * 1024


class BatchConfirmationRequiredError(RuntimeError):
    """The exact current batch has not been explicitly confirmed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _batch_key(batch_id: str) -> str:
    return production_tool.sha256_bytes(batch_id.encode("utf-8"))[:24]


def _batch_preview(batch: Mapping[str, Any], jobs: list[Mapping[str, Any]]) -> dict[str, Any]:
    fingerprint = str(batch["fingerprint"])
    return {
        "batch_id": batch["batch_id"],
        "modality": "image",
        "adapter": batch["adapter"],
        "count": len(jobs),
        "parallelism": min(MAX_BATCH_JOBS, len(jobs)),
        "max_parallelism": MAX_BATCH_JOBS,
        "jobs": [production_tool._preview(job) for job in jobs],
        "confirmation": f"CONFIRM-BATCH {batch['batch_id']} {fingerprint[:12]}",
        "state": "needs_confirmation",
    }


def _read_manifest(manifest_path: Path) -> tuple[str, list[dict[str, Any]]]:
    if manifest_path.stat().st_size > MAX_BATCH_BYTES:
        raise ValueError("image batch manifest is too large")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or set(document) - {"schema_version", "batch_id", "jobs"}:
        raise ValueError("image batch manifest fields are invalid")
    if document.get("schema_version", BATCH_SCHEMA) != BATCH_SCHEMA:
        raise ValueError("unsupported image batch schema")
    batch_id = document.get("batch_id")
    if not isinstance(batch_id, str) or BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("batch_id must be a portable 1-80 character identifier")
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("image batch jobs must be a non-empty list")
    if len(jobs) > MAX_BATCH_JOBS:
        raise ValueError(f"image batch supports at most {MAX_BATCH_JOBS} jobs")
    if not all(isinstance(job, Mapping) for job in jobs):
        raise ValueError("image batch jobs must be objects")
    return batch_id, [dict(job) for job in jobs]


def _normalize_jobs(root: Path, raw_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    outputs: set[str] = set()
    adapter: str | None = None
    for raw in raw_jobs:
        job = production_tool._normalize_job(root, raw)
        if job["modality"] != "image":
            raise ValueError("image batch may contain image jobs only")
        if len(job["outputs"]) != 1:
            raise ValueError("each image batch job must produce exactly one image")
        job_id = str(job["job_id"])
        if job_id in ids:
            raise ValueError(f"duplicate image batch job_id: {job_id}")
        output = str(job["outputs"][0])
        if output in outputs:
            raise ValueError(f"duplicate image batch output: {output}")
        if adapter is None:
            adapter = str(job["adapter"])
        elif str(job["adapter"]) != adapter:
            raise ValueError("one image batch must use one adapter profile")
        ids.add(job_id)
        outputs.add(output)
        normalized.append(job)
    return normalized


def _write_job_and_batch_records(
    root: Path,
    *,
    batch_id: str,
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    job_records = {
        str(job["job_id"]): str(job["fingerprint"])
        for job in jobs
    }
    batch_execution = {
        "schema_version": BATCH_SCHEMA,
        "batch_id": batch_id,
        "modality": "image",
        "adapter": jobs[0]["adapter"],
        "job_ids": [str(job["job_id"]) for job in jobs],
        "fingerprints": job_records,
    }
    batch = dict(batch_execution)
    batch["fingerprint"] = production_tool.sha256_bytes(
        production_tool._canonical(batch_execution)
    )
    batch["prepared_at"] = _now()
    batch["state"] = "needs_confirmation"

    with production_tool._project_lock(root):
        for job in jobs:
            job_id = str(job["job_id"])
            if production_tool._active_run(root, job_id) is not None:
                raise RuntimeError(f"image batch job is already running: {job_id}")
        for job in jobs:
            job_id = str(job["job_id"])
            production_tool._metadata_atomic_json(
                root,
                ("jobs",),
                f"{production_tool._job_key(job_id)}.json",
                job,
            )
            try:
                production_tool._metadata_unlink(
                    root,
                    ("confirmations",),
                    f"{production_tool._job_key(job_id)}.json",
                )
            except FileNotFoundError:
                pass
        production_tool._metadata_atomic_json(
            root,
            ("batches",),
            f"{_batch_key(batch_id)}.json",
            batch,
        )
    return batch


def prepare_batch(project: Path, manifest_path: Path) -> dict[str, Any]:
    root = production_tool.find_project(project)
    batch_id, raw_jobs = _read_manifest(manifest_path)
    jobs = _normalize_jobs(root, raw_jobs)
    batch = _write_job_and_batch_records(root, batch_id=batch_id, jobs=jobs)
    return _batch_preview(batch, jobs)


def _read_batch(root: Path, batch_id: str) -> dict[str, Any]:
    if BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("invalid batch_id")
    document = production_tool._metadata_read_json(
        root,
        ("batches",),
        f"{_batch_key(batch_id)}.json",
        maximum=MAX_BATCH_RECORD_BYTES,
    )
    if not isinstance(document, dict) or document.get("batch_id") != batch_id:
        raise ValueError("image batch record is invalid")
    if document.get("schema_version") != BATCH_SCHEMA or document.get("modality") != "image":
        raise ValueError("image batch record is invalid")
    job_ids = document.get("job_ids")
    fingerprints = document.get("fingerprints")
    if (
        not isinstance(job_ids, list)
        or not job_ids
        or len(job_ids) > MAX_BATCH_JOBS
        or not all(isinstance(job_id, str) for job_id in job_ids)
        or len(set(job_ids)) != len(job_ids)
        or not isinstance(fingerprints, dict)
        or set(fingerprints) != set(job_ids)
        or not all(isinstance(value, str) for value in fingerprints.values())
    ):
        raise ValueError("image batch job list is invalid")
    execution = {
        key: document[key]
        for key in ("schema_version", "batch_id", "modality", "adapter", "job_ids", "fingerprints")
    }
    if document.get("fingerprint") != production_tool.sha256_bytes(
        production_tool._canonical(execution)
    ):
        raise ValueError("image batch fingerprint is invalid")
    return document


def _load_batch_jobs(root: Path, batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for job_id in batch["job_ids"]:
        job = production_tool._read_job(root, str(job_id))
        if job["modality"] != "image" or len(job["outputs"]) != 1:
            raise ValueError(f"image batch child job is invalid: {job_id}")
        if job["fingerprint"] != batch["fingerprints"].get(str(job_id)):
            raise BatchConfirmationRequiredError(
                f"image batch child job changed; prepare the batch again: {job_id}"
            )
        jobs.append(job)
    return jobs


def _batch_inputs_current(root: Path, jobs: list[Mapping[str, Any]]) -> None:
    stale = [
        str(job["job_id"])
        for job in jobs
        if not production_tool._inputs_current(root, job)
    ]
    if stale:
        raise BatchConfirmationRequiredError(
            "image batch inputs changed; prepare the batch again: " + ", ".join(stale)
        )


def _write_child_confirmations(root: Path, jobs: list[Mapping[str, Any]]) -> None:
    confirmed_at = _now()
    for job in jobs:
        job_id = str(job["job_id"])
        receipt = {
            "schema_version": production_tool.JOB_SCHEMA,
            "job_id": job_id,
            "fingerprint": job["fingerprint"],
            "confirmed_at": confirmed_at,
            "consumed_at": None,
            "run_id": None,
        }
        production_tool._metadata_atomic_json(
            root,
            ("confirmations",),
            f"{production_tool._job_key(job_id)}.json",
            receipt,
        )


def confirm_batch(project: Path, *, batch_id: str, confirmation: str) -> dict[str, Any]:
    root = production_tool.find_project(project)
    with production_tool._project_lock(root):
        batch = _read_batch(root, batch_id)
        jobs = _load_batch_jobs(root, batch)
        _batch_inputs_current(root, jobs)
        expected = _batch_preview(batch, jobs)["confirmation"]
        if confirmation != expected:
            raise BatchConfirmationRequiredError(
                "confirmation does not match the exact current image batch"
            )
        _write_child_confirmations(root, jobs)
        receipt = {
            "schema_version": BATCH_SCHEMA,
            "batch_id": batch_id,
            "fingerprint": batch["fingerprint"],
            "confirmed_at": _now(),
            "consumed_at": None,
        }
        production_tool._metadata_atomic_json(
            root,
            ("batch-confirmations",),
            f"{_batch_key(batch_id)}.json",
            receipt,
        )
        batch["state"] = "confirmed"
        production_tool._metadata_atomic_json(
            root,
            ("batches",),
            f"{_batch_key(batch_id)}.json",
            batch,
        )
    return {"batch_id": batch_id, "count": len(jobs), "state": "confirmed"}


def _run_one(root: Path, job_id: str, adapter_config: Path) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        return job_id, production_tool.run_job(
            root, job_id=job_id, adapter_config=adapter_config
        ), None
    except Exception as exc:  # the child run record contains the detailed safe state
        return job_id, None, str(exc)


def run_batch(project: Path, *, batch_id: str, adapter_config: Path) -> dict[str, Any]:
    root = production_tool.find_project(project)
    with production_tool._project_lock(root):
        batch = _read_batch(root, batch_id)
        jobs = _load_batch_jobs(root, batch)
        _batch_inputs_current(root, jobs)
        try:
            receipt = production_tool._metadata_read_json(
                root,
                ("batch-confirmations",),
                f"{_batch_key(batch_id)}.json",
                maximum=MAX_BATCH_RECORD_BYTES,
            )
        except FileNotFoundError as exc:
            raise BatchConfirmationRequiredError("image batch needs explicit confirmation") from exc
        if (
            not isinstance(receipt, dict)
            or receipt.get("fingerprint") != batch["fingerprint"]
            or receipt.get("consumed_at") is not None
        ):
            raise BatchConfirmationRequiredError("image batch needs a new explicit confirmation")
        for job in jobs:
            output = production_tool._project_file(root, str(job["outputs"][0]))
            if output.exists() and not bool(job["overwrite"]):
                raise FileExistsError(f"output exists and overwrite is false: {job['outputs'][0]}")
            if production_tool._active_run(root, str(job["job_id"])) is not None:
                raise RuntimeError(f"image batch job is already running: {job['job_id']}")
        receipt["consumed_at"] = _now()
        production_tool._metadata_atomic_json(
            root,
            ("batch-confirmations",),
            f"{_batch_key(batch_id)}.json",
            receipt,
        )
        batch["state"] = "running"
        batch["started_at"] = _now()
        production_tool._metadata_atomic_json(
            root,
            ("batches",),
            f"{_batch_key(batch_id)}.json",
            batch,
        )

    max_workers = min(MAX_BATCH_JOBS, len(jobs))
    results: list[tuple[str, dict[str, Any] | None, str | None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_run_one, root, str(job["job_id"]), adapter_config)
            for job in jobs
        ]
        for future in futures:
            results.append(future.result())

    succeeded = [result for _, result, error in results if error is None and result is not None]
    failed = [
        {"job_id": job_id, "error": error or "image job failed"}
        for job_id, result, error in results
        if result is None
    ]
    state = "succeeded" if not failed else "partial_failure"
    summary = {
        "batch_id": batch_id,
        "count": len(jobs),
        "parallelism": max_workers,
        "state": state,
        "succeeded": succeeded,
        "failed": failed,
    }
    with production_tool._project_lock(root):
        batch = _read_batch(root, batch_id)
        batch["state"] = state
        batch["finished_at"] = _now()
        batch["result"] = {
            "count": len(jobs),
            "parallelism": max_workers,
            "succeeded_job_ids": [str(item["job_id"]) for item in succeeded],
            "failed_job_ids": [str(item["job_id"]) for item in failed],
        }
        production_tool._metadata_atomic_json(
            root,
            ("batches",),
            f"{_batch_key(batch_id)}.json",
            batch,
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run actual image jobs in one confirmed parallel batch (maximum 80)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Validate and preview an image batch.")
    prepare.add_argument("project")
    prepare.add_argument("--manifest", required=True)
    confirm = commands.add_parser("confirm", help="Confirm the exact image batch.")
    confirm.add_argument("project")
    confirm.add_argument("--batch-id", required=True)
    confirm.add_argument("--confirmation", required=True)
    run = commands.add_parser("run", help="Run a confirmed image batch in parallel.")
    run.add_argument("project")
    run.add_argument("--batch-id", required=True)
    run.add_argument("--adapter-config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_batch(Path(args.project), Path(args.manifest))
        elif args.command == "confirm":
            result = confirm_batch(
                Path(args.project),
                batch_id=args.batch_id,
                confirmation=args.confirmation,
            )
        else:
            result = run_batch(
                Path(args.project),
                batch_id=args.batch_id,
                adapter_config=Path(args.adapter_config),
            )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
