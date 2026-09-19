from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SUITE = Path(__file__).resolve().parents[1]
BATCH_SCRIPT = SUITE / "skills/short-drama-produce/scripts/image_batch.py"
FIXTURE_ADAPTER = SUITE / "skills/short-drama-produce/scripts/fixture_adapter.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


image_batch = load_module("image_batch_under_test", BATCH_SCRIPT)


class ImageBatchTests(unittest.TestCase):
    def make_project(self, directory: str) -> Path:
        root = Path(directory) / "project"
        root.mkdir()
        (root / "short-drama.json").write_text("{}\n", encoding="utf-8")
        return root

    def make_manifest(self, root: Path, count: int = 3) -> Path:
        jobs = []
        for index in range(1, count + 1):
            job_id = f"EP001-IMG-{index:03d}"
            jobs.append(
                {
                    "schema_version": "1.0",
                    "job_id": job_id,
                    "modality": "image",
                    "adapter": "fixture",
                    "prompt": f"test image {index}",
                    "references": [],
                    "outputs": [
                        f"剧集/EP001/制作成果/images/{job_id}.png"
                    ],
                    "parameters": {"resolution": "4K"},
                    "overwrite": False,
                }
            )
        manifest = root.parent / "image-batch.json"
        manifest.write_text(
            json.dumps(
                {"schema_version": "1.0", "batch_id": "EP001-IMAGE-BATCH", "jobs": jobs},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return manifest

    def make_adapter_config(self, directory: str) -> Path:
        config = Path(directory) / "adapters.json"
        config.write_text(
            json.dumps(
                {
                    "adapters": {
                        "fixture": {
                            "command": [sys.executable, str(FIXTURE_ADAPTER)],
                            "timeout_seconds": 30,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_actual_count_is_parallelized_once_and_runs_all_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_project(directory)
            manifest = self.make_manifest(root, count=3)
            preview = image_batch.prepare_batch(root, manifest)

            self.assertEqual(preview["count"], 3)
            self.assertEqual(preview["parallelism"], 3)
            self.assertEqual(preview["max_parallelism"], 80)
            self.assertEqual(len(preview["jobs"]), 3)
            self.assertTrue(preview["confirmation"].startswith("CONFIRM-BATCH "))

            with self.assertRaises(image_batch.BatchConfirmationRequiredError):
                image_batch.confirm_batch(
                    root,
                    batch_id="EP001-IMAGE-BATCH",
                    confirmation="CONFIRM-BATCH wrong",
                )

            image_batch.confirm_batch(
                root,
                batch_id="EP001-IMAGE-BATCH",
                confirmation=preview["confirmation"],
            )
            result = image_batch.run_batch(
                root,
                batch_id="EP001-IMAGE-BATCH",
                adapter_config=self.make_adapter_config(directory),
            )

            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["count"], 3)
            self.assertEqual(result["parallelism"], 3)
            self.assertEqual(len(result["succeeded"]), 3)
            self.assertEqual(result["failed"], [])
            for index in range(1, 4):
                self.assertTrue(
                    (root / f"剧集/EP001/制作成果/images/EP001-IMG-{index:03d}.png").is_file()
                )

    def test_batch_rejects_more_than_eighty_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_project(directory)
            manifest = self.make_manifest(root, count=81)
            with self.assertRaisesRegex(ValueError, "at most 80"):
                image_batch.prepare_batch(root, manifest)


if __name__ == "__main__":
    unittest.main()
