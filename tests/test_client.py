from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wandoujia_downloader.client import (  # noqa: E402
    ResolvedJobs,
    WandoujiaClient,
    build_manifest,
    write_json_atomic,
)
from wandoujia_downloader.models import ApkArtifact, ApkJob, SearchResult  # noqa: E402
from wandoujia_downloader.parsing import InputError, target_path  # noqa: E402


def make_apk(path: Path) -> tuple[int, str]:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", b"dex\n035\0")
    data = path.read_bytes()
    return len(data), hashlib.md5(data, usedforsecurity=False).hexdigest()


def job(expected_size: int | None = None, expected_md5: str | None = None) -> ApkJob:
    return ApkJob(
        app_id="1",
        detail_url="https://www.wandoujia.com/apps/1/history_v2",
        download_url="https://android-apps.pp.cn/a.apk?did=private",
        package_name="com.example.app",
        version="1.2.3",
        version_code="2",
        release_time="2026-07-28T10:00:00+08:00",
        year="2026",
        app_name="Example",
        expected_size=expected_size,
        expected_md5=expected_md5,
        expected_crc32=None,
        min_sdk="23",
    )


class FakeSearchClient(WandoujiaClient):
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results

    async def search(self, query: str) -> list[SearchResult]:
        return self.results


class FakePageClient(WandoujiaClient):
    def __init__(
        self,
        pages: dict[str, tuple[str, str]] | None = None,
        payloads: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.payloads = payloads or {}
        self.json_calls: list[str] = []

    async def fetch_page(
        self,
        url: str,
        *,
        accepted_content_types: tuple[str, ...] = ("html", "text"),
    ) -> tuple[str, str]:
        del accepted_content_types
        return self.pages[url]

    async def fetch_json(self, url: str) -> dict[str, object]:
        self.json_calls.append(url)
        return self.payloads[url]


class ClientResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_target_does_not_need_network_search(self) -> None:
        client = FakeSearchClient([])
        self.assertEqual(
            await client.resolve_target("596157"),
            "https://www.wandoujia.com/apps/596157",
        )

    async def test_direct_history_detail_target_is_preserved(self) -> None:
        client = FakeSearchClient([])
        target = "https://www.wandoujia.com/apps/596157/history_v3120"
        self.assertEqual(await client.resolve_target(target), target)

    async def test_name_target_uses_exact_search_match(self) -> None:
        client = FakeSearchClient(
            [
                SearchResult("1", "https://www.wandoujia.com/apps/1", "Other", "x", None, None),
                SearchResult(
                    "2",
                    "https://www.wandoujia.com/apps/2",
                    "App",
                    "com.example",
                    None,
                    None,
                ),
            ]
        )
        self.assertEqual(
            await client.resolve_target("App"),
            "https://www.wandoujia.com/apps/2",
        )

    async def test_package_alias_redirect_resolves_canonical_app(self) -> None:
        alias = "https://www.wandoujia.com/apps/com.tencent.mm"
        body = """
        <a data-app-id="596157" data-app-name="微信"
          data-app-pname="com.tencent.mm" data-app-vcode="3140"
          data-app-vname="8.0.76"></a>
        """
        client = FakePageClient(
            {alias: ("https://www.wandoujia.com/apps/596157", body)}
        )
        self.assertEqual(
            await client.resolve_target("com.tencent.mm"),
            "https://www.wandoujia.com/apps/596157",
        )

    async def test_package_alias_rejects_mismatched_canonical_metadata(self) -> None:
        alias = "https://www.wandoujia.com/apps/com.tencent.mm"
        body = """
        <a data-app-id="596157" data-app-name="Other"
          data-app-pname="com.example.other" data-app-vcode="1"
          data-app-vname="1.0"></a>
        """
        client = FakePageClient(
            {alias: ("https://www.wandoujia.com/apps/596157", body)}
        )
        with self.assertRaises(InputError):
            await client.resolve_target("com.tencent.mm")

    async def test_name_search_uses_bounded_pagination_and_deduplicates(self) -> None:
        initial_url = "https://www.wandoujia.com/search?key=%E8%81%8A%E5%A4%A9"
        page_one = (
            "https://www.wandoujia.com/wdjweb/api/search/more?"
            "page=1&key=%E8%81%8A%E5%A4%A9"
        )
        page_two = page_one.replace("page=1", "page=2")
        first = """
        <a class="detail-check-btn" data-app-id="1" data-app-name="甲"
          data-app-pname="com.example.one" href="/apps/1"></a>
        """
        second = """
        <a class="detail-check-btn" data-app-id="2" data-app-name="聊天"
          data-app-pname="com.example.two" href="/apps/2"></a>
        """
        client = FakePageClient(
            {initial_url: (initial_url, first)},
            {
                page_one: {"data": {"totalPage": 2, "content": first}},
                page_two: {"data": {"totalPage": 2, "content": second}},
            },
        )
        results = await client.search("聊天")
        self.assertEqual([item.app_id for item in results], ["1", "2"])
        self.assertEqual(client.json_calls, [page_one, page_two])

    async def test_search_limit_skips_pagination_when_initial_page_is_enough(self) -> None:
        initial_url = "https://www.wandoujia.com/search?key=test"
        first = """
        <a class="detail-check-btn" data-app-id="1" data-app-name="Test"
          data-app-pname="com.example.one" href="/apps/1"></a>
        """
        client = FakePageClient({initial_url: (initial_url, first)})
        results = await client.search("test", limit=1)
        self.assertEqual([item.app_id for item in results], ["1"])
        self.assertEqual(client.json_calls, [])

    async def test_name_search_caps_remote_pagination_at_ten_pages(self) -> None:
        initial_url = "https://www.wandoujia.com/search?key=test"
        first = """
        <a class="detail-check-btn" data-app-id="1" data-app-name="Test"
          data-app-pname="com.example.one" href="/apps/1"></a>
        """
        prefix = "https://www.wandoujia.com/wdjweb/api/search/more?"
        payloads = {
            f"{prefix}page={page}&key=test": {
                "data": {"totalPage": 100, "content": ""}
            }
            for page in range(1, 11)
        }
        client = FakePageClient(
            {initial_url: (initial_url, first)},
            payloads,
        )
        results = await client.search("test")
        self.assertEqual([item.app_id for item in results], ["1"])
        self.assertEqual(len(client.json_calls), 10)
        self.assertTrue(client.json_calls[-1].startswith(f"{prefix}page=10&"))

    async def test_existing_verified_apk_is_not_redownloaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            path = target_path(out, job())
            size, md5 = make_apk(path)
            current = job(size, md5)
            client = WandoujiaClient(object())  # session is unused for existing files
            artifact = await client.download_job(
                current,
                out,
                overwrite=False,
                max_bytes=1024 * 1024,
                aapt2=None,
                show_download_urls=False,
            )
        self.assertEqual(artifact.status, "existing")
        self.assertEqual(artifact.size, size)
        self.assertNotIn("private", artifact.source_download_url)

    async def test_existing_hash_mismatch_is_a_failed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            path = target_path(out, job())
            make_apk(path)
            client = WandoujiaClient(object())
            artifact = await client.download_job(
                job(expected_md5="0" * 32),
                out,
                overwrite=False,
                max_bytes=1024 * 1024,
                aapt2=None,
                show_download_urls=False,
            )
        self.assertEqual(artifact.status, "failed")
        self.assertIn("MD5 mismatch", artifact.error or "")


class ManifestTests(unittest.TestCase):
    def test_manifest_counts_and_atomic_write(self) -> None:
        artifact = ApkArtifact(
            status="saved",
            path="C:/workspace/app.apk",
            size=123,
            sha256="a" * 64,
            md5="b" * 32,
            archive_valid=True,
            package_name="com.example",
            version="1.0",
            version_code="1",
            source_detail_url="https://www.wandoujia.com/apps/1/history_v1",
            source_download_url="https://android-apps.pp.cn/a.apk?did=%3Credacted%3E",
            source_download_url_sha256="c" * 64,
            expected_size=123,
            expected_md5="b" * 32,
        )
        resolved = ResolvedJobs(
            app_url="https://www.wandoujia.com/apps/1",
            history_url="https://www.wandoujia.com/apps/1/history",
            jobs=[job()],
            warnings=[],
        )
        manifest = build_manifest(
            input_target="com.example",
            resolved=resolved,
            artifacts=[artifact],
        )
        self.assertEqual(manifest["summary"]["saved"], 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            write_json_atomic(path, manifest)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema"], "wandoujia-downloader.manifest.v1")
            self.assertFalse(path.with_suffix(".json.part").exists())

    def test_manifest_write_failure_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            with patch(
                "wandoujia_downloader.client.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(OSError):
                    write_json_atomic(path, {"schema": "test"})
            self.assertFalse(path.with_suffix(".json.part").exists())


if __name__ == "__main__":
    unittest.main()
