from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wandoujia_downloader.cli import build_parser, normalize_argv  # noqa: E402


class CliContractTests(unittest.TestCase):
    def test_legacy_url_invocation_maps_to_download(self) -> None:
        argv = normalize_argv(
            [
                "https://www.wandoujia.com/apps/1/history",
                "--dry-run",
                "--limit",
                "1",
            ]
        )
        self.assertEqual(argv[0], "download")
        args = build_parser().parse_args(argv)
        self.assertEqual(args.command, "download")
        self.assertTrue(args.dry_run)

    def test_search_and_list_subcommands_are_distinct(self) -> None:
        parser = build_parser()
        search = parser.parse_args(["search", "微信", "--json"])
        listed = parser.parse_args(["list", "com.tencent.mm", "--latest"])
        self.assertEqual(search.command, "search")
        self.assertEqual(listed.command, "list")
        self.assertTrue(listed.latest)

    def test_non_positive_limit_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["list", "596157", "--limit", "0"])

    def test_download_defaults_are_bounded(self) -> None:
        args = build_parser().parse_args(["download", "596157", "--latest"])
        self.assertEqual(args.concurrency, 4)
        self.assertEqual(args.retries, 2)
        self.assertEqual(args.max_bytes, 4 * 1024 * 1024 * 1024)

    def test_multiple_history_selectors_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["download", "596157", "--latest", "--version-code", "3120"]
            )


if __name__ == "__main__":
    unittest.main()
