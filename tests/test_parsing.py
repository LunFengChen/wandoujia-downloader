from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wandoujia_downloader.models import ApkJob
from wandoujia_downloader.parsing import (  # noqa: E402
    InputError,
    app_page_result,
    apk_job,
    choose_search_result,
    detail_url_for_version_code,
    detail_urls,
    history_url,
    looks_like_package_name,
    parse_aapt2_badging,
    redact_url,
    search_results,
    target_path,
    validate_apk_archive,
    validate_download_url,
    validate_wandoujia_url,
)


SEARCH_HTML = """
<ul id="j-search-list">
  <li><a class="detail-check-btn" data-app-id="596157"
    data-app-name="微信" data-app-pname="com.tencent.mm"
    data-app-vcode="3140" data-app-vname="8.0.76"
    href="https://www.wandoujia.com/apps/596157">查看</a></li>
  <li><a data-app-id="7451210" data-app-name="Soul"
    data-app-pname="cn.soulapp.android" data-app-vcode="999"
    data-app-vname="5.0" class="other detail-check-btn"
    href="/apps/7451210">查看</a></li>
</ul>
"""

DETAIL_HTML = """
<span class="title">微信</span>
<p class="version-name">官方版本号：<span><a>v8.0.74</a></span></p>
<p class="update-time">更新时间：2026年06月12日 14:41</p>
<a class="normal-dl-btn"
 data-href="https://android-apps.pp.cn/a.apk?size=1234&amp;md5=0123456789abcdef0123456789abcdef&amp;crc32=123&amp;minSDK=24&amp;did=secret-value">普通下载</a>
<a data-app-pname="com.tencent.mm" data-app-vcode="3120"
 data-app-vname="8.0.74"></a>
"""


