"""Pure parsing, validation, and file-name helpers."""

from __future__ import annotations

import hashlib
import html
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

from .models import ApkInspection, ApkJob, SearchResult


WANDOUJIA_HOSTS = {"wandoujia.com", "www.wandoujia.com", "m.wandoujia.com"}
DEFAULT_DOWNLOAD_DOMAINS = {"pp.cn", "25pp.com"}
REDACTED_QUERY_KEYS = {
    "access_token",
    "auth",
    "did",
    "key",
    "password",
    "secret",
    "sign",
    "signature",
    "token",
}

DETAIL_RE = re.compile(
    r"https?://(?:www\.|m\.)?wandoujia\.com/apps/\d+/history_v\d+"
    r"|/apps/\d+/history_v\d+",
    re.I,
)
APP_ID_RE = re.compile(r"/apps/(\d+)")
DETAIL_PATH_RE = re.compile(r"/history_v\d+/?$", re.I)
HISTORY_YEAR_RE = re.compile(r"history_y(\d{4})", re.I)
PACKAGE_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
DATA_HREF_RE = re.compile(r"data-href=[\"']([^\"']+\.apk[^\"']*)[\"']", re.I)
HREF_APK_RE = re.compile(r"href=[\"']([^\"']+\.apk[^\"']*)[\"']", re.I)
APP_PNAME_RE = re.compile(
    r"data-(?:app-)?pname=[\"']([^\"']+)[\"']"
    r"|data-pn=[\"']([^\"']+)[\"']",
    re.I,
)
APP_VNAME_RE = re.compile(r"data-app-vname=[\"']v?([^\"']+)[\"']", re.I)
APP_VCODE_RE = re.compile(r"data-app-vcode=[\"']([^\"']+)[\"']", re.I)
VERSION_TEXT_RE = re.compile(
    r"官方版本号\s*[:：]\s*<span>\s*<a[^>]*>\s*v?([^<]+)",
    re.I,
)
UPDATE_TIME_RE = re.compile(
    r"更新时间\s*[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
    r"(?:\s+(\d{1,2}):(\d{2}))?",
    re.I,
)
TITLE_RE = re.compile(
    r"<span[^>]+class=[\"']title[\"'][^>]*>(.*?)</span>|<title>(.*?)</title>",
    re.I | re.S,
)
ANCHOR_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>", re.I | re.S)
APP_META_TAG_RE = re.compile(r"<(?:body|a)\b(?P<attrs>[^>]*)>", re.I | re.S)
ATTRIBUTE_RE = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.S)
AAPT_PACKAGE_RE = re.compile(
    r"package:\s+name='([^']+)'\s+versionCode='([^']*)'\s+versionName='([^']*)'",
)


class InputError(ValueError):
    """Invalid or ambiguous user input."""


def first_group(match: re.Match[str] | None) -> str | None:
    """Return the first non-empty regex group without markup."""

    if match is None:
        return None
    for value in match.groups():
        if value:
            text = re.sub(r"<.*?>", "", value).strip()
            return html.unescape(text)
    return None


