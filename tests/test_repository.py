from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wandoujia_downloader.client import ResolvedJobs, build_manifest  # noqa: E402
from wandoujia_downloader.models import ApkArtifact, ApkJob  # noqa: E402


class RepositoryContractTests(unittest.TestCase):
    def test_manifest_schema_tracks_artifact_fields(self) -> None:
        schema = json.loads((ROOT / "schemas" / "manifest-v1.schema.json").read_text(encoding="utf-8"))
        required = set(schema["properties"]["artifacts"]["items"]["required"])
        for field in (
            "package_name",
            "version",
            "version_code",
            "expected_size",
            "expected_md5",
            "expected_crc32",
            "min_sdk",
        ):
            self.assertIn(field, required)

    def test_manifest_persists_source_crc32_and_min_sdk(self) -> None:
        job = ApkJob(
            app_id="1",
            detail_url="https://www.wandoujia.com/apps/1/history_v2",
            download_url="https://android-apps.pp.cn/a.apk",
            package_name="com.example.app",
            version="1.0",
            version_code="2",
            release_time="2026-07-28T10:00:00+08:00",
            year="2026",
            app_name="Example",
            expected_size=123,
            expected_md5="b" * 32,
            expected_crc32="abcd1234",
            min_sdk="23",
        )
        artifact = ApkArtifact(
            status="saved",
            path="C:/workspace/app.apk",
            size=123,
            sha256="a" * 64,
            md5="b" * 32,
            archive_valid=True,
            package_name="com.example.app",
            version="1.0",
            version_code="2",
            source_detail_url=job.detail_url,
            source_download_url=job.download_url,
            source_download_url_sha256="c" * 64,
            expected_size=job.expected_size,
            expected_md5=job.expected_md5,
            expected_crc32=job.expected_crc32,
            min_sdk=job.min_sdk,
        )
        manifest = build_manifest(
            input_target="com.example.app",
            resolved=ResolvedJobs(
                app_url="https://www.wandoujia.com/apps/1",
                history_url="https://www.wandoujia.com/apps/1/history",
                jobs=[job],
                warnings=[],
            ),
            artifacts=[artifact],
        )
        observed = manifest["artifacts"][0]
        self.assertEqual(observed["expected_crc32"], "abcd1234")
        self.assertEqual(observed["min_sdk"], "23")


if __name__ == "__main__":
    unittest.main()
