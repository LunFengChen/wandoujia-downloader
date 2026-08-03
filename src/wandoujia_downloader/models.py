"""Typed public models used by the downloader."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SearchResult:
    """One app returned by Wandoujia search."""

    app_id: str
    app_url: str
    name: str | None
    package_name: str | None
    version: str | None
    version_code: str | None


@dataclass(slots=True, frozen=True)
class ApkJob:
    """Resolved historical APK and source-side integrity metadata."""

    app_id: str | None
    detail_url: str
    download_url: str
    package_name: str | None
    version: str | None
    version_code: str | None
    release_time: str | None
    year: str | None
    app_name: str | None
    expected_size: int | None
    expected_md5: str | None
    expected_crc32: str | None
    min_sdk: str | None


@dataclass(slots=True, frozen=True)
class ApkInspection:
    """Package metadata observed from a local APK."""

    package_name: str | None = None
    version: str | None = None
    version_code: str | None = None


@dataclass(slots=True, frozen=True)
class ApkArtifact:
    """One download result recorded in the evidence manifest."""

    status: str
    path: str | None
    size: int | None
    sha256: str | None
    md5: str | None
    archive_valid: bool
    package_name: str | None
    version: str | None
    version_code: str | None
    source_detail_url: str
    source_download_url: str
    source_download_url_sha256: str
    expected_size: int | None
    expected_md5: str | None
    expected_crc32: str | None = None
    min_sdk: str | None = None
    error: str | None = None