def safe_part(value: str | None, fallback: str) -> str:
    """Make one portable file-name component."""

    text = html.unescape(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-+")
    return text or fallback


def clean_version(value: str | None) -> str | None:
    """Normalize a displayed version without inventing semantics."""

    if value is None:
        return None
    text = html.unescape(value).strip()
    text = text.removeprefix("v").removeprefix("V")
    text = re.sub(r"[^0-9A-Za-z._+-]+", "_", text).strip("._-+")
    return text or None


def unique(items: list[str]) -> list[str]:
    """Deduplicate strings while retaining source order."""

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def app_id_from_url(url: str) -> str | None:
    """Extract the numeric Wandoujia app id."""

    match = APP_ID_RE.search(url)
    return match.group(1) if match else None


def validate_wandoujia_url(url: str) -> str:
    """Validate a HTTPS Wandoujia app URL and return it unchanged."""

    validate_wandoujia_request_url(url)
    parsed = urlparse(url)
    if app_id_from_url(parsed.path) is None:
        raise InputError("Wandoujia URL must contain /apps/<id>")
    return url


def validate_wandoujia_request_url(url: str) -> str:
    """Validate scheme and authority for any Wandoujia page request."""

    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise InputError("Wandoujia URL must use HTTPS")
    if parsed.username or parsed.password:
        raise InputError("Wandoujia URL must not contain user information")
    if (parsed.hostname or "").lower() not in WANDOUJIA_HOSTS:
        raise InputError("URL host is not Wandoujia")
    return url


def canonical_app_url(app_id: str) -> str:
    """Build the canonical desktop app URL."""

    if not app_id.isdigit():
        raise InputError("app id must be numeric")
    return f"https://www.wandoujia.com/apps/{app_id}"


def history_url(url: str, year: str | None = None) -> str:
    """Normalize an app/history URL to the requested history surface."""

    validate_wandoujia_url(url)
    app_id = app_id_from_url(url)
    assert app_id is not None
    if year is not None:
        if not re.fullmatch(r"\d{4}", year):
            raise InputError("year must contain four digits")
        return f"https://www.wandoujia.com/apps/{app_id}/history_y{year}"
    if DETAIL_PATH_RE.search(urlparse(url).path):
        return url
    return f"https://www.wandoujia.com/apps/{app_id}/history"


def detail_url_for_version_code(url: str, version_code: str) -> str:
    """Build a direct historical detail URL from a Wandoujia version code."""

    validate_wandoujia_url(url)
    if not version_code.isdigit():
        raise InputError("version code must be numeric")
    app_id = app_id_from_url(url)
    assert app_id is not None
    return f"https://www.wandoujia.com/apps/{app_id}/history_v{version_code}"


def absolute_url(value: str, base_url: str) -> str:
    """Decode HTML entities and absolutize a URL."""

    return html.unescape(urljoin(base_url, value.strip()))


def detail_urls(page_url: str, page_body: str) -> list[str]:
    """Extract all historical detail URLs already embedded in the page."""

    return unique(
        [absolute_url(match.group(0), page_url) for match in DETAIL_RE.finditer(page_body)]
    )


def _attributes(raw: str) -> dict[str, str]:
    return {
        name.lower(): html.unescape(value.strip())
        for name, _, value in ATTRIBUTE_RE.findall(raw)
    }


def looks_like_package_name(value: str) -> bool:
    """Return whether a target has Android package-name syntax."""

    return PACKAGE_NAME_RE.fullmatch(value.strip()) is not None


def app_page_result(
    page_url: str,
    page_body: str,
    *,
    expected_package: str | None = None,
) -> SearchResult:
    """Parse the canonical app identity from a redirected app detail page."""

    app_id = app_id_from_url(urlparse(page_url).path)
    if app_id is None:
        raise InputError("app detail page did not resolve to /apps/<id>")

    folded = expected_package.casefold() if expected_package else None
    best: SearchResult | None = None
    best_score = -1
    for match in APP_META_TAG_RE.finditer(page_body):
        attrs = _attributes(match.group("attrs"))
        candidate_id = attrs.get("data-app-id") or attrs.get("cache-app-id")
        if candidate_id != app_id:
            continue
        package = attrs.get("data-app-pname") or attrs.get("data-pn")
        if not package or (folded and package.casefold() != folded):
            continue
        name = (
            attrs.get("data-app-name")
            or attrs.get("data-name")
            or attrs.get("data-title")
        )
        version = clean_version(attrs.get("data-app-vname"))
        version_code = attrs.get("data-app-vcode")
        score = sum(
            value is not None for value in (name, version, version_code)
        )
        if score > best_score:
            best = SearchResult(
                app_id=app_id,
                app_url=canonical_app_url(app_id),
                name=name,
                package_name=package,
                version=version,
                version_code=version_code,
            )
            best_score = score

    if best is None:
        suffix = f" for package {expected_package}" if expected_package else ""
        raise InputError(f"canonical app metadata was not found{suffix}")
    return best


def search_results(page_url: str, page_body: str) -> list[SearchResult]:
    """Parse search cards from the server-rendered result page."""

    results: list[SearchResult] = []
    seen: set[tuple[str, str | None]] = set()
    for match in ANCHOR_RE.finditer(page_body):
        attrs = _attributes(match.group("attrs"))
        classes = set(attrs.get("class", "").split())
        if "detail-check-btn" not in classes:
            continue
        app_id = attrs.get("data-app-id")
        href = attrs.get("href")
        if not app_id and href:
            app_id = app_id_from_url(href)
        if not app_id or not app_id.isdigit():
            continue
        package = attrs.get("data-app-pname") or attrs.get("data-pn")
        key = (app_id, package)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            SearchResult(
                app_id=app_id,
                app_url=canonical_app_url(app_id),
                name=attrs.get("data-app-name") or attrs.get("data-name"),
                package_name=package,
                version=clean_version(attrs.get("data-app-vname")),
                version_code=attrs.get("data-app-vcode"),
            )
        )
    return results


def choose_search_result(
    query: str,
    results: list[SearchResult],
    select: int | None = None,
) -> SearchResult:
    """Choose an exact result or require an explicit one-based index."""

    if not results:
        raise InputError(f"no Wandoujia app matched: {query}")
    if select is not None:
        if select < 1 or select > len(results):
            raise InputError(f"--select must be between 1 and {len(results)}")
        return results[select - 1]

    folded = query.casefold()
    exact_package = [
        item for item in results if (item.package_name or "").casefold() == folded
    ]
    if len(exact_package) == 1:
        return exact_package[0]
    exact_name = [item for item in results if (item.name or "").casefold() == folded]
    if len(exact_name) == 1:
        return exact_name[0]
    if len(results) == 1:
        return results[0]

    preview = "; ".join(
        f"{index}:{item.name or '?'} ({item.package_name or '?'})"
        for index, item in enumerate(results[:10], 1)
    )
    raise InputError(f"ambiguous target; use --select N. Candidates: {preview}")


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _numeric_query_value(query: dict[str, list[str]], key: str) -> int | None:
    value = _query_value(query, key)
    return int(value) if value and value.isdigit() else None


