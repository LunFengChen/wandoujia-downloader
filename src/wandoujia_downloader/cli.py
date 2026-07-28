"""Command-line interface for historical APK acquisition."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp

from . import __version__
from .client import (
    DownloadError,
    WandoujiaClient,
    build_manifest,
    write_json_atomic,
)
from .models import ApkJob, SearchResult
from .parsing import InputError, download_url_sha256, redact_url, target_path


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
COMMANDS = {"search", "list", "download"}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def bounded_timeout(value: str) -> float:
    parsed = float(value)
    if parsed < 1 or parsed > 600:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 600 seconds")
    return parsed


def _add_network_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=bounded_timeout,
        default=30.0,
        help="total HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        choices=range(0, 9),
        default=2,
        metavar="N",
        help="retries for transient failures, 0-8 (default: 2)",
    )
    parser.add_argument(
        "--no-trust-env",
        action="store_true",
        help="ignore HTTP(S)_PROXY and other aiohttp environment settings",
    )


def _add_selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        help="Wandoujia URL, numeric app id, app name, or package name",
    )
    parser.add_argument(
        "--select",
        type=positive_int,
        help="choose a one-based search result when the target is ambiguous",
    )
    parser.add_argument("--year", help="use one history year page, e.g. 2025")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--latest",
        action="store_true",
        help="only resolve the first historical version",
    )
    selector.add_argument(
        "--limit",
        type=positive_int,
        help="maximum number of historical detail pages to resolve",
    )
    selector.add_argument(
        "--version",
        help="keep jobs whose displayed version name matches exactly",
    )
    selector.add_argument(
        "--version-code",
        help="resolve one direct Wandoujia history_v version code",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wandoujia-downloader",
        description="Search, list, download, and verify Wandoujia historical APKs",
    )
    parser.add_argument(
        "--tool-version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="search apps by name or package")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=positive_int, default=20)
    search_parser.add_argument("--json", action="store_true")
    _add_network_options(search_parser)

    list_parser = subparsers.add_parser("list", help="resolve historical versions")
    _add_selection_options(list_parser)
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument(
        "--show-download-urls",
        action="store_true",
        help="include full source URLs instead of redacting token-like query values",
    )
    _add_network_options(list_parser)

    download_parser = subparsers.add_parser(
        "download",
        help="download historical APKs with integrity checks",
    )
    _add_selection_options(download_parser)
    download_parser.add_argument(
        "-o",
        "--out-dir",
        default=".",
        help="APK output directory (default: current directory)",
    )
    download_parser.add_argument(
        "--manifest",
        help="evidence manifest path (default: OUT_DIR/wandoujia-manifest.json)",
    )
    download_parser.add_argument(
        "-c",
        "--concurrency",
        type=positive_int,
        default=4,
        help="concurrent APK downloads, 1-32 (default: 4)",
    )
    download_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="atomically replace an existing canonical APK",
    )
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print jobs without downloading",
    )
    download_parser.add_argument("--json", action="store_true")
    download_parser.add_argument(
        "--show-download-urls",
        action="store_true",
        help="persist full source URLs instead of redacting token-like query values",
    )
    download_parser.add_argument(
        "--allow-download-host",
        action="append",
        default=[],
        metavar="DOMAIN",
        help="add an expected APK CDN base domain; may be repeated",
    )
    download_parser.add_argument(
        "--max-bytes",
        type=positive_int,
        default=4 * 1024 * 1024 * 1024,
        help="per-APK byte ceiling (default: 4 GiB)",
    )
    download_parser.add_argument(
        "--aapt2",
        help="explicit aapt2 path for package/versionCode verification",
    )
    _add_network_options(download_parser)
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    """Preserve the original `URL [download options]` invocation."""

    if not argv:
        return argv
    if argv[0] in COMMANDS or argv[0] in {"-h", "--help", "--tool-version"}:
        return argv
    return ["download", *argv]


def _search_payload(query: str, results: list[SearchResult]) -> dict[str, Any]:
    return {
        "schema": "wandoujia-downloader.search.v1",
        "query": query,
        "count": len(results),
        "results": [asdict(item) for item in results],
    }


def _job_payload(job: ApkJob, show_download_urls: bool) -> dict[str, Any]:
    value = asdict(job)
    value["download_url"] = redact_url(job.download_url, show_download_urls)
    value["download_url_sha256"] = download_url_sha256(job.download_url)
    return value


def _list_payload(args: argparse.Namespace, resolved: Any) -> dict[str, Any]:
    return {
        "schema": "wandoujia-downloader.list.v1",
        "input": args.target,
        "resolvedAppUrl": resolved.app_url,
        "historyUrl": resolved.history_url,
        "count": len(resolved.jobs),
        "warnings": resolved.warnings,
        "jobs": [
            _job_payload(job, args.show_download_urls) for job in resolved.jobs
        ],
    }


def _print_search(query: str, results: list[SearchResult], as_json: bool) -> None:
    if as_json:
        print(json.dumps(_search_payload(query, results), ensure_ascii=False, indent=2))
        return
    for index, item in enumerate(results, 1):
        print(
            f"[{index}] {item.name or '?'} | {item.package_name or '?'} | "
            f"{item.version or '?'} ({item.version_code or '?'}) | {item.app_url}"
        )


def _print_jobs(args: argparse.Namespace, resolved: Any) -> None:
    payload = _list_payload(args, resolved)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for warning in resolved.warnings:
        print(f"[warn] {warning}", file=sys.stderr)
    for index, job in enumerate(resolved.jobs, 1):
        print(
            f"[{index}] {job.package_name or '?'} {job.version or '?'} "
            f"versionCode={job.version_code or '?'} date={job.release_time or '?'}"
        )
        print(f"    detail: {job.detail_url}")
        print(
            "    download: "
            + redact_url(job.download_url, args.show_download_urls)
        )
        print(f"    target: {target_path(Path('.'), job).name}")
        print(
            f"    expected: size={job.expected_size or '?'} "
            f"md5={job.expected_md5 or '?'} minSDK={job.min_sdk or '?'}"
        )


async def async_main(args: argparse.Namespace) -> int:
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector_limit = max(4, min(getattr(args, "concurrency", 4), 32))
    connector = aiohttp.TCPConnector(limit=connector_limit)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.wandoujia.com/",
        "Accept": (
            "application/json,text/html,application/xhtml+xml,"
            "application/vnd.android.package-archive,*/*;q=0.8"
        ),
    }
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers=headers,
        trust_env=not args.no_trust_env,
    ) as session:
        client = WandoujiaClient(
            session,
            retries=args.retries,
            allowed_download_domains=set(
                getattr(args, "allow_download_host", [])
            ),
        )
        if args.command == "search":
            results = await client.search(args.query, limit=args.limit)
            _print_search(args.query, results, args.json)
            return 0 if results else 2

        resolved = await client.resolve_jobs(
            args.target,
            select=args.select,
            year=args.year,
            latest=args.latest,
            limit=args.limit,
            version=args.version,
            version_code=args.version_code,
        )
        if args.command == "list" or args.dry_run:
            _print_jobs(args, resolved)
            return 0

        out_dir = Path(args.out_dir).expanduser().resolve()
        manifest_path = (
            Path(args.manifest).expanduser().resolve()
            if args.manifest
            else out_dir / "wandoujia-manifest.json"
        )
        aapt2 = Path(args.aapt2).expanduser().resolve() if args.aapt2 else None
        artifacts = await client.download_jobs(
            resolved.jobs,
            out_dir,
            concurrency=args.concurrency,
            overwrite=args.overwrite,
            max_bytes=args.max_bytes,
            aapt2=aapt2,
            show_download_urls=args.show_download_urls,
        )
        manifest = build_manifest(
            input_target=args.target,
            resolved=resolved,
            artifacts=artifacts,
        )
        write_json_atomic(manifest_path, manifest)
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            for item in artifacts:
                if item.status == "failed":
                    print(
                        f"[failed] {item.source_detail_url}: {item.error}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[{item.status}] {item.path} | size={item.size} "
                        f"sha256={item.sha256}"
                    )
            print(f"manifest: {manifest_path}")
        failed = manifest["summary"]["failed"]
        completed = manifest["summary"]["saved"] + manifest["summary"]["existing"]
        if failed == 0 and completed > 0:
            return 0
        return 3 if completed > 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(raw)
    try:
        return asyncio.run(async_main(args))
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        DownloadError,
        InputError,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