def make_apk(path: Path, include_manifest: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        if include_manifest:
            archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", b"dex\n035\0")


class UrlAndSearchTests(unittest.TestCase):
    def test_package_name_syntax_is_explicit(self) -> None:
        self.assertTrue(looks_like_package_name("com.tencent.mm"))
        self.assertFalse(looks_like_package_name("微信"))
        self.assertFalse(looks_like_package_name("com..example"))

    def test_validates_only_https_wandoujia_app_urls(self) -> None:
        valid = "https://www.wandoujia.com/apps/596157/history"
        self.assertEqual(validate_wandoujia_url(valid), valid)
        for invalid in (
            "http://www.wandoujia.com/apps/596157",
            "https://example.com/apps/596157",
            "https://www.wandoujia.com/search?q=x",
            "https://user:pass@www.wandoujia.com/apps/596157",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(InputError):
                validate_wandoujia_url(invalid)

    def test_normalizes_history_and_direct_version_urls(self) -> None:
        app = "https://m.wandoujia.com/apps/596157"
        self.assertEqual(
            history_url(app),
            "https://www.wandoujia.com/apps/596157/history",
        )
        self.assertEqual(
            history_url(app, "2025"),
            "https://www.wandoujia.com/apps/596157/history_y2025",
        )
        self.assertEqual(
            detail_url_for_version_code(app, "3120"),
            "https://www.wandoujia.com/apps/596157/history_v3120",
        )

    def test_detail_urls_are_absolute_and_deduplicated(self) -> None:
        body = (
            '<a href="/apps/1/history_v2">a</a>'
            '<a href="https://www.wandoujia.com/apps/1/history_v2">b</a>'
            '<a href="/apps/1/history_v1">c</a>'
        )
        self.assertEqual(
            detail_urls("https://www.wandoujia.com/apps/1/history", body),
            [
                "https://www.wandoujia.com/apps/1/history_v2",
                "https://www.wandoujia.com/apps/1/history_v1",
            ],
        )

    def test_search_parser_extracts_machine_identifiers(self) -> None:
        results = search_results(
            "https://www.wandoujia.com/search?key=x",
            SEARCH_HTML,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].package_name, "com.tencent.mm")
        self.assertEqual(results[0].version_code, "3140")
        self.assertEqual(results[1].app_url, "https://www.wandoujia.com/apps/7451210")

    def test_canonical_app_parser_prefers_complete_current_metadata(self) -> None:
        body = """
        <body data-app-id="596157" data-title="微信" data-pn="com.tencent.mm">
        <a data-app-id="596157" data-app-name="微信"
          data-app-pname="com.tencent.mm" data-app-vcode="3140"
          data-app-vname="8.0.76"></a>
        <a data-app-id="596157" data-app-name="微信"
          data-app-pname="com.tencent.mm" data-app-vcode="3120"
          data-app-vname="8.0.74"></a>
        """
        result = app_page_result(
            "https://www.wandoujia.com/apps/596157",
            body,
            expected_package="com.tencent.mm",
        )
        self.assertEqual(result.app_id, "596157")
        self.assertEqual(result.version, "8.0.76")
        self.assertEqual(result.version_code, "3140")

    def test_exact_package_wins_over_search_order(self) -> None:
        results = search_results("https://www.wandoujia.com/search", SEARCH_HTML)
        selected = choose_search_result("cn.soulapp.android", results)
        self.assertEqual(selected.app_id, "7451210")

    def test_ambiguous_search_requires_select(self) -> None:
        results = search_results("https://www.wandoujia.com/search", SEARCH_HTML)
        with self.assertRaisesRegex(InputError, "--select"):
            choose_search_result("聊天", results)
        self.assertEqual(choose_search_result("聊天", results, 2).app_id, "7451210")


class DetailAndIntegrityTests(unittest.TestCase):
    def test_apk_job_parses_source_integrity_metadata(self) -> None:
        job = apk_job(
            "https://www.wandoujia.com/apps/596157/history_v3120",
            DETAIL_HTML,
        )
        self.assertEqual(job.package_name, "com.tencent.mm")
        self.assertEqual(job.version, "8.0.74")
        self.assertEqual(job.version_code, "3120")
        self.assertEqual(job.release_time, "2026-06-12T14:41:00+08:00")
        self.assertEqual(job.expected_size, 1234)
        self.assertEqual(job.expected_md5, "0123456789abcdef0123456789abcdef")
        self.assertEqual(job.min_sdk, "24")

    def test_download_url_redacts_did_but_preserves_integrity_fields(self) -> None:
        job = apk_job(
            "https://www.wandoujia.com/apps/596157/history_v3120",
            DETAIL_HTML,
        )
        value = redact_url(job.download_url)
        self.assertIn("did=%3Credacted%3E", value)
        self.assertIn("size=1234", value)
        self.assertNotIn("secret-value", value)

    def test_download_host_gate_accepts_expected_cdns(self) -> None:
        for url in (
            "https://android-apps.pp.cn/a.apk",
            "https://ucdl.25pp.com/a.apk",
        ):
            self.assertEqual(validate_download_url(url), url)
        with self.assertRaises(InputError):
            validate_download_url("https://example.com/a.apk")

    def test_target_name_includes_version_code_and_full_date(self) -> None:
        job = apk_job(
            "https://www.wandoujia.com/apps/596157/history_v3120",
            DETAIL_HTML,
        )
        self.assertEqual(
            target_path(Path("out"), job).name,
            "com.tencent.mm-8.0.74-3120-20260612.apk",
        )

    def test_apk_archive_requires_android_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.apk"
            invalid = Path(directory) / "invalid.apk"
            make_apk(valid)
            make_apk(invalid, include_manifest=False)
            validate_apk_archive(valid)
            with self.assertRaisesRegex(InputError, "AndroidManifest"):
                validate_apk_archive(invalid)

    def test_aapt2_badging_parser(self) -> None:
        observed = parse_aapt2_badging(
            "package: name='com.tencent.mm' versionCode='3120' "
            "versionName='8.0.74' platformBuildVersionName=''\n"
        )
        self.assertEqual(observed.package_name, "com.tencent.mm")
        self.assertEqual(observed.version_code, "3120")

    def test_full_url_hash_is_not_the_download_md5(self) -> None:
        job = apk_job(
            "https://www.wandoujia.com/apps/596157/history_v3120",
            DETAIL_HTML,
        )
        self.assertNotEqual(
            hashlib.sha256(job.download_url.encode()).hexdigest(),
            job.expected_md5,
        )


if __name__ == "__main__":
    unittest.main()