def download_url(detail_url: str, page_body: str) -> str:
    """Extract the direct APK URL from a historical detail page."""

    for regex in (DATA_HREF_RE, HREF_APK_RE):
        match = regex.search(page_body)
        if match:
            return absolute_url(match.group(1), detail_url)

    match = re.search(r"downloadUrl=([^\"'&]+)", page_body)
    if match:
        return html.unescape(unquote(match.group(1)))
    raise InputError(f"APK URL not found: {detail_url}")


def _release_time(page_body: str) -> tuple[str | None, str | None]:
    match = UPDATE_TIME_RE.search(page_body)
    if match is None:
        return None, None
    year, month, day, hour, minute = match.groups()
    stamp = f"{year}-{int(month):02d}-{int(day):02d}"
    if hour and minute:
        stamp += f"T{int(hour):02d}:{int(minute):02d}:00+08:00"
    return stamp, year


def apk_job(detail_url: str, page_body: str) -> ApkJob:
    """Build one resolved job, including source-side integrity fields."""

    direct_url = download_url(detail_url, page_body)
    query = parse_qs(urlparse(direct_url).query)
    release_time, year = _release_time(page_body)
    if year is None:
        year_match = HISTORY_YEAR_RE.search(detail_url)
        year = year_match.group(1) if year_match else None
    version_code = first_group(APP_VCODE_RE.search(page_body))
    if version_code is None:
        detail_match = re.search(r"history_v(\d+)", detail_url)
        version_code = detail_match.group(1) if detail_match else None
    expected_md5 = _query_value(query, "md5")
    if expected_md5 and not re.fullmatch(r"[0-9a-fA-F]{32}", expected_md5):
        expected_md5 = None
    return ApkJob(
        app_id=app_id_from_url(detail_url),
        detail_url=detail_url,
        download_url=direct_url,
        package_name=first_group(APP_PNAME_RE.search(page_body)),
        version=clean_version(
            first_group(APP_VNAME_RE.search(page_body))
            or first_group(VERSION_TEXT_RE.search(page_body))
        ),
        version_code=version_code,
        release_time=release_time,
        year=year,
        app_name=first_group(TITLE_RE.search(page_body)),
        expected_size=_numeric_query_value(query, "size"),
        expected_md5=expected_md5.lower() if expected_md5 else None,
        expected_crc32=_query_value(query, "crc32"),
        min_sdk=_query_value(query, "minSDK"),
    )


def redact_url(url: str, show_secrets: bool = False) -> str:
    """Redact token-like query values while preserving reproducibility fields."""

    if show_secrets:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    redacted: list[tuple[str, str]] = []
    for key in sorted(query):
        for value in query[key]:
            replacement = "<redacted>" if key.casefold() in REDACTED_QUERY_KEYS else value
            redacted.append((key, replacement))
    return urlunparse(parsed._replace(query=urlencode(redacted)))


def download_url_sha256(url: str) -> str:
    """Create a stable identity without persisting the full source URL."""

    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def host_in_domains(host: str | None, domains: set[str]) -> bool:
    """Return whether a host is exactly or beneath an allowed base domain."""

    if not host:
        return False
    lowered = host.rstrip(".").lower()
    return any(lowered == domain or lowered.endswith(f".{domain}") for domain in domains)


def validate_download_url(url: str, domains: set[str] | None = None) -> str:
    """Reject unexpected schemes, credentials, and download hosts."""

    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise InputError("APK download URL must use HTTPS")
    if parsed.username or parsed.password:
        raise InputError("APK download URL must not contain user information")
    allowed = domains or DEFAULT_DOWNLOAD_DOMAINS
    if not host_in_domains(parsed.hostname, allowed):
        raise InputError(f"unapproved APK download host: {parsed.hostname or '?'}")
    return url


def target_path(out_dir: Path, job: ApkJob) -> Path:
    """Build a collision-resistant canonical APK path."""

    release = (job.release_time or job.year or "unknown_date").split("T", 1)[0]
    release = release.replace("-", "")
    file_name = "{}-{}-{}-{}.apk".format(
        safe_part(job.package_name, "unknown.package"),
        safe_part(job.version, "unknown_version"),
        safe_part(job.version_code, "unknown_vcode"),
        safe_part(release, "unknown_date"),
    )
    return out_dir / file_name


def validate_apk_archive(path: Path) -> None:
    """Validate ZIP structure and the mandatory Android manifest entry."""

    if not path.is_file():
        raise InputError(f"APK is missing: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as error:
        raise InputError(f"invalid APK ZIP: {error}") from error
    if "AndroidManifest.xml" not in names:
        raise InputError("invalid APK: AndroidManifest.xml is missing")


def parse_aapt2_badging(output: str) -> ApkInspection:
    """Parse the first package line from `aapt2 dump badging`."""

    match = AAPT_PACKAGE_RE.search(output)
    if match is None:
        raise InputError("aapt2 output contains no package metadata")
    package_name, version_code, version = match.groups()
    return ApkInspection(
        package_name=package_name or None,
        version=clean_version(version),
        version_code=version_code or None,
    )
