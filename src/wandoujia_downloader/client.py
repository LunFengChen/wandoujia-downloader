"""Async Wandoujia client and verified APK download pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import aiohttp

from .models import ApkArtifact, ApkInspection, ApkJob, SearchResult
from .parsing import (
    DEFAULT_DOWNLOAD_DOMAINS,
    DETAIL_PATH_RE,
    InputError,
    app_page_result,
    apk_job,
    canonical_app_url,
    choose_search_result,
    detail_url_for_version_code,
    detail_urls,
    download_url_sha256,
    history_url,
    host_in_domains,
    looks_like_package_name,
    parse_aapt2_badging,
    redact_url,
    search_results,
    target_path,
    validate_apk_archive,
    validate_download_url,
    validate_wandoujia_request_url,
    validate_wandoujia_url,
)


RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MANIFEST_SCHEMA = "wandoujia-downloader.manifest.v1"
MAX_SEARCH_PAGES = 10


class DownloadError(RuntimeError):
    """A source, transport, or integrity check failed."""


@dataclass(slots=True, frozen=True)
class ResolvedJobs:
    """Target resolution output used by list and download commands."""

    app_url: str
    history_url: str
    jobs: list[ApkJob]
    warnings: list[str]


def _hash_file(path: Path) -> tuple[int, str, str]:
    size = 0
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


def _validate_expected(job: ApkJob, size: int, md5: str) -> None:
    if job.expected_size is not None and size != job.expected_size:
        raise DownloadError(
            f"size mismatch: expected {job.expected_size}, observed {size}"
        )
    if job.expected_md5 is not None and md5.casefold() != job.expected_md5.casefold():
        raise DownloadError(
            f"MD5 mismatch: expected {job.expected_md5}, observed {md5}"
        )


def _validate_observed_metadata(job: ApkJob, observed: ApkInspection) -> None:
    if (
        job.package_name
        and observed.package_name
        and job.package_name != observed.package_name
    ):
        raise DownloadError(
            f"package mismatch: source={job.package_name}, APK={observed.package_name}"
        )
    if (
        job.version_code
        and observed.version_code
        and job.version_code != observed.version_code
    ):
        raise DownloadError(
            f"versionCode mismatch: source={job.version_code}, APK={observed.version_code}"
        )


def _unlink_best_effort(path: Path) -> None:
    """Remove a temporary file without masking the primary failure."""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def inspect_with_aapt2(aapt2: Path, apk_path: Path) -> ApkInspection:
    """Read package metadata with an explicit aapt2 binary."""

    if not aapt2.is_file():
        raise DownloadError(f"aapt2 is missing: {aapt2}")
    process = await asyncio.create_subprocess_exec(
        str(aapt2),
        "dump",
        "badging",
        str(apk_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise DownloadError(f"aapt2 failed ({process.returncode}): {message}")
    try:
        return parse_aapt2_badging(stdout.decode("utf-8", errors="replace"))
    except InputError as error:
        raise DownloadError(str(error)) from error


class WandoujiaClient:
    """Bounded, retrying client for Wandoujia HTML and APK CDN downloads."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        retries: int = 2,
        allowed_download_domains: set[str] | None = None,
    ) -> None:
        if retries < 0 or retries > 8:
            raise InputError("retries must be between 0 and 8")
        self.session = session
        self.retries = retries
        self.allowed_download_domains = set(DEFAULT_DOWNLOAD_DOMAINS)
        if allowed_download_domains:
            self.allowed_download_domains.update(
                item.casefold().strip(".") for item in allowed_download_domains
            )

    async def _retry_delay(self, attempt: int) -> None:
        await asyncio.sleep(min(0.5 * (2**attempt), 4.0))

    async def fetch_page(
        self,
        url: str,
        *,
        accepted_content_types: tuple[str, ...] = ("html", "text"),
    ) -> tuple[str, str]:
        """Fetch one Wandoujia page and retain its validated final URL."""

        validate_wandoujia_request_url(url)
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                async with self.session.get(url, allow_redirects=True) as response:
                    validate_wandoujia_request_url(str(response.url))
                    if response.status in RETRYABLE_STATUSES and attempt < self.retries:
                        await response.read()
                        await self._retry_delay(attempt)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").casefold()
                    if not any(
                        expected in content_type
                        for expected in accepted_content_types
                    ):
                        raise DownloadError(
                            f"unexpected page content type: {content_type or 'missing'}"
                        )
                    return str(response.url), await response.text(errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as error:
                last_error = error
                if attempt >= self.retries:
                    break
                await self._retry_delay(attempt)
        raise DownloadError(f"request failed after {self.retries + 1} attempts: {last_error}")

    async def fetch_text(self, url: str) -> str:
        """Fetch a Wandoujia HTML page with bounded retries."""

        _, body = await self.fetch_page(url)
        return body

    async def fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch and decode one Wandoujia JSON endpoint."""

        _, body = await self.fetch_page(
            url,
            accepted_content_types=("json", "text"),
        )
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise DownloadError(f"invalid JSON response: {error}") from error
        if not isinstance(value, dict):
            raise DownloadError("unexpected JSON response shape")
        return value

    async def resolve_package(self, package_name: str) -> SearchResult:
        """Resolve a package through Wandoujia's package-alias redirect."""

        package_name = package_name.strip()
        if not looks_like_package_name(package_name):
            raise InputError(f"invalid Android package name: {package_name}")
        alias_url = "https://www.wandoujia.com/apps/" + quote(
            package_name,
            safe=".",
        )
        final_url, body = await self.fetch_page(alias_url)
        validate_wandoujia_url(final_url)
        return app_page_result(
            final_url,
            body,
            expected_package=package_name,
        )

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> list[SearchResult]:
        """Search by app name or resolve an exact package alias."""

        query = query.strip()
        if not query:
            raise InputError("search query is empty")
        if limit is not None and limit < 1:
            raise InputError("search limit must be positive")
        if looks_like_package_name(query):
            return [await self.resolve_package(query)]

        url = "https://www.wandoujia.com/search?" + urlencode({"key": query})
        body = await self.fetch_text(url)
        results = search_results(url, body)
        if limit is not None and len(results) >= limit:
            return results[:limit]

        seen = {item.app_id for item in results}
        total_pages = 1
        page = 1
        while page <= min(total_pages, MAX_SEARCH_PAGES):
            api_url = "https://www.wandoujia.com/wdjweb/api/search/more?" + urlencode(
                {"page": page, "key": query}
            )
            try:
                payload = await self.fetch_json(api_url)
            except DownloadError:
                break
            data = payload.get("data")
            if not isinstance(data, dict):
                break
            if page == 1:
                raw_total = data.get("totalPage")
                try:
                    total_pages = max(1, int(raw_total))
                except (TypeError, ValueError):
                    total_pages = 1
            content = data.get("content")
            if isinstance(content, str):
                for item in search_results(api_url, content):
                    if item.app_id not in seen:
                        seen.add(item.app_id)
                        results.append(item)
                        if limit is not None and len(results) >= limit:
                            return results[:limit]
            page += 1
        return results[:limit] if limit is not None else results

    async def resolve_target(self, target: str, select: int | None = None) -> str:
        """Resolve URL, numeric app id, name, or package into one app URL."""

        target = target.strip()
        if not target:
            raise InputError("target is empty")
        if target.isdigit():
            return canonical_app_url(target)
        if target.casefold().startswith("https://"):
            validate_wandoujia_url(target)
            return target
        if looks_like_package_name(target):
            return (await self.resolve_package(target)).app_url
        result = choose_search_result(target, await self.search(target), select)
        return result.app_url

    async def _resolve_one(self, detail_url: str) -> tuple[ApkJob | None, str | None]:
        try:
            body = await self.fetch_text(detail_url)
            return apk_job(detail_url, body), None
        except (aiohttp.ClientError, asyncio.TimeoutError, InputError, DownloadError) as error:
            return None, f"skip {detail_url}: {error}"

    async def resolve_jobs(
        self,
        target: str,
        *,
        select: int | None = None,
        year: str | None = None,
        latest: bool = False,
        limit: int | None = None,
        version: str | None = None,
        version_code: str | None = None,
    ) -> ResolvedJobs:
        """Resolve an app to bounded historical APK jobs."""

        if limit is not None and limit < 1:
            raise InputError("limit must be positive")
        resolved_target = await self.resolve_target(target, select)
        app_id = urlparse(resolved_target).path.split("/apps/", 1)[1].split("/", 1)[0]
        app_url = canonical_app_url(app_id)
        source_url = history_url(resolved_target, year)
        if version_code is not None:
            source_url = detail_url_for_version_code(app_url, version_code)

        body = await self.fetch_text(source_url)
        if DETAIL_PATH_RE.search(urlparse(source_url).path):
            jobs = [apk_job(source_url, body)]
            return ResolvedJobs(app_url, source_url, jobs, [])

        urls = detail_urls(source_url, body)
        if latest:
            urls = urls[:1]
        if limit is not None:
            urls = urls[:limit]
        if not urls:
            jobs = [apk_job(source_url, body)]
            return ResolvedJobs(app_url, source_url, jobs, [])

        resolved = await asyncio.gather(*(self._resolve_one(url) for url in urls))
        jobs = [job for job, _ in resolved if job is not None]
        warnings = [warning for _, warning in resolved if warning is not None]
        if version is not None:
            wanted = version.casefold().removeprefix("v")
            jobs = [
                job
                for job in jobs
                if (job.version or "").casefold().removeprefix("v") == wanted
            ]
            if not jobs:
                raise InputError(f"version not found in resolved history: {version}")
        if not jobs:
            raise DownloadError("no downloadable historical APK was resolved")
        return ResolvedJobs(app_url, source_url, jobs, warnings)

    def _validate_final_download_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.casefold() != "https":
            raise DownloadError("redirected APK URL is not HTTPS")
        if not host_in_domains(parsed.hostname, self.allowed_download_domains):
            raise DownloadError(
                f"redirected to unapproved APK host: {parsed.hostname or '?'}"
            )

    async def _stream_apk(
        self,
        job: ApkJob,
        part_path: Path,
        *,
        max_bytes: int,
    ) -> tuple[int, str, str]:
        validate_download_url(job.download_url, self.allowed_download_domains)
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                _unlink_best_effort(part_path)
                sha256 = hashlib.sha256()
                md5 = hashlib.md5(usedforsecurity=False)
                size = 0
                prefix = bytearray()
                async with self.session.get(
                    job.download_url,
                    allow_redirects=True,
                ) as response:
                    self._validate_final_download_url(str(response.url))
                    if response.status in RETRYABLE_STATUSES and attempt < self.retries:
                        await response.read()
                        await self._retry_delay(attempt)
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise DownloadError(
                            f"declared APK size exceeds --max-bytes: {declared}"
                        )
                    with part_path.open("xb") as output:
                        async for chunk in response.content.iter_chunked(1024 * 256):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > max_bytes:
                                raise DownloadError(
                                    f"APK exceeds --max-bytes while streaming: {max_bytes}"
                                )
                            if len(prefix) < 4:
                                prefix.extend(chunk[: 4 - len(prefix)])
                            sha256.update(chunk)
                            md5.update(chunk)
                            output.write(chunk)
                if bytes(prefix) != b"PK\x03\x04":
                    raise DownloadError("download does not start with ZIP local-file magic")
                observed_md5 = md5.hexdigest()
                _validate_expected(job, size, observed_md5)
                validate_apk_archive(part_path)
                return size, sha256.hexdigest(), observed_md5
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                InputError,
                DownloadError,
            ) as error:
                last_error = error
                _unlink_best_effort(part_path)
                if attempt >= self.retries:
                    break
                await self._retry_delay(attempt)
        raise DownloadError(f"APK download failed after {self.retries + 1} attempts: {last_error}")

    async def download_job(
        self,
        job: ApkJob,
        out_dir: Path,
        *,
        overwrite: bool,
        max_bytes: int,
        aapt2: Path | None,
        show_download_urls: bool,
    ) -> ApkArtifact:
        """Download one APK atomically and return its audited artifact."""

        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_path(out_dir, job)
        part_path = final_path.with_suffix(final_path.suffix + ".part")
        redacted = redact_url(job.download_url, show_download_urls)
        url_hash = download_url_sha256(job.download_url)

        try:
            if final_path.exists() and not overwrite:
                validate_apk_archive(final_path)
                size, sha256, md5 = _hash_file(final_path)
                _validate_expected(job, size, md5)
                observed = (
                    await inspect_with_aapt2(aapt2, final_path)
                    if aapt2 is not None
                    else ApkInspection()
                )
                _validate_observed_metadata(job, observed)
                return ApkArtifact(
                    status="existing",
                    path=str(final_path.resolve()),
                    size=size,
                    sha256=sha256,
                    md5=md5,
                    archive_valid=True,
                    package_name=observed.package_name or job.package_name,
                    version=observed.version or job.version,
                    version_code=observed.version_code or job.version_code,
                    source_detail_url=job.detail_url,
                    source_download_url=redacted,
                    source_download_url_sha256=url_hash,
                    expected_size=job.expected_size,
                    expected_md5=job.expected_md5,
                    expected_crc32=job.expected_crc32,
                    min_sdk=job.min_sdk,
                )

            size, sha256, md5 = await self._stream_apk(
                job,
                part_path,
                max_bytes=max_bytes,
            )
            observed = (
                await inspect_with_aapt2(aapt2, part_path)
                if aapt2 is not None
                else ApkInspection()
            )
            _validate_observed_metadata(job, observed)
            os.replace(part_path, final_path)
            return ApkArtifact(
                status="saved",
                path=str(final_path.resolve()),
                size=size,
                sha256=sha256,
                md5=md5,
                archive_valid=True,
                package_name=observed.package_name or job.package_name,
                version=observed.version or job.version,
                version_code=observed.version_code or job.version_code,
                source_detail_url=job.detail_url,
                source_download_url=redacted,
                source_download_url_sha256=url_hash,
                expected_size=job.expected_size,
                expected_md5=job.expected_md5,
                expected_crc32=job.expected_crc32,
                min_sdk=job.min_sdk,
            )
        except (OSError, InputError, DownloadError) as error:
            _unlink_best_effort(part_path)
            return ApkArtifact(
                status="failed",
                path=None,
                size=None,
                sha256=None,
                md5=None,
                archive_valid=False,
                package_name=job.package_name,
                version=job.version,
                version_code=job.version_code,
                source_detail_url=job.detail_url,
                source_download_url=redacted,
                source_download_url_sha256=url_hash,
                expected_size=job.expected_size,
                expected_md5=job.expected_md5,
                expected_crc32=job.expected_crc32,
                min_sdk=job.min_sdk,
                error=str(error),
            )

    async def download_jobs(
        self,
        jobs: list[ApkJob],
        out_dir: Path,
        *,
        concurrency: int,
        overwrite: bool,
        max_bytes: int,
        aapt2: Path | None,
        show_download_urls: bool,
    ) -> list[ApkArtifact]:
        """Download multiple jobs with an explicit task semaphore."""

        if concurrency < 1 or concurrency > 32:
            raise InputError("concurrency must be between 1 and 32")
        if max_bytes < 1024:
            raise InputError("max-bytes must be at least 1024")
        semaphore = asyncio.Semaphore(concurrency)

        async def one(job: ApkJob) -> ApkArtifact:
            async with semaphore:
                return await self.download_job(
                    job,
                    out_dir,
                    overwrite=overwrite,
                    max_bytes=max_bytes,
                    aapt2=aapt2,
                    show_download_urls=show_download_urls,
                )

        return await asyncio.gather(*(one(job) for job in jobs))


def build_manifest(
    *,
    input_target: str,
    resolved: ResolvedJobs,
    artifacts: list[ApkArtifact],
) -> dict[str, Any]:
    """Build a deterministic, sanitized evidence manifest."""

    counts = {
        status: sum(1 for item in artifacts if item.status == status)
        for status in ("saved", "existing", "failed")
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provider": "wandoujia",
        "input": input_target,
        "resolvedAppUrl": resolved.app_url,
        "historyUrl": resolved.history_url,
        "warnings": resolved.warnings,
        "summary": {"requested": len(resolved.jobs), **counts},
        "artifacts": [asdict(item) for item in artifacts],
    }


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write UTF-8 JSON through an adjacent temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except OSError:
        _unlink_best_effort(temp)
        raise
