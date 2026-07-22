"""Modern async image batch downloader (Python 3.12+)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn


RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+)\s*$")
APP_VERSION = "1.12.6"
GITHUB_REPOSITORY = "scarlel96-design/LumaFetch"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASES_URL_PREFIX = f"https://github.com/{GITHUB_REPOSITORY}/releases/"
MAX_RELEASE_METADATA_BYTES = 256 * 1024
MAX_UPDATE_INSTALLER_BYTES = 150 * 1024 * 1024
FAVORITES_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LumaFetch" / "favorites.json"
MAX_FAVORITES = 1000
FAVORITES_PAGE_SIZE = 30
MAX_CHARACTER_CODES = 1000
MAX_CHARACTER_CODE_LENGTH = 120
MAX_CHARACTER_LIST_LENGTH = 131_072
def runtime_asset(name: str) -> Path:
    """Return a bundled PyInstaller asset or its source-tree equivalent."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name

KOREAN_TEMPLATE_ALIASES = (
    ("{캐릭터}", "{char}"), ("{상황}", "{situation}"), ("{의상}", "{outfit}"),
    ("캐릭터", "{char}"), ("상황", "{situation}"), ("의상", "{outfit}"),
)


def normalize_template_url(value: str) -> str:
    """Accept Korean URL tokens while retaining the compact internal syntax."""
    for source, target in KOREAN_TEMPLATE_ALIASES:
        value = value.replace(source, target)
    return value


MAX_IMAGE_BYTES = 30 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif"}
Image.MAX_IMAGE_PIXELS = 20_000_000
IMAGE_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
)
AUTO_REFERER_CANDIDATES = ("https://si-ran.com/",)


def make_request_headers(referer: str | None) -> dict[str, str]:
    """Build scoped, browser-compatible headers for image GET requests only."""
    headers = {
        "User-Agent": IMAGE_BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer:
        headers["Referer"] = referer
    return headers


class ImageRequestPolicy:
    """Resolve a hotlink Referer once per host, then reuse it for the batch."""

    def __init__(
        self,
        explicit_referer: str | None,
        on_detected: Callable[[str, str], None] | None = None,
    ) -> None:
        self.explicit_referer = explicit_referer
        self.on_detected = on_detected
        self._resolved: dict[str, str | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _referer_headers(referer: str | None) -> dict[str, str] | None:
        return {"Referer": referer} if referer else None

    async def get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        **kwargs: object,
    ) -> aiohttp.ClientResponse:
        if self.explicit_referer:
            return await session.get(
                url,
                headers=self._referer_headers(self.explicit_referer),
                **kwargs,
            )

        host = (urlparse(url).hostname or "").casefold()
        if host in self._resolved:
            return await session.get(
                url,
                headers=self._referer_headers(self._resolved[host]),
                **kwargs,
            )

        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host in self._resolved:
                return await session.get(
                    url,
                    headers=self._referer_headers(self._resolved[host]),
                    **kwargs,
                )

            candidates: tuple[str | None, ...] = (None, *AUTO_REFERER_CANDIDATES)
            saw_forbidden = False
            for index, referer in enumerate(candidates):
                response = await session.get(
                    url,
                    headers=self._referer_headers(referer),
                    **kwargs,
                )
                if response.status == 403 and index < len(candidates) - 1:
                    saw_forbidden = True
                    response.release()
                    continue

                self._resolved[host] = referer
                if saw_forbidden and referer and response.status != 403 and self.on_detected:
                    self.on_detected(host, referer)
                return response

        raise RuntimeError("이미지 요청 정책이 응답을 선택하지 못했습니다.")


class SecurityGuard:
    """Small defensive layer: public HTTPS only, raster images only, bounded size."""

    @staticmethod
    def validate_url(value: str, *, template: bool = False) -> str:
        parsed = urlparse(value.replace("{char}", "x").replace("{pose}", "x")
                          .replace("{situation}", "x").replace("{outfit}", "x"))
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("보안을 위해 https:// 공개 URL만 사용할 수 있습니다.")
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            raise ValueError("URL의 인증 정보와 비표준 포트는 허용되지 않습니다.")
        if template and Path(parsed.path).suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError("이미지 확장자는 webp, png, jpg, jpeg, gif, avif만 허용됩니다.")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return value
        if not address.is_global:
            raise ValueError("로컬·사설 네트워크 주소는 사용할 수 없습니다.")
        return value

    @staticmethod
    def defender_executable() -> Path | None:
        candidates = [
            Path(os.environ.get("ProgramFiles", "")) / "Windows Defender" / "MpCmdRun.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Windows Defender" / "MpCmdRun.exe",
        ]
        platform_root = Path(os.environ.get("ProgramData", "")) / "Microsoft" / "Windows Defender" / "Platform"
        if platform_root.is_dir():
            candidates.extend(sorted(platform_root.glob("*/MpCmdRun.exe"), reverse=True))
        return next((candidate for candidate in candidates if candidate.is_file()), None)
    @staticmethod
    def has_safe_image_signature(header: bytes) -> bool:
        return (
            header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
            or header.startswith((b"GIF87a", b"GIF89a"))
            or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
            or (header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"})
        )


class PublicResolver(AbstractResolver):
    """Reject loopback, private, link-local and otherwise non-public DNS results."""

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = 0) -> list[dict[str, object]]:
        records = await self._resolver.resolve(host, port, family)
        for record in records:
            address = ipaddress.ip_address(str(record["host"]))
            if not address.is_global:
                raise OSError("보안 정책: 공개 인터넷 주소가 아닌 대상은 차단됩니다.")
        return records

    async def close(self) -> None:
        await self._resolver.close()
class DownloadConfig(BaseModel):
    """Validated settings collected from the GUI.

    {pose} is replaced with ``{situation}{outfit}``.  Templates may also use
    {situation} and {outfit} independently for sites with another naming rule.
    """

    template_url: str
    character: str = Field(min_length=1, max_length=MAX_CHARACTER_LIST_LENGTH)
    ranges: str = Field(min_length=1)
    outfit: str = "X"
    destination: Path
    separate_character_folders: bool = False
    scan_with_defender: bool = False
    referer: str | None = None
    concurrency: int = Field(default=20, ge=1, le=50)
    retries: int = Field(default=3, ge=1, le=5)

    @field_validator("template_url")
    @classmethod
    def valid_template_url(cls, value: str) -> str:
        return SecurityGuard.validate_url(normalize_template_url(value.strip()), template=True)

    @field_validator("referer")
    @classmethod
    def valid_referer(cls, value: str | None) -> str | None:
        if not value or not (referer := value.strip()):
            return None
        if "://" not in referer:
            referer = f"https://{referer}"
        return SecurityGuard.validate_url(referer)

    @field_validator("character")
    @classmethod
    def normalize_characters(cls, value: str) -> str:
        codes = [code.strip() for code in value.split(",") if code.strip()]
        if not codes:
            raise ValueError("캐릭터 코드를 하나 이상 입력하세요.")
        if len(codes) > MAX_CHARACTER_CODES:
            raise ValueError(f"캐릭터 코드는 최대 {MAX_CHARACTER_CODES:,}개까지 입력할 수 있습니다.")
        if any(len(code) > MAX_CHARACTER_CODE_LENGTH for code in codes):
            raise ValueError(f"각 캐릭터 코드는 최대 {MAX_CHARACTER_CODE_LENGTH}자까지 입력할 수 있습니다.")
        if any(code in {".", ".."} for code in codes):
            raise ValueError("캐릭터 코드로 . 또는 ..은 사용할 수 없습니다.")
        if any(any(char in code for char in r'\\/:*?"<>|') for code in codes):
            raise ValueError("캐릭터 코드에는 파일명에 사용할 수 없는 문자를 쓸 수 없습니다.")
        return ",".join(dict.fromkeys(codes))

    @field_validator("outfit")
    @classmethod
    def normalize_outfit(cls, value: str) -> str:
        value = value.strip() or "X"
        if any(char in value for char in r'\\/:*?"<>|'):
            raise ValueError("의상 코드에는 파일명에 사용할 수 없는 문자를 쓸 수 없습니다.")
        return value

    @model_validator(mode="after")
    def validate_template_and_ranges(self) -> DownloadConfig:
        known = ("{char}", "{pose}", "{situation}", "{outfit}")
        if not any(marker in self.template_url for marker in known):
            raise ValueError("템플릿에는 {char}, {pose}, {situation}, {outfit} 중 하나가 필요합니다.")
        self.expand_situations()  # validates every range
        return self

    def expand_characters(self) -> list[str]:
        return self.character.split(",")
    def expand_situations(self) -> list[str]:
        values: dict[str, None] = {}
        for part in self.ranges.split(","):
            match = RANGE_PATTERN.fullmatch(part)
            if not match:
                raise ValueError("범위는 0001..0500, 1001..1420처럼 쉼표로 구분하세요.")
            start_text, end_text = match.groups()
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"잘못된 범위: {part.strip()}")
            width = max(len(start_text), len(end_text))
            if end - start > 100_000:
                raise ValueError("한 범위는 최대 100,001개까지 가능합니다.")
            for number in range(start, end + 1):
                values.setdefault(f"{number:0{width}d}", None)
        return list(values)


class FavoritePreset(BaseModel):
    """A named, validated snapshot of the download form."""

    name: str = Field(min_length=1, max_length=40)
    template_url: str
    character: str
    ranges: str
    outfit: str = "X"
    referer: str | None = None
    concurrency: int = Field(default=20, ge=1, le=50)
    separate_character_folders: bool = False
    scan_with_defender: bool = False
    fixed_destination: bool = False
    destination: str | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not (name := value.strip()):
            raise ValueError("즐겨찾기 이름을 입력하세요.")
        return name

    @model_validator(mode="after")
    def validate_snapshot(self) -> FavoritePreset:
        DownloadConfig(
            template_url=self.template_url,
            character=self.character,
            ranges=self.ranges,
            outfit=self.outfit,
            referer=self.referer,
            concurrency=self.concurrency,
            destination=Path(self.destination or Path.cwd()),
            separate_character_folders=self.separate_character_folders,
            scan_with_defender=self.scan_with_defender,
        )
        if self.fixed_destination and not self.destination:
            raise ValueError("고정 저장 위치가 비어 있습니다.")
        return self


def load_favorites(path: Path = FAVORITES_FILE) -> list[FavoritePreset]:
    """Load a bounded local preset file; malformed data never blocks startup."""
    try:
        if not path.is_file() or path.stat().st_size > 512 * 1024:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [FavoritePreset.model_validate(item) for item in payload[:MAX_FAVORITES]]
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return []


def save_favorites(favorites: list[FavoritePreset], path: Path = FAVORITES_FILE) -> None:
    """Atomically save validated presets in the current user's app-data folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    payload = [favorite.model_dump(mode="json") for favorite in favorites[:MAX_FAVORITES]]
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)

@dataclass(slots=True)
class DownloadStats:
    total: int
    success: int = 0
    failed: int = 0
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    tag_name: str
    version: tuple[int, int, int]
    title: str
    notes: str
    release_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    asset_sha256: str


def parse_release_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(map(int, match.groups())) if match else None


def is_trusted_release_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(f"/{GITHUB_REPOSITORY}/releases/")
    )


def is_trusted_asset_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(f"/{GITHUB_REPOSITORY}/releases/download/")
        and parsed.path.lower().endswith(".exe")
    )


def is_trusted_asset_response(value: str) -> bool:
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        hostname == "github.com" or hostname == "release-assets.githubusercontent.com"
    )


def build_update_installer_command(installer: Path, *, restart: bool) -> list[str]:
    """Build a verified, non-interactive Inno Setup update handoff."""
    if sys.platform != "win32" or not installer.is_file() or installer.suffix.lower() != ".exe":
        raise RuntimeError("검증된 Windows 설치 파일을 찾을 수 없습니다.")
    command = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/FORCECLOSEAPPLICATIONS",
        "/LUMAFETCHUPDATE",
    ]
    if restart:
        command.append("/AUTORESTARTAPP")
    return command


async def fetch_latest_release() -> UpdateInfo:
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=300, resolver=PublicResolver())
    timeout = aiohttp.ClientTimeout(total=15, connect=7, sock_read=10)
    headers = {
        "User-Agent": f"LumaFetch/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        async with session.get(LATEST_RELEASE_API, allow_redirects=False) as response:
            if response.status == 404:
                raise RuntimeError("아직 공개된 GitHub 릴리스가 없습니다.")
            if response.status == 403:
                raise RuntimeError("GitHub 요청 제한에 도달했습니다. 잠시 후 다시 확인하세요.")
            if response.status != 200:
                raise RuntimeError(f"GitHub 업데이트 확인 실패 (HTTP {response.status})")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(32 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_RELEASE_METADATA_BYTES:
                    raise RuntimeError("릴리스 정보가 허용 크기를 초과했습니다.")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("GitHub 릴리스 응답을 해석할 수 없습니다.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub 릴리스 응답 형식이 올바르지 않습니다.")
    tag_name = str(payload.get("tag_name") or "")
    version = parse_release_version(tag_name)
    release_url = str(payload.get("html_url") or "")
    if version is None or not is_trusted_release_url(release_url):
        raise RuntimeError("GitHub 릴리스의 버전 또는 주소가 올바르지 않습니다.")
    expected_asset_name = f"LumaFetch-Setup-{'.'.join(map(str, version))}.exe"
    assets = payload.get("assets")
    asset = next(
        (
            item for item in assets
            if isinstance(item, dict)
            and item.get("state") == "uploaded"
            and item.get("name") == expected_asset_name
        ),
        None,
    ) if isinstance(assets, list) else None
    if asset is None:
        raise RuntimeError(f"릴리스에서 {expected_asset_name} 설치 파일을 찾을 수 없습니다.")
    asset_url = str(asset.get("browser_download_url") or "")
    digest = str(asset.get("digest") or "")
    digest_match = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest)
    try:
        asset_size = int(asset.get("size") or 0)
    except (TypeError, ValueError) as error:
        raise RuntimeError("릴리스 설치 파일 크기가 올바르지 않습니다.") from error
    if (
        not is_trusted_asset_url(asset_url)
        or digest_match is None
        or not 0 < asset_size <= MAX_UPDATE_INSTALLER_BYTES
    ):
        raise RuntimeError("릴리스 설치 파일의 주소·크기·SHA-256 정보가 올바르지 않습니다.")
    return UpdateInfo(
        tag_name=tag_name,
        version=version,
        title=str(payload.get("name") or tag_name),
        notes=str(payload.get("body") or "").strip()[:1200],
        release_url=release_url,
        asset_name=expected_asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        asset_sha256=digest_match.group(1).lower(),
    )


async def download_update_installer(
    info: UpdateInfo,
    on_progress: Callable[[int, int], None],
    cancelled: threading.Event,
    destination_dir: Path | None = None,
) -> Path:
    if not is_trusted_asset_url(info.asset_url):
        raise RuntimeError("신뢰할 수 없는 업데이트 주소입니다.")
    update_dir = destination_dir or Path(tempfile.gettempdir()) / "LumaFetch" / "Updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    target = update_dir / info.asset_name
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)
    for old_installer in update_dir.glob("LumaFetch-Setup-*.exe"):
        if old_installer != target:
            old_installer.unlink(missing_ok=True)

    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=300, resolver=PublicResolver())
    timeout = aiohttp.ClientTimeout(total=600, connect=15, sock_read=60)
    headers = {
        "User-Agent": f"LumaFetch/{APP_VERSION}",
        "Accept": "application/octet-stream",
    }
    downloaded = 0
    reported = 0
    digest = hashlib.sha256()
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            async with session.get(info.asset_url, allow_redirects=True, max_redirects=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"업데이트 다운로드 실패 (HTTP {response.status})")
                if not is_trusted_asset_response(str(response.url)):
                    raise RuntimeError("GitHub 이외의 주소로 리디렉션되어 다운로드를 중단했습니다.")
                if response.content_length and response.content_length > MAX_UPDATE_INSTALLER_BYTES:
                    raise RuntimeError("업데이트 설치 파일이 허용 크기를 초과했습니다.")
                async with aiofiles.open(partial, "wb") as file:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        if cancelled.is_set():
                            raise RuntimeError("업데이트 다운로드가 취소되었습니다.")
                        downloaded += len(chunk)
                        if downloaded > MAX_UPDATE_INSTALLER_BYTES:
                            raise RuntimeError("업데이트 설치 파일이 허용 크기를 초과했습니다.")
                        digest.update(chunk)
                        await file.write(chunk)
                        if downloaded - reported >= 512 * 1024 or downloaded == info.asset_size:
                            reported = downloaded
                            on_progress(downloaded, info.asset_size)
        if downloaded != info.asset_size:
            raise RuntimeError(f"업데이트 파일 크기 불일치 ({downloaded}/{info.asset_size} bytes)")
        actual_digest = digest.hexdigest().lower()
        if not hmac.compare_digest(actual_digest, info.asset_sha256):
            raise RuntimeError("업데이트 SHA-256 검증에 실패했습니다.")
        partial.replace(target)
        on_progress(downloaded, info.asset_size)
        return target
    except Exception:
        partial.unlink(missing_ok=True)
        raise


@dataclass(slots=True)
class PreviewBatch:
    """Summary for one character's streamed preview session."""

    total: int
    requested: int
    success: int
    failed: int
    errors: list[str]


@dataclass(slots=True)
class PreviewImage:
    """Validated preview metadata backed by bounded memory or a temp cache."""

    character: str
    situation: str
    url: str
    data: bytes = b""
    thumbnail_ppm: bytes | None = None
    cache_path: Path | None = None
    thumbnail_path: Path | None = None


def encode_thumbnail_ppm(data: bytes, size: tuple[int, int]) -> bytes:
    """Decode and resize away from Tk's UI thread."""
    with Image.open(BytesIO(data)) as source:
        source.draft("RGB", size)
        image = ImageOps.contain(source.convert("RGB"), size, method=Image.Resampling.BILINEAR)
    buffer = BytesIO()
    image.save(buffer, format="PPM")
    return buffer.getvalue()


async def read_image_response(response: aiohttp.ClientResponse) -> bytes:
    """Read every HTTP chunk while enforcing the image-size ceiling."""
    data = bytearray()
    async for chunk in response.content.iter_chunked(256 * 1024):
        data.extend(chunk)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 30 MiB limit.")
    return bytes(data)

async def fetch_live_previews(
    config: DownloadConfig,
    character: str,
    on_item: Callable[[int, PreviewImage], None],
    cancelled: threading.Event,
    cache_dir: Path | None = None,
) -> PreviewBatch:
    """Stream previews through separate, backpressured network and decode pipelines."""
    situations = config.expand_situations()
    total = len(situations)
    probe = Downloader(config, threading.Event(), lambda _kind, _payload: None)
    network_workers = min(16, max(1, total))
    decode_workers = min(4, max(1, total))
    connector = aiohttp.TCPConnector(
        limit=network_workers,
        limit_per_host=network_workers,
        ttl_dns_cache=600,
        keepalive_timeout=30,
        enable_cleanup_closed=True,
        resolver=PublicResolver(),
    )
    timeout = aiohttp.ClientTimeout(total=25, connect=10, sock_read=18)
    headers = make_request_headers(None)
    request_policy = ImageRequestPolicy(config.referer)
    next_index = 0
    index_lock = asyncio.Lock()
    success = 0
    failed = 0
    errors: list[str] = []
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    def reason(error: Exception) -> str:
        if isinstance(error, aiohttp.ClientResponseError):
            match error.status:
                case 401: return "HTTP 401: authentication is required."
                case 403: return "HTTP 403: the server rejected this request (authorized Referer, login, or cookie may be required)."
                case 404: return "HTTP 404: the image URL was not found."
                case status: return f"HTTP {status}: the server rejected this request."
        if isinstance(error, asyncio.TimeoutError):
            return "Timeout: the image server did not respond in time."
        return str(error)

    async def fetch_one(session: aiohttp.ClientSession, situation: str) -> tuple[PreviewImage | None, str | None]:
        last_error: Exception | None = None
        for candidate in probe.situation_candidates(situation):
            if cancelled.is_set():
                return None, None
            url = probe.make_url(character, candidate)
            try:
                response = await request_policy.get(session, url, allow_redirects=False)
                async with response:
                    if response.status == 404:
                        last_error = aiohttp.ClientResponseError(response.request_info, response.history, status=404)
                        continue
                    if response.status != 200:
                        raise aiohttp.ClientResponseError(response.request_info, response.history, status=response.status)
                    if not response.content_type.startswith("image/"):
                        raise ValueError("Response MIME type is not an image.")
                    if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                        raise ValueError("Image exceeds the 30 MiB limit.")
                    data = await read_image_response(response)
                    if not SecurityGuard.has_safe_image_signature(data[:32]):
                        raise ValueError("Image signature is not allowed.")
                    return PreviewImage(character, situation, url, data=data), None
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
                last_error = error
        return None, reason(last_error or RuntimeError("Unknown preview failure."))

    pipeline: asyncio.Queue[tuple[int, PreviewImage] | None] = asyncio.Queue(
        maxsize=network_workers * 2
    )

    async def network_worker(session: aiohttp.ClientSession) -> None:
        nonlocal next_index, failed
        while not cancelled.is_set():
            async with index_lock:
                if next_index >= total:
                    return
                index = next_index
                next_index += 1
            item, error = await fetch_one(session, situations[index])
            if cancelled.is_set():
                return
            if item is not None:
                await pipeline.put((index, item))
            else:
                failed += 1
                if error and error not in errors and len(errors) < 3:
                    errors.append(error)

    async def write_cache(path: Path, data: bytes) -> None:
        async with aiofiles.open(path, "wb") as file:
            await file.write(data)

    async def decode_worker() -> None:
        nonlocal success, failed
        while (work := await pipeline.get()) is not None:
            index, item = work
            try:
                if cancelled.is_set():
                    continue
                thumbnail = await asyncio.to_thread(encode_thumbnail_ppm, item.data, (178, 178))
                if cache_dir:
                    original_path = cache_dir / f"{index:08d}.image"
                    thumbnail_path = cache_dir / f"{index:08d}.ppm"
                    await asyncio.gather(
                        write_cache(original_path, item.data),
                        write_cache(thumbnail_path, thumbnail),
                    )
                    ready = PreviewImage(
                        item.character,
                        item.situation,
                        item.url,
                        cache_path=original_path,
                        thumbnail_path=thumbnail_path,
                    )
                else:
                    ready = PreviewImage(
                        item.character,
                        item.situation,
                        item.url,
                        data=item.data,
                        thumbnail_ppm=thumbnail,
                    )
                success += 1
                on_item(index, ready)
            except (OSError, ValueError, UnidentifiedImageError) as error:
                failed += 1
                message = reason(error)
                if message not in errors and len(errors) < 3:
                    errors.append(message)
            finally:
                pipeline.task_done()

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        decoders = [asyncio.create_task(decode_worker()) for _ in range(decode_workers)]
        await asyncio.gather(*(network_worker(session) for _ in range(network_workers)))
        for _ in decoders:
            await pipeline.put(None)
        await asyncio.gather(*decoders)
    requested = success + failed
    return PreviewBatch(total=total, requested=requested, success=success, failed=failed, errors=errors)

async def fetch_preview_covers(
    config: DownloadConfig,
    on_item: Callable[[str, PreviewImage], None],
    cancelled: threading.Event,
) -> None:
    """Find the first existing image for each character selector card."""
    situations = config.expand_situations()
    probe = Downloader(config, threading.Event(), lambda _kind, _payload: None)
    semaphore = asyncio.Semaphore(4)
    connector = aiohttp.TCPConnector(limit=4, limit_per_host=4, ttl_dns_cache=300, resolver=PublicResolver())
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = make_request_headers(None)
    request_policy = ImageRequestPolicy(config.referer)

    async def one(session: aiohttp.ClientSession, character: str) -> None:
        async with semaphore:
            for situation in situations:
                for candidate in probe.situation_candidates(situation):
                    if cancelled.is_set():
                        return
                    url = probe.make_url(character, candidate)
                    try:
                        response = await request_policy.get(session, url, allow_redirects=False)
                        async with response:
                            if response.status != 200 or not response.content_type.startswith("image/"):
                                continue
                            data = await read_image_response(response)
                            if not SecurityGuard.has_safe_image_signature(data[:32]):
                                continue
                            thumbnail = await asyncio.to_thread(encode_thumbnail_ppm, data, (132, 118))
                            on_item(character, PreviewImage(character, situation, url, thumbnail_ppm=thumbnail))
                            return
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                        continue

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        await asyncio.gather(*(one(session, character) for character in config.expand_characters()))

class Downloader:
    def __init__(self, config: DownloadConfig, cancelled: threading.Event, notify: Callable[[str, object], None]):
        self.config = config
        self.cancelled = cancelled
        self.notify = notify
        self.request_policy = ImageRequestPolicy(
            config.referer,
            lambda host, referer: self.notify(
                "log",
                f"403 자동 대응 · {host} 요청에 Referer {referer} 적용",
            ),
        )
        self.stats = DownloadStats(total=len(config.expand_characters()) * len(config.expand_situations()))
        self.defender = SecurityGuard.defender_executable() if config.scan_with_defender else None

    def scan_with_defender(self, path: Path) -> bool:
        if not self.defender:
            raise RuntimeError("Microsoft Defender 검사 도구를 찾을 수 없습니다.")
        result = subprocess.run(
            [str(self.defender), "-Scan", "-ScanType", "3", "-File", str(path)],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return False
        return "skipped" not in (result.stdout + result.stderr).lower()
    def make_url(self, character: str, situation: str) -> str:
        pose = f"{situation}{self.config.outfit}"
        return self.config.template_url.format(
            char=character, pose=pose, situation=situation, outfit=self.config.outfit
        )

    @staticmethod
    def situation_candidates(situation: str) -> list[str]:
        """Try the entered form first, then its unpadded numeric equivalent."""
        unpadded = str(int(situation))
        return [situation] if unpadded == situation else [situation, unpadded]

    async def download_one(self, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, character: str, situation: str) -> Literal["success", "failed", "cancelled"]:
        if self.cancelled.is_set():
            return "cancelled"
        extension = Path(urlparse(self.make_url(character, situation)).path).suffix or ".webp"
        output_dir = self.config.destination / character if self.config.separate_character_folders else self.config.destination
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{character}_{situation}{self.config.outfit}{extension}"
        temp = output.with_name(f".{output.stem}.part{output.suffix}")
        last_error: Exception | None = None
        async with semaphore:
            for candidate in self.situation_candidates(situation):
                url = self.make_url(character, candidate)
                for attempt in range(1, self.config.retries + 1):
                    if self.cancelled.is_set():
                        return "cancelled"
                    try:
                        response = await self.request_policy.get(session, url, allow_redirects=False)
                        async with response:
                            if response.status == 404:
                                last_error = aiohttp.ClientResponseError(
                                    response.request_info, response.history, status=response.status
                                )
                                break
                            if response.status != 200:
                                raise aiohttp.ClientResponseError(
                                    response.request_info, response.history, status=response.status
                                )
                            if not response.content_type.startswith("image/"):
                                raise ValueError("이미지 MIME 형식이 아닙니다.")
                            if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                                raise ValueError("이미지 크기 제한(30 MiB)을 초과했습니다.")
                            header = bytearray()
                            downloaded = 0
                            async with aiofiles.open(temp, "wb") as file:
                                async for chunk in response.content.iter_chunked(256 * 1024):
                                    if self.cancelled.is_set():
                                        return "cancelled"
                                    downloaded += len(chunk)
                                    if downloaded > MAX_IMAGE_BYTES:
                                        raise ValueError("이미지 크기 제한(30 MiB)을 초과했습니다.")
                                    if len(header) < 32:
                                        header.extend(chunk[:32 - len(header)])
                                    await file.write(chunk)
                            if not SecurityGuard.has_safe_image_signature(bytes(header)):
                                raise ValueError("허용된 래스터 이미지 서명이 아닙니다.")
                            if self.config.scan_with_defender:
                                scanned = await asyncio.to_thread(self.scan_with_defender, temp)
                                if not scanned and not getattr(self, "defender_skip_notified", False):
                                    self.notify("log", "Defender 검사가 건너뛰어졌거나 완료되지 않았습니다. 이미지 서명 검증은 계속 적용됩니다.")
                                    self.defender_skip_notified = True
                        temp.replace(output)
                        return "success"
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
                        last_error = error
                        temp.unlink(missing_ok=True)
                        if attempt < self.config.retries:
                            await asyncio.sleep(0.35 * attempt)
        self.notify("log", f"실패 [{character}/{situation}]: {last_error}")
        return "failed"
    async def run(self) -> DownloadStats:
        self.config.destination.mkdir(parents=True, exist_ok=True)
        if self.config.scan_with_defender and not self.defender:
            raise RuntimeError("Microsoft Defender 검사 도구를 찾을 수 없습니다.")
        connector = aiohttp.TCPConnector(
            limit=self.config.concurrency, limit_per_host=self.config.concurrency,
            ttl_dns_cache=300, enable_cleanup_closed=True, resolver=PublicResolver(),
        )
        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
        headers = make_request_headers(None)
        progress = Progress(SpinnerColumn(), TextColumn("다운로드"), BarColumn(), "{task.completed}/{task.total}", TimeRemainingColumn())
        worker_count = self.config.concurrency
        work_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=worker_count * 2)

        async def produce() -> None:
            for character in self.config.expand_characters():
                for situation in self.config.expand_situations():
                    if self.cancelled.is_set():
                        break
                    await work_queue.put((character, situation))
                if self.cancelled.is_set():
                    break
            for _ in range(worker_count):
                await work_queue.put(None)

        async def consume(
            session: aiohttp.ClientSession,
            semaphore: asyncio.Semaphore,
            task_id: object,
            progress_view: Progress,
        ) -> None:
            while (work := await work_queue.get()) is not None:
                character, situation = work
                result = await self.download_one(session, semaphore, character, situation)
                match result:
                    case "success": self.stats.success += 1
                    case "failed": self.stats.failed += 1
                    case _: self.stats.cancelled += 1
                progress_view.advance(task_id)
                self.notify("progress", self.stats)

        with progress:
            task_id = progress.add_task("images", total=self.stats.total)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                semaphore = asyncio.Semaphore(worker_count)
                async with asyncio.TaskGroup() as group:
                    group.create_task(produce())
                    for _ in range(worker_count):
                        group.create_task(consume(session, semaphore, task_id, progress))
            completed = self.stats.success + self.stats.failed + self.stats.cancelled
            if self.cancelled.is_set() and completed < self.stats.total:
                remaining = self.stats.total - completed
                self.stats.cancelled += remaining
                progress.advance(task_id, remaining)
                self.notify("progress", self.stats)
        return self.stats

class VirtualPreviewGrid(ctk.CTkFrame):
    """Canvas-native virtual grid: only visible thumbnails exist in Tk."""

    CELL_WIDTH = 204
    CELL_HEIGHT = 226
    OVERSCAN_ROWS = 1
    PHOTO_CACHE_LIMIT = 48
    RENDER_BATCH_SIZE = 4

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        *,
        colors: dict[str, str],
        font_factory: Callable[[int, str], ctk.CTkFont],
        photo_factory: Callable[[PreviewImage], tk.PhotoImage],
        on_open: Callable[[int], None],
    ) -> None:
        super().__init__(parent, fg_color="transparent")
        self.colors = colors
        self.font_factory = font_factory
        self.label_font = font_factory(10, "bold")
        self.photo_factory = photo_factory
        self.on_open = on_open
        self.items: dict[int, PreviewImage] = {}
        self.ordered_indices: list[int] = []
        self.total_slots = 0
        self.compact = False
        self.columns = 1
        self.cell_width = self.CELL_WIDTH
        self.layout_width = 0
        self.visible: dict[int, tuple[int, tuple[int, ...], tk.PhotoImage]] = {}
        self.photo_cache: dict[int, tk.PhotoImage] = {}
        self.redraw_id: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            bg=colors["bg"],
            bd=0,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
            yscrollincrement=24,
        )
        self.scrollbar = ctk.CTkScrollbar(self, command=self._scroll_to)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-1>", self._on_click)

    def reset(self, total: int) -> None:
        self.items.clear()
        self.ordered_indices.clear()
        self.photo_cache.clear()
        self.total_slots = total
        self.compact = False
        self.canvas.yview_moveto(0)
        self._destroy_visible()
        self._schedule_redraw()

    def add_item(self, index: int, item: PreviewImage) -> None:
        self.items[index] = item
        self._schedule_redraw()

    def finish(self) -> None:
        self.compact = True
        self.ordered_indices = sorted(self.items)
        self._destroy_visible()
        self._schedule_redraw()

    def clear(self) -> None:
        self.items.clear()
        self.ordered_indices.clear()
        self.photo_cache.clear()
        self.total_slots = 0
        self.compact = False
        self._destroy_visible()
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def _item_for_position(self, position: int) -> tuple[int, PreviewImage] | None:
        index = self.ordered_indices[position] if self.compact else position
        item = self.items.get(index)
        return (index, item) if item is not None else None

    def _on_configure(self, _event: tk.Event[tk.Misc]) -> None:
        width = max(1, self.canvas.winfo_width())
        columns = max(2, width // self.CELL_WIDTH)
        if columns != self.columns or abs(width - self.layout_width) >= 8:
            self.columns = columns
            self.layout_width = width
            self._destroy_visible()
        self._schedule_redraw()

    def _scroll_to(self, *arguments: str) -> None:
        self.canvas.yview(*arguments)
        self._schedule_redraw()

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str:
        delta = event.delta or 0
        direction = -1 if delta > 0 else 1
        magnitude = max(1, abs(delta) // 120)
        self.canvas.yview_scroll(direction * magnitude * 2, "units")
        self._schedule_redraw()
        return "break"

    def _on_click(self, event: tk.Event[tk.Misc]) -> None:
        column = min(self.columns - 1, max(0, int(self.canvas.canvasx(event.x) // self.cell_width)))
        row = max(0, int(self.canvas.canvasy(event.y) // self.CELL_HEIGHT))
        position = row * self.columns + column
        if resolved := self._item_for_position(position):
            self.on_open(resolved[0])

    def _schedule_redraw(self) -> None:
        if self.redraw_id is None:
            self.redraw_id = self.after(12, self._redraw)

    def _destroy_visible(self) -> None:
        for _index, canvas_ids, _photo in self.visible.values():
            for canvas_id in canvas_ids:
                self.canvas.delete(canvas_id)
        self.visible.clear()

    def _cached_photo(self, index: int, item: PreviewImage) -> tk.PhotoImage:
        if photo := self.photo_cache.pop(index, None):
            self.photo_cache[index] = photo
            return photo
        photo = self.photo_factory(item)
        self.photo_cache[index] = photo
        visible_indices = {entry[0] for entry in self.visible.values()}
        while len(self.photo_cache) > self.PHOTO_CACHE_LIMIT:
            stale = next((key for key in self.photo_cache if key not in visible_indices), None)
            if stale is None:
                break
            self.photo_cache.pop(stale, None)
        return photo

    def _redraw(self) -> None:
        self.redraw_id = None
        slot_count = len(self.ordered_indices) if self.compact else self.total_slots
        rows = (slot_count + self.columns - 1) // self.columns
        total_height = max(1, rows * self.CELL_HEIGHT)
        width = max(1, self.canvas.winfo_width())
        self.cell_width = max(170, width // self.columns)
        self.canvas.configure(scrollregion=(0, 0, width, total_height))

        top = max(0, int(self.canvas.canvasy(0) // self.CELL_HEIGHT) - self.OVERSCAN_ROWS)
        bottom = min(
            rows,
            int(self.canvas.canvasy(self.canvas.winfo_height()) // self.CELL_HEIGHT)
            + self.OVERSCAN_ROWS + 2,
        )
        wanted = set(range(top * self.columns, min(slot_count, bottom * self.columns)))
        for position in list(self.visible):
            if position not in wanted or self._item_for_position(position) is None:
                _index, canvas_ids, _photo = self.visible.pop(position)
                for canvas_id in canvas_ids:
                    self.canvas.delete(canvas_id)

        rendered = 0
        for position in sorted(wanted):
            if position in self.visible:
                continue
            resolved = self._item_for_position(position)
            if resolved is None:
                continue
            index, item = resolved
            try:
                photo = self._cached_photo(index, item)
            except Exception:
                continue
            row, column = divmod(position, self.columns)
            left = column * self.cell_width + 6
            top_y = row * self.CELL_HEIGHT + 6
            right = left + self.cell_width - 12
            bottom_y = top_y + self.CELL_HEIGHT - 12
            center_x = (left + right) // 2
            ids = (
                self.canvas.create_rectangle(
                    left, top_y, right, bottom_y,
                    fill=self.colors["surface"], outline="#26314E", width=1,
                ),
                self.canvas.create_image(center_x, top_y + 8, image=photo, anchor="n"),
                self.canvas.create_text(
                    center_x, bottom_y - 18,
                    text=f"{item.character} / {item.situation}  ·  원본 보기",
                    fill=self.colors["text"],
                    font=self.label_font,
                    anchor="center",
                ),
            )
            self.visible[position] = (index, ids, photo)
            rendered += 1
            if rendered >= self.RENDER_BATCH_SIZE:
                self._schedule_redraw()
                break

class DownloaderApp(ctk.CTk):
    """Desktop-first UI: macOS softness, One UI spacing, Windows 11 clarity."""

    COLORS = {
        "bg": "#0A1020", "sidebar": "#10182D", "surface": "#151F36",
        "input": "#0D1528", "accent": "#7484FF", "accent_hover": "#6172F5",
        "muted": "#93A0BE", "text": "#F6F8FF", "success": "#46DDA2", "danger": "#FF7185",
    }

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Luma Fetch — Image Batch Downloader")
        if (icon_path := runtime_asset("LumaFetch.ico")).is_file():
            self.iconbitmap(default=str(icon_path))
        self.geometry("1080x700")
        self.minsize(900, 620)
        self.configure(fg_color=self.COLORS["bg"])
        self.events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
        self.cancel_event = threading.Event()
        self.running = False
        self.entries: dict[str, ctk.CTkEntry] = {}
        self.entry_vars: dict[str, ctk.StringVar] = {}
        self.preview_after_id: str | None = None
        self.preview_updates_suspended = False
        self.validation_error_active = False
        self.preview_sequence = 0
        self.preview_photo: tk.PhotoImage | None = None
        self.preview_cancel_event = threading.Event()
        self.preview_config: DownloadConfig | None = None
        self.preview_cache_root = Path(tempfile.mkdtemp(prefix="LumaFetch-preview-"))
        self.gallery_items: dict[int, PreviewImage] = {}
        self.viewer_sequence = 0
        self.viewer_photo: tk.PhotoImage | None = None
        self.update_checking = False
        self.favorites = load_favorites()
        self._build()
        if "--update-complete" in sys.argv:
            self.update_status.configure(text=f"v{APP_VERSION} 업데이트 완료", text_color=self.COLORS["success"])
            self.after(250, lambda: self._write_log(f"업데이트 완료 — Luma Fetch v{APP_VERSION}"))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(12 if not self.events.empty() else 30, self._poll_events)

    def _font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family="Segoe UI Variable", size=size, weight=weight)

    def _card(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, corner_radius=24, fg_color=self.COLORS["surface"], border_width=1, border_color="#26314E")

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=176, corner_radius=0, fg_color=self.COLORS["sidebar"])
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        ctk.CTkLabel(sidebar, text="LUMA", font=self._font(24, "bold"), text_color=self.COLORS["text"]).pack(anchor="w", padx=22, pady=(26, 0))
        ctk.CTkLabel(sidebar, text="FETCH", font=self._font(10, "bold"), text_color=self.COLORS["accent"]).pack(anchor="w", padx=24, pady=(0, 22))
        nav = ctk.CTkFrame(sidebar, corner_radius=14, fg_color="#17213A")
        nav.pack(fill="x", padx=14)
        self.download_nav_button = ctk.CTkButton(
            nav, text="↓  일괄 다운로드", height=38, anchor="w", corner_radius=10,
            fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"],
            font=self._font(11, "bold"), command=lambda: self._show_main_view("download"),
        )
        self.download_nav_button.pack(fill="x", padx=5, pady=(5, 2))
        self.favorites_nav_button = ctk.CTkButton(
            nav, text="★  즐겨찾기", height=38, anchor="w", corner_radius=10,
            fg_color="transparent", hover_color="#273450",
            font=self._font(11, "bold"), command=lambda: self._show_main_view("favorites"),
        )
        self.favorites_nav_button.pack(fill="x", padx=5, pady=(2, 5))
        ctk.CTkLabel(sidebar, text="ASYNC · RETRY · FAST", font=self._font(9, "bold"), text_color=self.COLORS["muted"]).pack(anchor="w", padx=22, pady=(28, 8))
        ctk.CTkLabel(sidebar, text="저장한 설정은 즐겨찾기 화면에서\n검색하고 바로 실행할 수 있습니다.", justify="left", font=self._font(10), text_color=self.COLORS["muted"]).pack(anchor="w", padx=22)
        self.version_label = ctk.CTkLabel(sidebar, text=f"v{APP_VERSION}", font=self._font(10), text_color="#5F6E92")
        self.version_label.pack(side="bottom", anchor="w", padx=22, pady=(8, 18))
        self.update_status = ctk.CTkLabel(sidebar, text="", font=self._font(9), text_color=self.COLORS["muted"], wraplength=140, justify="left")
        self.update_status.pack(side="bottom", anchor="w", padx=22, pady=(2, 0))
        self.update_button = ctk.CTkButton(
            sidebar, text="↻  업데이트 확인", width=142, height=32, corner_radius=11,
            fg_color="#273450", hover_color="#334364", font=self._font(10, "bold"),
            command=self._check_for_updates,
        )
        self.update_button.pack(side="bottom", padx=16, pady=(20, 2))

        self.download_view = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main = self.download_view
        main.grid(row=0, column=1, sticky="nsew", padx=22, pady=16)
        main.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="이미지 수집", font=self._font(25, "bold"), text_color=self.COLORS["text"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="템플릿 · 캐릭터 · 범위를 입력하세요.", font=self._font(12), text_color=self.COLORS["muted"]).grid(row=1, column=0, sticky="w")
        action_bar = ctk.CTkFrame(header, fg_color="transparent")
        action_bar.grid(row=0, column=1, rowspan=2, sticky="e")
        self.cancel_button = ctk.CTkButton(action_bar, text="취소", width=76, height=38, corner_radius=13, state="disabled", fg_color="#342133", hover_color="#49283C", text_color="#FFB3C0", command=self._cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.start_button = ctk.CTkButton(action_bar, text="↓  다운로드", width=130, height=38, corner_radius=13, fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"], font=self._font(11, "bold"), command=self._start)
        self.start_button.pack(side="right")
        self.preview_button = ctk.CTkButton(action_bar, text="▦  미리보기", width=108, height=38, corner_radius=13, fg_color="#273450", hover_color="#334364", font=self._font(11, "bold"), command=self._manual_preview)
        self.preview_button.pack(side="right", padx=(0, 8))
        self.favorite_save_button = ctk.CTkButton(action_bar, text="☆  즐겨찾기", width=98, height=38, corner_radius=13, fg_color="#273450", hover_color="#334364", font=self._font(11, "bold"), command=self._save_current_favorite)
        self.favorite_save_button.pack(side="right", padx=(0, 8))
        self.state_badge = ctk.CTkLabel(header, text="●  준비됨", corner_radius=14, fg_color="#16372D", text_color=self.COLORS["success"], font=self._font(10, "bold"), padx=10, pady=5)
        self.state_badge.grid(row=2, column=1, sticky="e", pady=(5, 0))

        form = self._card(main)
        form.grid(row=1, column=0, sticky="ew")
        form.grid_columnconfigure((0, 1), weight=1)
        self._entry(form, "템플릿 URL", "치환 토큰: 캐릭터 · 상황 · 의상", 0, 0, span=2)
        self._entry(form, "캐릭터 코드", "쉼표로 여러 코드", 1, 0)
        self._entry(form, "의상 코드", "공란 = X", 1, 1)
        self._entry(form, "상황 코드 범위", "시작..끝, 시작..끝", 2, 0)
        self._entry(form, "동시 다운로드", "공란 = 20", 2, 1)
        self._entry(form, "Referer", "필요 시 원본 페이지 주소", 3, 0, span=2)
        preview_row = ctk.CTkFrame(form, fg_color="transparent")
        preview_row.grid(row=4, column=0, columnspan=2, padx=18, pady=(4, 2), sticky="ew")
        preview_row.grid_columnconfigure(0, weight=1)
        self.preview = ctk.CTkLabel(
            preview_row,
            text="설정을 입력한 뒤 미리보기 버튼을 누르세요.",
            text_color="#AAB7D8",
            fg_color="transparent",
            corner_radius=10,
            font=self._font(10),
            justify="left",
            anchor="w",
            wraplength=680,
            padx=10,
            pady=5,
        )
        self.preview.grid(row=0, column=0, sticky="ew")
        input_help = (
            "캐릭터 코드는 쉼표(,)로 구분하고, 상황 범위는 1..10 형식으로 입력하세요.\n"
            "템플릿 URL에서 코드가 들어갈 위치는 캐릭터 · 의상 · 상황 키워드로 채우세요."
        )
        ctk.CTkLabel(
            form,
            text=input_help,
            text_color="#7180A5",
            font=self._font(9),
            justify="left",
            anchor="w",
        ).grid(row=5, column=0, columnspan=2, padx=18, pady=(0, 12), sticky="ew")

        storage = self._card(main)
        storage.grid(row=2, column=0, sticky="ew", pady=10)
        storage.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(storage, text="저장 위치", font=self._font(11, "bold")).grid(row=0, column=0, padx=(18, 10), pady=(12, 4), sticky="w")
        self.folder_var = ctk.StringVar(value="")
        ctk.CTkEntry(storage, textvariable=self.folder_var, placeholder_text="저장 폴더 선택", height=34, fg_color=self.COLORS["input"], border_color="#2A3655").grid(row=0, column=1, padx=(0, 8), pady=(10, 4), sticky="ew")
        ctk.CTkButton(storage, text="선택", width=64, height=34, corner_radius=11, fg_color="#273450", hover_color="#334364", command=self._choose_folder).grid(row=0, column=2, padx=(0, 8), pady=(10, 4))
        ctk.CTkButton(storage, text="열기", width=54, height=34, corner_radius=11, fg_color="transparent", border_width=1, border_color="#425274", command=self._open_folder).grid(row=0, column=3, padx=(0, 14), pady=(10, 4))
        options = ctk.CTkFrame(storage, fg_color="transparent")
        options.grid(row=1, column=1, columnspan=3, padx=(0, 14), pady=(0, 10), sticky="w")
        self.separate_folders_var = ctk.BooleanVar(value=False)
        self.defender_scan_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(options, text="캐릭터별 하위 폴더", variable=self.separate_folders_var, checkbox_width=17, checkbox_height=17, font=self._font(11), text_color="#C9D4F3").pack(side="left", padx=(0, 16))
        ctk.CTkCheckBox(options, text="Defender 검사 요청 (느림)", variable=self.defender_scan_var, checkbox_width=17, checkbox_height=17, font=self._font(11), text_color="#C9D4F3").pack(side="left")

        status = self._card(main)
        status.grid(row=3, column=0, sticky="ew")
        status.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(status, height=13, corner_radius=10, fg_color="#25304B", progress_color=self.COLORS["accent"])
        self.progress.set(0)
        self.progress.grid(row=0, column=0, columnspan=2, padx=18, pady=(14, 8), sticky="ew")
        self.status = ctk.CTkLabel(status, text="대기 중", text_color=self.COLORS["muted"], font=self._font(11))
        self.status.grid(row=1, column=0, padx=18, pady=(0, 12), sticky="w")
        self.stats_label = ctk.CTkLabel(status, text="성공 0   실패 0   취소 0", text_color=self.COLORS["muted"], font=self._font(11, "bold"))
        self.stats_label.grid(row=1, column=1, padx=18, pady=(0, 12), sticky="e")

        lower = ctk.CTkFrame(main, fg_color="transparent")
        lower.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        lower.grid_columnconfigure(0, weight=1)
        activity = self._card(lower)
        activity.configure(height=58)
        activity.grid(row=0, column=0, sticky="ew")
        activity.grid_propagate(False)
        activity.grid_columnconfigure(0, weight=1); activity.grid_rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(activity, corner_radius=14, fg_color=self.COLORS["input"], border_width=0, font=self._font(11), text_color="#B9C5E3")
        self.log.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        self._build_favorites_view()
        self._render_favorites()
        self._show_main_view("download")


    def _build_favorites_view(self) -> None:
        self.favorite_page = 0
        self.favorite_render_after_id: str | None = None
        self.favorites_view = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.favorites_view.grid(row=0, column=1, sticky="nsew", padx=22, pady=16)
        self.favorites_view.grid_columnconfigure(0, weight=1)
        self.favorites_view.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self.favorites_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="즐겨찾기", font=self._font(25, "bold"), text_color=self.COLORS["text"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="저장한 설정을 검색하고 미리보기 또는 다운로드를 바로 실행하세요.", font=self._font(12), text_color=self.COLORS["muted"]).grid(row=1, column=0, sticky="w")
        self.favorite_count_label = ctk.CTkLabel(header, text="", font=self._font(11, "bold"), text_color="#AAB7D8")
        self.favorite_count_label.grid(row=0, column=1, rowspan=2, sticky="e")

        toolbar = self._card(self.favorites_view)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        toolbar.grid_columnconfigure(0, weight=1)
        self.favorite_search_var = ctk.StringVar(value="")
        search = ctk.CTkEntry(toolbar, textvariable=self.favorite_search_var, placeholder_text="이름 · 주소 · 캐릭터 코드 검색", height=38, corner_radius=12, fg_color=self.COLORS["input"], border_color="#2A3655", font=self._font(11))
        search.grid(row=0, column=0, padx=(14, 8), pady=12, sticky="ew")
        search.bind("<KeyRelease>", self._schedule_favorite_render)
        ctk.CTkButton(toolbar, text="검색 지우기", width=82, height=34, corner_radius=11, fg_color="#273450", hover_color="#334364", command=lambda: self.favorite_search_var.set("")).grid(row=0, column=1, padx=(0, 14), pady=12)
        self.favorite_prev_button = ctk.CTkButton(toolbar, text="‹", width=38, height=34, corner_radius=11, fg_color="#273450", hover_color="#334364", command=lambda: self._change_favorite_page(-1))
        self.favorite_prev_button.grid(row=0, column=2, padx=(0, 5), pady=12)
        self.favorite_page_label = ctk.CTkLabel(toolbar, text="1 / 1", width=66, font=self._font(10, "bold"), text_color="#AAB7D8")
        self.favorite_page_label.grid(row=0, column=3, pady=12)
        self.favorite_next_button = ctk.CTkButton(toolbar, text="›", width=38, height=34, corner_radius=11, fg_color="#273450", hover_color="#334364", command=lambda: self._change_favorite_page(1))
        self.favorite_next_button.grid(row=0, column=4, padx=(5, 14), pady=12)
        self.favorite_search_var.trace_add("write", lambda *_args: self._schedule_favorite_render())

        self.favorite_grid = ctk.CTkScrollableFrame(self.favorites_view, corner_radius=18, fg_color="#0D1528")
        self.favorite_grid.grid(row=2, column=0, sticky="nsew")
        self.favorite_grid.grid_columnconfigure((0, 1), weight=1, uniform="favorite")
        self.favorites_view.grid_remove()

    def _show_main_view(self, view: str) -> None:
        is_favorites = view == "favorites"
        if is_favorites:
            self.download_view.grid_remove()
            self.favorites_view.grid()
            self._render_favorites()
        else:
            self.favorites_view.grid_remove()
            self.download_view.grid()
        self.download_nav_button.configure(fg_color="transparent" if is_favorites else self.COLORS["accent"])
        self.favorites_nav_button.configure(fg_color=self.COLORS["accent"] if is_favorites else "transparent")

    def _schedule_favorite_render(self, _event: object | None = None) -> None:
        self.favorite_page = 0
        if self.favorite_render_after_id:
            self.after_cancel(self.favorite_render_after_id)
        self.favorite_render_after_id = self.after(90, self._render_favorites)

    def _change_favorite_page(self, offset: int) -> None:
        self.favorite_page = max(0, self.favorite_page + offset)
        self._render_favorites()

    def _load_favorite_to_form(self, favorite: FavoritePreset) -> None:
        self._apply_favorite(favorite)
        self._show_main_view("download")
        self._clear_live_preview("즐겨찾기 설정을 불러왔습니다. 미리보기 또는 다운로드를 누르세요.")

    def _check_for_updates(self) -> None:
        if self.update_checking:
            return
        self.update_checking = True
        self.update_button.configure(state="disabled", text="확인 중…")
        self.update_status.configure(text="GitHub 릴리스 확인 중…", text_color=self.COLORS["muted"])
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        try:
            info = asyncio.run(fetch_latest_release())
            self.events.put(("update_result", info))
        except Exception as error:
            self.events.put(("update_error", str(error)))

    def _handle_update_result(self, info: UpdateInfo) -> None:
        self.update_checking = False
        self.update_button.configure(state="normal", text="↻  업데이트 확인")
        current = parse_release_version(APP_VERSION)
        if current is None:
            self.update_status.configure(text="현재 버전 확인 불가", text_color=self.COLORS["danger"])
            return
        if info.version > current:
            self.update_status.configure(text=f"새 버전 {info.tag_name}", text_color=self.COLORS["success"])
            self._show_update_dialog(info)
        elif info.version == current:
            self.update_status.configure(text="현재 최신 버전입니다.", text_color=self.COLORS["success"])
        else:
            self.update_status.configure(text=f"개발 버전 · 공개 {info.tag_name}", text_color=self.COLORS["muted"])

    def _handle_update_error(self, message: str) -> None:
        self.update_checking = False
        self.update_button.configure(state="normal", text="↻  업데이트 확인")
        self.update_status.configure(text="업데이트 확인 실패", text_color=self.COLORS["danger"])
        self._write_log(f"업데이트 확인 실패: {message}")

    def _show_update_dialog(self, info: UpdateInfo) -> None:
        if dialog := getattr(self, "update_dialog", None):
            if dialog.winfo_exists():
                dialog.destroy()
        self.update_dialog = ctk.CTkToplevel(self)
        self.update_dialog.title("Luma Fetch — 새 업데이트")
        self.update_dialog.geometry("540x540")
        self.update_dialog.resizable(False, False)
        self.update_dialog.configure(fg_color=self.COLORS["bg"])
        self.update_dialog.transient(self)
        self.update_dialog.grab_set()
        self.update_download_cancel = threading.Event()
        ctk.CTkLabel(
            self.update_dialog, text="새 버전을 사용할 수 있습니다",
            font=self._font(20, "bold"), text_color=self.COLORS["text"],
        ).pack(anchor="w", padx=24, pady=(24, 4))
        ctk.CTkLabel(
            self.update_dialog,
            text=f"현재 v{APP_VERSION}  →  최신 {info.tag_name}\n{info.title}",
            font=self._font(12), text_color=self.COLORS["muted"], justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 14))
        notes = ctk.CTkTextbox(
            self.update_dialog, height=150, corner_radius=14,
            fg_color=self.COLORS["input"], font=self._font(11), text_color="#C9D4F3",
        )
        notes.pack(fill="both", expand=True, padx=24, pady=(0, 12))
        notes.insert("1.0", info.notes or "자세한 변경 사항은 GitHub 릴리스 페이지에서 확인할 수 있습니다.")
        notes.configure(state="disabled")
        self.update_phase_label = ctk.CTkLabel(
            self.update_dialog,
            text="1 다운로드  →  2 보안 검증  →  3 자동 설치",
            font=self._font(10, "bold"), text_color="#AAB7D8",
        )
        self.update_phase_label.pack(anchor="w", padx=24, pady=(0, 8))
        self.update_relaunch_var = ctk.BooleanVar(value=True)
        self.update_relaunch_checkbox = ctk.CTkCheckBox(
            self.update_dialog,
            text="설치 완료 후 Luma Fetch 자동 재실행",
            variable=self.update_relaunch_var,
            checkbox_width=17, checkbox_height=17,
            font=self._font(10), text_color="#C9D4F3",
        )
        self.update_relaunch_checkbox.pack(anchor="w", padx=24, pady=(0, 10))
        self.update_download_progress = ctk.CTkProgressBar(
            self.update_dialog, height=10, corner_radius=8,
            fg_color="#25304B", progress_color=self.COLORS["accent"],
        )
        self.update_download_progress.set(0)
        self.update_download_progress.pack(fill="x", padx=24, pady=(0, 5))
        self.update_download_label = ctk.CTkLabel(
            self.update_dialog,
            text=f"설치 파일 {info.asset_size / (1024 * 1024):.1f} MiB · 검증 후 앱 안에서 자동 설치",
            font=self._font(10), text_color=self.COLORS["muted"],
        )
        self.update_download_label.pack(anchor="w", padx=24, pady=(0, 12))
        actions = ctk.CTkFrame(self.update_dialog, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(0, 22))
        self.update_cancel_button = ctk.CTkButton(
            actions, text="나중에", width=90, height=36, corner_radius=12,
            fg_color="#273450", hover_color="#334364", command=self._cancel_update_download,
        )
        self.update_cancel_button.pack(side="right")
        self.update_install_button = ctk.CTkButton(
            actions, text="지금 업데이트", width=145, height=36, corner_radius=12,
            fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"],
            command=lambda: self._download_and_install_update(info),
        )
        self.update_install_button.pack(side="right", padx=(0, 8))
        ctk.CTkButton(
            actions, text="릴리스 보기", width=100, height=36, corner_radius=12,
            fg_color="transparent", border_width=1, border_color="#425274",
            command=lambda: self._open_release_page(info.release_url),
        ).pack(side="left")
        self.update_dialog.protocol("WM_DELETE_WINDOW", self._cancel_update_download)

    def _download_and_install_update(self, info: UpdateInfo) -> None:
        self.update_download_cancel = threading.Event()
        self.update_install_button.configure(state="disabled", text="업데이트 진행 중…")
        self.update_cancel_button.configure(state="normal", text="취소")
        self.update_relaunch_checkbox.configure(state="disabled")
        self.update_phase_label.configure(
            text="● 다운로드 중   ○ 보안 검증   ○ 자동 설치", text_color="#AAB7D8"
        )
        self.update_download_label.configure(
            text="GitHub에서 설치 파일을 다운로드하는 중…", text_color=self.COLORS["muted"]
        )
        self.update_dialog.protocol("WM_DELETE_WINDOW", self._cancel_update_download)
        threading.Thread(target=self._download_update_worker, args=(info,), daemon=True).start()

    def _download_update_worker(self, info: UpdateInfo) -> None:
        try:
            def report(downloaded: int, total: int) -> None:
                self.events.put(("update_download_progress", (downloaded, total)))
            installer = asyncio.run(
                download_update_installer(info, report, self.update_download_cancel)
            )
            self.events.put(("update_download_done", installer))
        except Exception as error:
            self.events.put(("update_download_error", str(error)))

    def _cancel_update_download(self) -> None:
        if cancel := getattr(self, "update_download_cancel", None):
            cancel.set()
        if dialog := getattr(self, "update_dialog", None):
            if dialog.winfo_exists():
                dialog.destroy()

    def _handle_update_download_progress(self, downloaded: int, total: int) -> None:
        if not getattr(self, "update_dialog", None) or not self.update_dialog.winfo_exists():
            return
        ratio = min(1.0, downloaded / total) if total else 0.0
        self.update_download_progress.set(ratio)
        self.update_download_label.configure(
            text=f"다운로드 {downloaded / (1024 * 1024):.1f} / {total / (1024 * 1024):.1f} MiB"
        )

    def _handle_update_download_error(self, message: str) -> None:
        self.update_status.configure(text="업데이트 설치 실패", text_color=self.COLORS["danger"])
        self._write_log(f"업데이트 설치 실패: {message}")
        if getattr(self, "update_dialog", None) and self.update_dialog.winfo_exists():
            self.update_install_button.configure(state="normal", text="다시 시도")
            self.update_cancel_button.configure(state="normal", text="닫기")
            self.update_relaunch_checkbox.configure(state="normal")
            self.update_phase_label.configure(text="업데이트 중단 · 다시 시도할 수 있습니다.", text_color=self.COLORS["danger"])
            self.update_download_label.configure(text=message, text_color=self.COLORS["danger"])
            self.update_dialog.protocol("WM_DELETE_WINDOW", self._cancel_update_download)

    def _handle_update_download_done(self, installer: Path) -> None:
        restart = True
        if getattr(self, "update_dialog", None) and self.update_dialog.winfo_exists():
            restart = bool(self.update_relaunch_var.get())
            self.update_download_progress.set(1)
            self.update_phase_label.configure(
                text="✓ 다운로드 완료   ✓ SHA-256 검증   ● 자동 설치 준비",
                text_color=self.COLORS["success"],
            )
            self.update_download_label.configure(text="보안 검증 완료 · 앱을 종료하고 자동 설치합니다.")
            self.update_cancel_button.configure(state="disabled", text="설치 준비")
            self.update_dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        self.update_status.configure(text="업데이트 자동 설치 준비", text_color=self.COLORS["success"])
        self.after(500, lambda: self._launch_update_installer(installer, restart=restart))

    def _launch_update_installer(self, installer: Path, *, restart: bool) -> None:
        try:
            command = build_update_installer_command(installer, restart=restart)
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            subprocess.Popen(command, close_fds=True, creationflags=creationflags)
        except Exception as error:
            self._handle_update_download_error(str(error))
            return
        self.after(150, self._on_close)

    def _open_release_page(self, release_url: str) -> None:
        if not is_trusted_release_url(release_url):
            self._write_log("보안 검증에 실패해 릴리스 주소를 열지 않았습니다.")
            return
        webbrowser.open(release_url, new=2)

    def _entry(self, parent: ctk.CTkFrame, label: str, placeholder: str, row: int, column: int, *, span: int = 1, initial: str = "") -> None:
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.grid(row=row, column=column, columnspan=span, padx=18, pady=(9 if row == 0 else 3, 3), sticky="ew")
        holder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(holder, text=label, font=self._font(11, "bold"), text_color="#DDE5FF").grid(row=0, column=0, sticky="w", pady=(0, 3))
        variable = ctk.StringVar(value=initial)
        entry = ctk.CTkEntry(holder, textvariable=variable, placeholder_text=placeholder, height=34, corner_radius=11, fg_color=self.COLORS["input"], border_color="#2A3655", font=self._font(11))
        entry.grid(row=1, column=0, sticky="ew")
        entry.bind("<KeyRelease>", self._queue_preview_update)
        entry.bind("<FocusOut>", self._queue_preview_update)
        self.entries[label] = entry
        self.entry_vars[label] = variable
        variable.trace_add("write", lambda *_args: self._trace_preview_update())

    def _trace_preview_update(self) -> None:
        """Ignore programmatic favorite loads while preserving normal live validation."""
        if not self.preview_updates_suspended:
            self.validation_error_active = False
            self.preview.configure(fg_color="transparent", text_color="#AAB7D8")
            self._invalidate_preview_session()
            self._queue_preview_update()

    def _invalidate_preview_session(self) -> None:
        """Immediately disconnect every request associated with edited inputs."""
        self.preview_sequence += 1
        self.preview_cancel_event.set()

    def _cancel_preview_update(self) -> None:
        """Cancel the only tracked input invalidation before an explicit action."""
        if not self.preview_after_id:
            return
        after_id, self.preview_after_id = self.preview_after_id, None
        try:
            self.after_cancel(after_id)
        except tk.TclError:
            pass

    def _queue_preview_update(self, _event: object | None = None) -> None:
        """Coalesce rapid field changes without cancelling a favorite preview being opened."""
        self._cancel_preview_update()
        self.preview_after_id = self.after(45, self._update_preview)

    def _preview_config(self) -> DownloadConfig:
        """Validate preview inputs without requiring a download destination."""
        return DownloadConfig(
            template_url=self.entries["템플릿 URL"].get(),
            character=self.entries["캐릭터 코드"].get(),
            ranges=self.entries["상황 코드 범위"].get(),
            outfit=self.entries["의상 코드"].get(),
            referer=self.entries["Referer"].get().strip() or None,
            concurrency=1,
            destination=Path.cwd(),
        )

    def _clear_live_preview(self, message: str, *, error: bool = False) -> None:
        self.preview_photo = None
        self.preview.configure(
            text=message,
            fg_color="#3B2133" if error else "transparent",
            text_color="#FFADC0" if error else "#AAB7D8",
        )

    @staticmethod
    def _input_error_message(error: ValidationError | ValueError) -> str:
        if isinstance(error, ValidationError) and (issues := error.errors(include_url=False)):
            issue = issues[0]
            location = issue.get("loc") or ("입력",)
            field = str(location[0])
            label = {
                "template_url": "템플릿 URL", "character": "캐릭터 코드",
                "ranges": "상황 코드 범위", "outfit": "의상 코드",
                "referer": "Referer", "concurrency": "동시 다운로드",
                "destination": "저장 위치",
            }.get(field, "입력값")
            message = str(issue.get("msg", error)).removeprefix("Value error, ")
            return f"{label}: {message}"
        return str(error)

    def _show_input_error(
        self,
        action: str,
        error: ValidationError | ValueError,
        *,
        write_log: bool = True,
    ) -> None:
        message = self._input_error_message(error)
        self.validation_error_active = True
        self._clear_live_preview(f"{action} 불가 · {message}", error=True)
        self.state_badge.configure(text="●  입력 확인", fg_color="#452536", text_color="#FF9CAD")
        if write_log:
            self._write_log(f"{action} 입력 오류: {message}")

    def _update_preview(self, _event: object | None = None) -> None:
        """Refresh validation text after the request session was invalidated immediately."""
        self.preview_after_id = None
        if self.validation_error_active:
            return
        try:
            self._preview_config()
        except (ValidationError, ValueError) as error:
            self._show_input_error("입력", error, write_log=False)
            return
        self.validation_error_active = False
        self.state_badge.configure(text="●  준비됨", fg_color="#16372D", text_color=self.COLORS["success"])
        self._clear_live_preview("설정 준비됨 · ▦ 미리보기 버튼을 눌러 캐릭터를 선택하세요.")

    def _manual_preview(self) -> None:
        """Open the character selector; no network request starts before a character is clicked."""
        self._cancel_preview_update()
        if self.running:
            self._write_log("다운로드 중에는 미리보기를 새로 요청할 수 없습니다.")
            return
        try:
            config = self._preview_config()
        except (ValidationError, ValueError) as error:
            self._show_input_error("미리보기", error)
            return
        self.validation_error_active = False
        self.state_badge.configure(text="●  준비됨", fg_color="#16372D", text_color=self.COLORS["success"])
        self.preview_sequence += 1
        sequence = self.preview_sequence
        self.preview_cancel_event.set()
        self.preview_cancel_event = threading.Event()
        self.preview_config = config
        self._open_preview_selector(config)
        threading.Thread(
            target=self._cover_worker,
            args=(config, sequence, self.preview_cancel_event),
            daemon=True,
        ).start()
        referer_state = (
            f"Referer 적용: {urlparse(config.referer).netloc}"
            if config.referer
            else "Referer 자동 감지(403 대응)"
        )
        self.preview.configure(text=f"캐릭터를 선택하면 전체 이미지를 불러옵니다. · {referer_state}")

    def _ensure_preview_gallery(self) -> None:
        if getattr(self, "preview_gallery", None) and self.preview_gallery.winfo_exists():
            return
        self.preview_gallery = ctk.CTkToplevel(self)
        self.preview_gallery.title("Luma Fetch — 이미지 미리보기")
        self.preview_gallery.geometry("1020x740")
        self.preview_gallery.minsize(760, 540)
        self.preview_gallery.configure(fg_color=self.COLORS["bg"])
        self.preview_gallery.grid_columnconfigure(0, weight=1)
        self.preview_gallery.grid_rowconfigure(2, weight=1)
        self.gallery_summary = ctk.CTkLabel(self.preview_gallery, font=self._font(12), text_color=self.COLORS["muted"])
        self.gallery_summary.grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")
        self.gallery_tabs = ctk.CTkScrollableFrame(self.preview_gallery, height=46, orientation="horizontal", fg_color="transparent")
        self.gallery_tabs.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="ew")
        self.gallery_frame = ctk.CTkScrollableFrame(self.preview_gallery, fg_color="transparent")
        self.gallery_frame.grid(row=2, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.virtual_gallery = VirtualPreviewGrid(
            self.preview_gallery,
            colors=self.COLORS,
            font_factory=self._font,
            photo_factory=self._preview_photo_for_item,
            on_open=self._open_original,
        )
        self.virtual_gallery.grid(row=2, column=0, padx=14, pady=(0, 14), sticky="nsew")
        self.virtual_gallery.grid_remove()

    def _open_preview_selector(self, config: DownloadConfig) -> None:
        self._ensure_preview_gallery()
        self.virtual_gallery.grid_remove()
        self.gallery_frame.grid()
        self.virtual_gallery.clear()
        for child in self.gallery_tabs.winfo_children():
            child.destroy()
        for child in self.gallery_frame.winfo_children():
            child.destroy()
        self.gallery_tab_buttons: dict[str, ctk.CTkButton] = {}
        self.cover_labels: dict[str, tk.Label] = {}
        self.cover_photos: list[tk.PhotoImage] = []
        self.gallery_items = {}
        self.gallery_summary.configure(text=f"캐릭터 선택 · {len(config.expand_characters())}개 캐릭터 · 첫 이미지를 불러오는 중…")
        for column in range(4):
            self.gallery_frame.grid_columnconfigure(column, weight=1)
        for index, character in enumerate(config.expand_characters()):
            tab = ctk.CTkButton(self.gallery_tabs, text=character, width=82, height=30, corner_radius=10, fg_color="#273450", hover_color=self.COLORS["accent_hover"], font=self._font(10, "bold"), command=lambda value=character: self._load_preview_character(value))
            tab.pack(side="left", padx=4, pady=4)
            self.gallery_tab_buttons[character] = tab
            card = ctk.CTkFrame(self.gallery_frame, height=166, corner_radius=16, fg_color=self.COLORS["surface"], border_width=1, border_color="#344363")
            card.grid(row=index // 4, column=index % 4, padx=8, pady=8, sticky="nsew")
            card.grid_propagate(False)
            cover = tk.Label(card, text="첫 이미지 확인 중…", bg="#151F36", fg="#93A0BE", bd=0, highlightthickness=0)
            cover.pack(fill="both", expand=True, padx=7, pady=(7, 2))
            cover.bind("<Button-1>", lambda _event, value=character: self._load_preview_character(value))
            ctk.CTkButton(card, text=f"{character}  ·  전체 이미지", height=28, corner_radius=9, fg_color="#273450", hover_color=self.COLORS["accent_hover"], font=self._font(10, "bold"), command=lambda value=character: self._load_preview_character(value)).pack(fill="x", padx=7, pady=(0, 7))
            self.cover_labels[character] = cover

    def _cover_worker(self, config: DownloadConfig, sequence: int, cancelled: threading.Event) -> None:
        try:
            def on_cover(character: str, item: PreviewImage) -> None:
                self.events.put(("preview_cover_item", (sequence, character, item)))
            asyncio.run(fetch_preview_covers(config, on_cover, cancelled))
        except Exception as error:
            self.events.put(("preview_cover_error", (sequence, str(error))))

    def _render_cover(self, character: str, item: PreviewImage) -> None:
        target = self.cover_labels.get(character)
        if target is None:
            return
        try:
            photo = (
                tk.PhotoImage(data=item.thumbnail_ppm, format="PPM")
                if item.thumbnail_ppm
                else self._tk_photo(item.data, (132, 118))
            )
            self.cover_photos.append(photo)
            target.configure(image=photo, text="")
            target.image = photo
        except Exception as error:
            self._write_log(f"캐릭터 첫 이미지 표시 실패 [{character}]: {error}")
    def _load_preview_character(self, character: str) -> None:
        if not self.preview_config:
            return
        self.preview_sequence += 1
        sequence = self.preview_sequence
        previous_cache = getattr(self, "gallery_cache_dir", None)
        self.preview_cancel_event.set()
        self.preview_cancel_event = threading.Event()
        cache_dir = self.preview_cache_root / f"session-{sequence:08d}"
        self.gallery_cache_dir = cache_dir
        if previous_cache:
            threading.Thread(target=self._cleanup_preview_cache, args=(previous_cache,), daemon=True).start()
        self.gallery_selected_character = character
        for name, button in self.gallery_tab_buttons.items():
            button.configure(fg_color=self.COLORS["accent"] if name == character else "#273450")
        self.events.put(("live_preview_start", (sequence, character, len(self.preview_config.expand_situations()))))
        threading.Thread(target=self._live_preview_worker, args=(self.preview_config, character, sequence, self.preview_cancel_event, cache_dir), daemon=True).start()

    @staticmethod
    def _cleanup_preview_cache(path: Path) -> None:
        """Remove a stale session after cancellation without blocking Tk."""
        waiter = threading.Event()
        for _ in range(12):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
            waiter.wait(0.25)


    def _live_preview_worker(self, config: DownloadConfig, character: str, sequence: int, cancelled: threading.Event, cache_dir: Path) -> None:
        try:
            def on_item(index: int, item: PreviewImage) -> None:
                self.events.put(("live_preview_item", (sequence, index, item)))
            batch = asyncio.run(fetch_live_previews(config, character, on_item, cancelled, cache_dir))
            self.events.put(("live_preview_done", (sequence, batch)))
        except Exception as error:
            self.events.put(("live_preview_error", (sequence, str(error))))

    @staticmethod
    def _thumbnail(data: bytes, size: tuple[int, int]) -> Image.Image:
        with Image.open(BytesIO(data)) as source:
            return ImageOps.contain(source.convert("RGB"), size)

    def _tk_photo(self, data: bytes, size: tuple[int, int]) -> tk.PhotoImage:
        """Use Tk's built-in PPM decoder, avoiding optional Pillow/Tcl image codecs."""
        return tk.PhotoImage(data=encode_thumbnail_ppm(data, size), format="PPM")

    def _preview_photo_for_item(self, item: PreviewImage) -> tk.PhotoImage:
        if item.thumbnail_path:
            ppm = item.thumbnail_path.read_bytes()
        elif item.thumbnail_ppm:
            ppm = item.thumbnail_ppm
        elif item.data:
            ppm = encode_thumbnail_ppm(item.data, (178, 178))
        else:
            raise ValueError("썸네일 캐시가 없습니다.")
        return tk.PhotoImage(data=ppm, format="PPM")

    def _start_preview_gallery(self, character: str, total: int) -> None:
        self._ensure_preview_gallery()
        self.gallery_frame.grid_remove()
        self.virtual_gallery.grid()
        self.gallery_items = {}
        self.gallery_loaded = 0
        self.gallery_total = total
        self.virtual_gallery.reset(total)
        self.gallery_summary.configure(text=f"{character} · 전체 {total}장 요청 준비 · 스트리밍 수신 중…")

    def _render_preview_item(self, index: int, item: PreviewImage) -> None:
        if item.character != getattr(self, "gallery_selected_character", None):
            return
        self.gallery_items[index] = item
        self.virtual_gallery.add_item(index, item)
        self.gallery_loaded += 1
        if self.gallery_loaded == 1 or self.gallery_loaded % 8 == 0 or self.gallery_loaded == self.gallery_total:
            self.gallery_summary.configure(text=f"{item.character} · 수신 {self.gallery_loaded}장 · 전체 {self.gallery_total}장 요청 중")
            self.preview.configure(text=f"미리보기 수신 중 · {item.character} {self.gallery_loaded}장")

    def _finish_preview_batch(self, batch: PreviewBatch) -> None:
        character = getattr(self, "gallery_selected_character", "")
        if not batch.success:
            reason = batch.errors[0] if batch.errors else "서버가 이미지 데이터를 반환하지 않았습니다."
            self._clear_live_preview(f"미리보기를 불러오지 못했습니다.\n{reason}")
            self.gallery_summary.configure(text=f"{character} · 미리보기 실패 · {reason}")
            self._write_log(f"미리보기 실패: {reason}")
            return
        self.virtual_gallery.finish()
        detail = f" · {batch.errors[0]}" if batch.errors else ""
        self.gallery_summary.configure(text=f"{character} · 표시 {self.gallery_loaded}장 · 성공 {batch.success} · 실패 {batch.failed}{detail}")
        self.preview.configure(text=f"미리보기 완료 · {character} 성공 {batch.success} · 실패 {batch.failed}")
    def _ensure_original_viewer(self) -> None:
        if getattr(self, "original_viewer", None) and self.original_viewer.winfo_exists():
            return
        self.original_viewer = ctk.CTkToplevel(self)
        self.original_viewer.title("Luma Fetch — 원본 이미지")
        self.original_viewer.geometry("1180x860")
        self.original_viewer.minsize(720, 540)
        self.original_viewer.configure(fg_color=self.COLORS["bg"])
        self.original_viewer.grid_columnconfigure(1, weight=1)
        self.original_viewer.grid_rowconfigure(1, weight=1)
        self.viewer_mode: Literal["fit", "fill", "manual"] = "fit"
        self.viewer_zoom = 1.0
        self.viewer_current_scale = 1.0
        self.viewer_original_size = (1, 1)
        self.viewer_render_after_id: str | None = None
        self.viewer_fullscreen = False

        toolbar = ctk.CTkFrame(self.original_viewer, fg_color="transparent")
        toolbar.grid(row=0, column=0, columnspan=3, padx=18, pady=(12, 8), sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)
        self.viewer_summary = ctk.CTkLabel(
            toolbar, text="원본 이미지를 준비합니다…", anchor="w",
            font=self._font(11, "bold"), text_color=self.COLORS["muted"],
        )
        self.viewer_summary.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.viewer_fit_button = ctk.CTkButton(
            toolbar, text="맞춤", width=66, height=30, corner_radius=10,
            fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"],
            command=lambda: self._set_viewer_mode("fit"),
        )
        self.viewer_fit_button.grid(row=0, column=1, padx=3)
        self.viewer_fill_button = ctk.CTkButton(
            toolbar, text="화면 채우기", width=94, height=30, corner_radius=10,
            fg_color="#273450", hover_color=self.COLORS["accent_hover"],
            command=lambda: self._set_viewer_mode("fill"),
        )
        self.viewer_fill_button.grid(row=0, column=2, padx=3)
        self.viewer_actual_button = ctk.CTkButton(
            toolbar, text="100%", width=66, height=30, corner_radius=10,
            fg_color="#273450", hover_color=self.COLORS["accent_hover"],
            command=self._show_viewer_actual_size,
        )
        self.viewer_actual_button.grid(row=0, column=3, padx=3)
        self.viewer_fullscreen_button = ctk.CTkButton(
            toolbar, text="전체 화면", width=82, height=30, corner_radius=10,
            fg_color="#273450", hover_color=self.COLORS["accent_hover"],
            command=self._toggle_viewer_fullscreen,
        )
        self.viewer_fullscreen_button.grid(row=0, column=4, padx=(3, 0))

        self.viewer_previous = ctk.CTkButton(
            self.original_viewer, text="‹", width=58, height=58, corner_radius=18,
            fg_color="#273450", hover_color=self.COLORS["accent_hover"],
            font=self._font(28, "bold"), command=lambda: self._navigate_original(-1),
        )
        self.viewer_previous.grid(row=1, column=0, padx=(18, 8), sticky="w")
        self.viewer_next = ctk.CTkButton(
            self.original_viewer, text="›", width=58, height=58, corner_radius=18,
            fg_color="#273450", hover_color=self.COLORS["accent_hover"],
            font=self._font(28, "bold"), command=lambda: self._navigate_original(1),
        )
        self.viewer_next.grid(row=1, column=2, padx=(8, 18), sticky="e")

        viewport = ctk.CTkFrame(self.original_viewer, corner_radius=14, fg_color=self.COLORS["input"])
        viewport.grid(row=1, column=1, padx=4, pady=(0, 14), sticky="nsew")
        viewport.grid_columnconfigure(0, weight=1)
        viewport.grid_rowconfigure(0, weight=1)
        self.viewer_canvas = tk.Canvas(
            viewport, bg=self.COLORS["input"], bd=0, highlightthickness=0,
            cursor="fleur", xscrollincrement=24, yscrollincrement=24,
        )
        self.viewer_canvas.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=(6, 0))
        self.viewer_vscroll = ctk.CTkScrollbar(viewport, command=self.viewer_canvas.yview)
        self.viewer_vscroll.grid(row=0, column=1, sticky="ns", padx=(4, 5), pady=(6, 0))
        self.viewer_hscroll = ctk.CTkScrollbar(viewport, orientation="horizontal", command=self.viewer_canvas.xview)
        self.viewer_hscroll.grid(row=1, column=0, sticky="ew", padx=(6, 0), pady=(4, 5))
        self.viewer_canvas.configure(
            xscrollcommand=self.viewer_hscroll.set,
            yscrollcommand=self.viewer_vscroll.set,
        )
        self.viewer_canvas.bind("<MouseWheel>", self._on_viewer_wheel)
        self.viewer_canvas.bind("<Configure>", self._on_viewer_resize)
        self.viewer_canvas.bind("<ButtonPress-1>", lambda event: self.viewer_canvas.scan_mark(event.x, event.y))
        self.viewer_canvas.bind("<B1-Motion>", lambda event: self.viewer_canvas.scan_dragto(event.x, event.y, gain=1))
        self.original_viewer.bind("<Left>", lambda _event: self._navigate_original(-1))
        self.original_viewer.bind("<Right>", lambda _event: self._navigate_original(1))
        self.original_viewer.bind("<F11>", lambda _event: self._toggle_viewer_fullscreen())
        self.original_viewer.bind("<Escape>", self._on_viewer_escape)
        self.original_viewer.protocol("WM_DELETE_WINDOW", self._hide_original_viewer)

    def _hide_original_viewer(self) -> None:
        self.viewer_sequence += 1
        if getattr(self, "viewer_fullscreen", False):
            self.original_viewer.attributes("-fullscreen", False)
            self.viewer_fullscreen = False
        self.original_viewer.withdraw()

    def _on_viewer_escape(self, _event: object | None = None) -> None:
        if self.viewer_fullscreen:
            self._toggle_viewer_fullscreen()
        else:
            self._hide_original_viewer()

    def _toggle_viewer_fullscreen(self) -> None:
        self.viewer_fullscreen = not self.viewer_fullscreen
        self.original_viewer.attributes("-fullscreen", self.viewer_fullscreen)
        self.viewer_fullscreen_button.configure(text="창 화면" if self.viewer_fullscreen else "전체 화면")
        self._schedule_viewer_render(140)

    def _sync_viewer_mode_buttons(self) -> None:
        active = self.COLORS["accent"]
        inactive = "#273450"
        self.viewer_fit_button.configure(fg_color=active if self.viewer_mode == "fit" else inactive)
        self.viewer_fill_button.configure(fg_color=active if self.viewer_mode == "fill" else inactive)
        self.viewer_actual_button.configure(
            fg_color=active if self.viewer_mode == "manual" and abs(self.viewer_zoom - 1.0) < 0.001 else inactive
        )

    def _set_viewer_mode(self, mode: Literal["fit", "fill"]) -> None:
        self.viewer_mode = mode
        self._sync_viewer_mode_buttons()
        self._schedule_viewer_render(20)

    def _show_viewer_actual_size(self) -> None:
        self.viewer_mode = "manual"
        self.viewer_zoom = 1.0
        self._sync_viewer_mode_buttons()
        self._schedule_viewer_render(20)

    def _on_viewer_wheel(self, event: tk.Event[tk.Misc]) -> str:
        if not getattr(self, "viewer_current_item", None):
            return "break"
        factor = 1.15 if (event.delta or 0) > 0 else 1 / 1.15
        base = self.viewer_current_scale if self.viewer_mode != "manual" else self.viewer_zoom
        self.viewer_zoom = min(4.0, max(0.08, base * factor))
        self.viewer_mode = "manual"
        self._sync_viewer_mode_buttons()
        self._schedule_viewer_render(55)
        return "break"

    def _on_viewer_resize(self, _event: tk.Event[tk.Misc]) -> None:
        if getattr(self, "viewer_mode", "fit") in {"fit", "fill"} and getattr(self, "viewer_current_item", None):
            self._schedule_viewer_render(140)

    def _schedule_viewer_render(self, delay: int = 0) -> None:
        if self.viewer_render_after_id:
            self.after_cancel(self.viewer_render_after_id)
        self.viewer_render_after_id = self.after(delay, self._request_viewer_render)

    def _open_original(self, index: int) -> None:
        indices = sorted(self.gallery_items)
        if index not in self.gallery_items or not indices:
            return
        self._ensure_original_viewer()
        self.viewer_indices = indices
        self.viewer_position = indices.index(index)
        self.viewer_mode = "fit"
        self._sync_viewer_mode_buttons()
        self.original_viewer.deiconify()
        self.original_viewer.lift()
        self.original_viewer.focus_force()
        self._show_original_at_position()

    def _navigate_original(self, step: int) -> None:
        indices = sorted(self.gallery_items)
        if not indices:
            return
        previous = self.viewer_indices[self.viewer_position] if getattr(self, "viewer_indices", []) else indices[0]
        current_position = indices.index(previous) if previous in indices else 0
        self.viewer_indices = indices
        self.viewer_position = (current_position + step) % len(indices)
        self.viewer_mode = "fit"
        self._sync_viewer_mode_buttons()
        self._show_original_at_position()

    def _show_original_at_position(self) -> None:
        index = self.viewer_indices[self.viewer_position]
        item = self.gallery_items.get(index)
        if item is None:
            return
        self.viewer_current_item = item
        self.viewer_canvas.delete("all")
        self.viewer_canvas.create_text(
            max(1, self.viewer_canvas.winfo_width()) // 2,
            max(1, self.viewer_canvas.winfo_height()) // 2,
            text="원본 이미지를 불러오는 중…", fill="#AAB7D8", font=("Segoe UI Variable", 13),
        )
        self._schedule_viewer_render(10)

    def _request_viewer_render(self) -> None:
        self.viewer_render_after_id = None
        item = getattr(self, "viewer_current_item", None)
        if item is None:
            return
        self.viewer_sequence += 1
        sequence = self.viewer_sequence
        self.original_viewer.update_idletasks()
        viewport_size = (
            max(320, self.viewer_canvas.winfo_width() - 8),
            max(240, self.viewer_canvas.winfo_height() - 8),
        )
        threading.Thread(
            target=self._viewer_decode_worker,
            args=(
                sequence, item, viewport_size, self.viewer_mode, self.viewer_zoom,
                self.viewer_position, len(self.viewer_indices),
            ),
            daemon=True,
        ).start()

    def _viewer_decode_worker(
        self,
        sequence: int,
        item: PreviewImage,
        viewport_size: tuple[int, int],
        mode: Literal["fit", "fill", "manual"],
        zoom: float,
        position: int,
        total: int,
    ) -> None:
        try:
            source_value: Path | BytesIO
            source_value = item.cache_path if item.cache_path else BytesIO(item.data)
            with Image.open(source_value) as source:
                original_size = source.size
                width, height = original_size
                match mode:
                    case "fit":
                        scale = min(viewport_size[0] / width, viewport_size[1] / height)
                    case "fill":
                        scale = max(viewport_size[0] / width, viewport_size[1] / height)
                    case _:
                        scale = zoom
                pixel_limit_scale = (16_000_000 / max(1, width * height)) ** 0.5
                scale = min(4.0, pixel_limit_scale, max(0.08, scale))
                display_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                rgb = source.convert("RGB")
                display = rgb if display_size == original_size else rgb.resize(display_size, Image.Resampling.LANCZOS)
            buffer = BytesIO()
            display.save(buffer, format="PPM")
            self.events.put((
                "viewer_image",
                (sequence, item, buffer.getvalue(), original_size, display_size, position, total),
            ))
        except Exception as error:
            self.events.put(("viewer_error", (sequence, item, str(error))))

    def _render_viewer_image(
        self,
        item: PreviewImage,
        ppm: bytes,
        original_size: tuple[int, int],
        display_size: tuple[int, int],
        position: int,
        total: int,
    ) -> None:
        self.viewer_photo = tk.PhotoImage(data=ppm, format="PPM")
        canvas_width = max(1, self.viewer_canvas.winfo_width())
        canvas_height = max(1, self.viewer_canvas.winfo_height())
        region_width = max(canvas_width, display_size[0])
        region_height = max(canvas_height, display_size[1])
        image_x = max(0, (region_width - display_size[0]) // 2)
        image_y = max(0, (region_height - display_size[1]) // 2)
        self.viewer_canvas.delete("all")
        self.viewer_canvas.create_image(image_x, image_y, image=self.viewer_photo, anchor="nw")
        self.viewer_canvas.configure(scrollregion=(0, 0, region_width, region_height))
        if region_width > canvas_width:
            self.viewer_canvas.xview_moveto((region_width - canvas_width) / 2 / region_width)
        else:
            self.viewer_canvas.xview_moveto(0)
        if region_height > canvas_height:
            self.viewer_canvas.yview_moveto((region_height - canvas_height) / 2 / region_height)
        else:
            self.viewer_canvas.yview_moveto(0)
        self.viewer_original_size = original_size
        self.viewer_current_scale = display_size[0] / max(1, original_size[0])
        if self.viewer_mode == "manual":
            self.viewer_zoom = self.viewer_current_scale
        mode_text = {"fit": "화면 맞춤", "fill": "화면 채우기", "manual": "수동 확대"}[self.viewer_mode]
        self.viewer_summary.configure(
            text=(
                f"{item.character} / {item.situation}  ·  {position + 1}/{total}  ·  "
                f"원본 {original_size[0]}×{original_size[1]}  ·  {mode_text} {self.viewer_current_scale:.0%}  ·  휠 확대/축소"
            )
        )
    def _render_favorites(self) -> None:
        if not hasattr(self, "favorite_grid"):
            return
        self.favorite_render_after_id = None
        query = self.favorite_search_var.get().strip().casefold()
        filtered = [
            favorite for favorite in self.favorites
            if not query or query in " ".join((
                favorite.name, favorite.template_url, favorite.character,
                favorite.ranges, favorite.referer or "",
            )).casefold()
        ]
        page_count = max(1, (len(filtered) + FAVORITES_PAGE_SIZE - 1) // FAVORITES_PAGE_SIZE)
        self.favorite_page = min(self.favorite_page, page_count - 1)
        first = self.favorite_page * FAVORITES_PAGE_SIZE
        visible = filtered[first:first + FAVORITES_PAGE_SIZE]
        for child in self.favorite_grid.winfo_children():
            child.destroy()
        self.favorite_count_label.configure(text=f"전체 {len(self.favorites):,}개 · 검색 결과 {len(filtered):,}개")
        self.favorite_page_label.configure(text=f"{self.favorite_page + 1} / {page_count}")
        self.favorite_prev_button.configure(state="normal" if self.favorite_page > 0 else "disabled")
        self.favorite_next_button.configure(state="normal" if self.favorite_page + 1 < page_count else "disabled")
        self.favorites_nav_button.configure(text=f"★  즐겨찾기  {len(self.favorites):,}")
        if not visible:
            message = "검색 결과가 없습니다." if query else "저장된 즐겨찾기가 없습니다.\n다운로드 화면에서 현재 설정을 즐겨찾기로 저장하세요."
            ctk.CTkLabel(self.favorite_grid, text=message, justify="center", font=self._font(12), text_color="#7180A5").grid(row=0, column=0, columnspan=2, padx=20, pady=70)
            return
        for index, favorite in enumerate(visible):
            card = ctk.CTkFrame(self.favorite_grid, corner_radius=16, fg_color=self.COLORS["surface"], border_width=1, border_color="#26314E")
            card.grid(row=index // 2, column=index % 2, padx=7, pady=7, sticky="nsew")
            card.grid_columnconfigure((0, 1, 2), weight=1, uniform="favorite_action")
            ctk.CTkButton(
                card, text=favorite.name, anchor="w", height=32, corner_radius=9,
                fg_color="transparent", hover_color="#273450", font=self._font(12, "bold"),
                command=lambda value=favorite: self._preview_favorite(value),
            ).grid(row=0, column=0, columnspan=4, padx=10, pady=(9, 2), sticky="ew")
            host = urlparse(favorite.template_url).netloc
            ctk.CTkLabel(card, text=host, anchor="w", font=self._font(9, "bold"), text_color=self.COLORS["accent"]).grid(row=1, column=0, columnspan=4, padx=13, sticky="ew")
            details = f"캐릭터  {favorite.character}   ·   범위  {favorite.ranges}"
            ctk.CTkLabel(card, text=details, anchor="w", justify="left", wraplength=320, font=self._font(10), text_color="#AAB7D8").grid(row=2, column=0, columnspan=4, padx=13, pady=(2, 8), sticky="ew")
            ctk.CTkButton(card, text="미리보기", height=29, corner_radius=9, fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"], font=self._font(9, "bold"), command=lambda value=favorite: self._preview_favorite(value)).grid(row=3, column=0, padx=(10, 4), pady=(0, 10), sticky="ew")
            ctk.CTkButton(card, text="다운로드", height=29, corner_radius=9, fg_color="#273450", hover_color="#334364", font=self._font(9, "bold"), command=lambda value=favorite: self._download_favorite(value)).grid(row=3, column=1, padx=4, pady=(0, 10), sticky="ew")
            ctk.CTkButton(card, text="불러오기", height=29, corner_radius=9, fg_color="#273450", hover_color="#334364", font=self._font(9), command=lambda value=favorite: self._load_favorite_to_form(value)).grid(row=3, column=2, padx=4, pady=(0, 10), sticky="ew")
            ctk.CTkButton(card, text="삭제", width=48, height=29, corner_radius=9, fg_color="#342133", hover_color="#49283C", text_color="#FFB3C0", font=self._font(9), command=lambda value=favorite: self._delete_favorite(value)).grid(row=3, column=3, padx=(4, 10), pady=(0, 10))

    def _apply_favorite(self, favorite: FavoritePreset) -> None:
        values = {
            "템플릿 URL": favorite.template_url,
            "캐릭터 코드": favorite.character,
            "의상 코드": favorite.outfit,
            "상황 코드 범위": favorite.ranges,
            "동시 다운로드": str(favorite.concurrency),
            "Referer": favorite.referer or "",
        }
        self.preview_updates_suspended = True
        try:
            for label, value in values.items():
                self.entry_vars[label].set(value)
            self.separate_folders_var.set(favorite.separate_character_folders)
            self.defender_scan_var.set(favorite.scan_with_defender)
            self.folder_var.set(favorite.destination or "")
        finally:
            self.preview_updates_suspended = False
        self.validation_error_active = False
        self.preview.configure(fg_color="transparent", text_color="#AAB7D8")
        self._write_log(f"즐겨찾기 불러옴: {favorite.name}")

    def _save_current_favorite(self) -> None:
        from tkinter import filedialog, messagebox
        self._cancel_preview_update()
        try:
            config = self._preview_config()
            concurrency = int(self.entries["동시 다운로드"].get().strip() or 20)
            if not 1 <= concurrency <= 50:
                raise ValueError("동시 다운로드는 1~50 사이여야 합니다.")
        except (ValidationError, ValueError) as error:
            self._show_input_error("즐겨찾기 저장", error)
            return
        dialog = ctk.CTkInputDialog(text="즐겨찾기 이름을 입력하세요.", title="즐겨찾기 저장")
        if not (name := (dialog.get_input() or "").strip()):
            return
        fixed_destination = messagebox.askyesno(
            "저장 위치 고정",
            "현재 저장 위치를 이 즐겨찾기에 고정할까요?\n\n아니요를 선택하면 바로 다운로드할 때마다 폴더를 묻습니다.",
            parent=self,
        )
        destination = self.folder_var.get().strip() or None
        if fixed_destination and not destination:
            destination = filedialog.askdirectory(initialdir=str(Path.cwd()), parent=self) or None
            if not destination:
                self._write_log("고정 저장 위치 선택이 취소되어 즐겨찾기를 저장하지 않았습니다.")
                return
            self.folder_var.set(destination)
        try:
            preset = FavoritePreset(
                name=name,
                template_url=config.template_url,
                character=config.character,
                ranges=config.ranges,
                outfit=config.outfit,
                referer=config.referer,
                concurrency=concurrency,
                separate_character_folders=self.separate_folders_var.get(),
                scan_with_defender=self.defender_scan_var.get(),
                fixed_destination=fixed_destination,
                destination=destination if fixed_destination else None,
            )
        except (ValidationError, ValueError) as error:
            self._show_input_error("즐겨찾기 저장", error)
            return
        existing = next((item for item in self.favorites if item.name.casefold() == preset.name.casefold()), None)
        if existing and not messagebox.askyesno("즐겨찾기 갱신", f"'{existing.name}' 설정을 덮어쓸까요?", parent=self):
            return
        if existing:
            self.favorites[self.favorites.index(existing)] = preset
        else:
            if len(self.favorites) >= MAX_FAVORITES:
                self._write_log(f"즐겨찾기는 최대 {MAX_FAVORITES}개까지 저장할 수 있습니다.")
                return
            self.favorites.append(preset)
        try:
            save_favorites(self.favorites)
        except OSError as error:
            self._write_log(f"즐겨찾기 파일 저장 실패: {error}")
            return
        self._render_favorites()
        self._write_log(f"즐겨찾기 저장 완료: {preset.name}")

    def _preview_favorite(self, favorite: FavoritePreset) -> None:
        if self.running:
            self._write_log("다운로드 중에는 즐겨찾기 미리보기를 열 수 없습니다.")
            return
        self._apply_favorite(favorite)
        self._manual_preview()

    def _download_favorite(self, favorite: FavoritePreset) -> None:
        from tkinter import filedialog
        if self.running:
            self._write_log("이미 다운로드가 진행 중입니다.")
            return
        self._apply_favorite(favorite)
        if favorite.fixed_destination and favorite.destination:
            self.folder_var.set(favorite.destination)
        else:
            selected = filedialog.askdirectory(
                initialdir=self.folder_var.get() or str(Path.cwd()),
                title=f"{favorite.name} 저장 위치",
                parent=self,
            )
            if not selected:
                self._write_log("즐겨찾기 다운로드가 취소되었습니다.")
                return
            self.folder_var.set(selected)
        self._start()

    def _delete_favorite(self, favorite: FavoritePreset) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno("즐겨찾기 삭제", f"'{favorite.name}' 항목을 삭제할까요?", parent=self):
            return
        self.favorites = [item for item in self.favorites if item is not favorite]
        try:
            save_favorites(self.favorites)
        except OSError as error:
            self._write_log(f"즐겨찾기 파일 저장 실패: {error}")
            return
        self._render_favorites()
        self._write_log(f"즐겨찾기 삭제: {favorite.name}")
    def _choose_folder(self) -> None:
        from tkinter import filedialog
        if selected := filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.cwd())): self.folder_var.set(selected)

    def _config(self) -> DownloadConfig:
        folder = self.folder_var.get().strip()
        if not folder:
            raise ValueError("저장 폴더를 선택하세요.")
        return DownloadConfig(
            template_url=self.entries["템플릿 URL"].get(),
            character=self.entries["캐릭터 코드"].get(),
            ranges=self.entries["상황 코드 범위"].get(),
            outfit=self.entries["의상 코드"].get(),
            referer=self.entries["Referer"].get().strip() or None,
            concurrency=int(self.entries["동시 다운로드"].get().strip() or 20),
            destination=Path(folder).expanduser(),
            separate_character_folders=self.separate_folders_var.get(),
            scan_with_defender=self.defender_scan_var.get(),
        )

    def _start(self) -> None:
        if self.running: return
        try:
            config = self._config()
        except (ValidationError, ValueError) as error:
            self._show_input_error("다운로드", error)
            return
        self.running, self.cancel_event = True, threading.Event()
        self.progress.set(0); self.log.delete("1.0", "end")
        self.start_button.configure(state="disabled"); self.preview_button.configure(state="disabled"); self.cancel_button.configure(state="normal")
        self.state_badge.configure(text="●  진행 중", fg_color="#293562", text_color="#B7C1FF")
        self.status.configure(text=f"0 / {len(config.expand_characters()) * len(config.expand_situations())}개를 준비했습니다.")
        self._write_log("다운로드 큐를 시작했습니다.")
        threading.Thread(target=self._worker, args=(config,), daemon=True).start()

    def _worker(self, config: DownloadConfig) -> None:
        try:
            notify = lambda kind, payload: self.events.put((kind, payload))
            self.events.put(("done", asyncio.run(Downloader(config, self.cancel_event, notify).run())))
        except Exception as error: self.events.put(("error", str(error)))

    def _cancel(self) -> None:
        self.cancel_event.set(); self.cancel_button.configure(state="disabled")
        self.state_badge.configure(text="●  취소 중", fg_color="#452536", text_color="#FFB1BD")
        self.status.configure(text="취소 요청됨 — 진행 중인 연결을 정리합니다…")

    def _poll_events(self) -> None:
        # Drain bursts quickly, but cap each UI slice so input and scrolling stay responsive.
        event_budget = 64 if self.events.qsize() > 24 else 16
        for _ in range(event_budget):
            if self.events.empty():
                break
            kind, payload = self.events.get_nowait()
            if kind == "log": self._write_log(str(payload))
            elif kind == "preview_cover_item":
                sequence, character, item = payload
                if sequence == self.preview_sequence:
                    assert isinstance(item, PreviewImage)
                    self._render_cover(character, item)
            elif kind == "preview_cover_error":
                sequence, message = payload
                if sequence == self.preview_sequence:
                    self._write_log(f"캐릭터 첫 이미지 요청 오류: {message}")
            elif kind == "live_preview_start":
                sequence, character, total = payload
                if sequence == self.preview_sequence:
                    self._start_preview_gallery(character, total)
            elif kind == "live_preview_item":
                sequence, index, item = payload
                if sequence == self.preview_sequence:
                    assert isinstance(item, PreviewImage)
                    self._render_preview_item(index, item)
            elif kind == "live_preview_done":
                sequence, batch = payload
                if sequence == self.preview_sequence:
                    assert isinstance(batch, PreviewBatch)
                    self._finish_preview_batch(batch)
            elif kind == "live_preview_error":
                sequence, message = payload
                if sequence == self.preview_sequence:
                    self._clear_live_preview(f"미리보기를 불러오지 못했습니다. {message}")
            elif kind == "viewer_image":
                sequence, item, ppm, original_size, display_size, position, total = payload
                if sequence == self.viewer_sequence:
                    self._render_viewer_image(item, ppm, original_size, display_size, position, total)
            elif kind == "viewer_error":
                sequence, item, message = payload
                if sequence == self.viewer_sequence:
                    self.viewer_canvas.delete("all")
                    self.viewer_canvas.create_text(max(1, self.viewer_canvas.winfo_width()) // 2, max(1, self.viewer_canvas.winfo_height()) // 2, text="원본 이미지를 표시할 수 없습니다.", fill="#AAB7D8", font=("Segoe UI Variable", 13))
                    self.viewer_summary.configure(text=f"{item.character} / {item.situation} · {message}")
            elif kind == "update_result":
                assert isinstance(payload, UpdateInfo)
                self._handle_update_result(payload)
            elif kind == "update_error":
                self._handle_update_error(str(payload))
            elif kind == "update_download_progress":
                downloaded, total = payload
                self._handle_update_download_progress(int(downloaded), int(total))
            elif kind == "update_download_done":
                assert isinstance(payload, Path)
                self._handle_update_download_done(payload)
            elif kind == "update_download_error":
                self._handle_update_download_error(str(payload))
            elif kind == "progress":
                stats = payload; assert isinstance(stats, DownloadStats)
                done = stats.success + stats.failed + stats.cancelled
                self.progress.set(done / stats.total if stats.total else 0)
                self.status.configure(text=f"{done}/{stats.total}개 처리 중")
                self.stats_label.configure(text=f"성공 {stats.success}   실패 {stats.failed}   취소 {stats.cancelled}")
            elif kind in {"done", "error"}:
                self.running = False; self.start_button.configure(state="normal"); self.preview_button.configure(state="normal"); self.cancel_button.configure(state="disabled")
                if kind == "done":
                    stats = payload; self.state_badge.configure(text="●  완료", fg_color="#16372D", text_color=self.COLORS["success"])
                    self._write_log(f"완료 — 성공 {stats.success}, 실패 {stats.failed}, 취소 {stats.cancelled}")
                else:
                    self.state_badge.configure(text="●  오류", fg_color="#452536", text_color="#FF9CAD"); self._write_log(f"오류: {payload}")
        self.after(12 if not self.events.empty() else 30, self._poll_events)

    def _write_log(self, message: str) -> None:
        self.log.insert("end", message + "\n"); self.log.see("end")

    def _on_close(self) -> None:
        """Stop pending UI work so a standard installer close exits the process."""
        self.cancel_event.set()
        self.preview_cancel_event.set()
        if update_cancel := getattr(self, "update_download_cancel", None):
            update_cancel.set()
        if viewer_render := getattr(self, "viewer_render_after_id", None):
            self.after_cancel(viewer_render)
            self.viewer_render_after_id = None
        self._cancel_preview_update()
        if gallery := getattr(self, "preview_gallery", None):
            if gallery.winfo_exists():
                gallery.destroy()
        if viewer := getattr(self, "original_viewer", None):
            if viewer.winfo_exists():
                viewer.destroy()
        shutil.rmtree(self.preview_cache_root, ignore_errors=True)
        self.quit()
        self.destroy()
    def _open_folder(self) -> None:
        selected = self.folder_var.get().strip()
        if not selected:
            self._write_log("저장 폴더를 먼저 선택하세요."); return
        folder = Path(selected).expanduser(); folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32": os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin": subprocess.run(["open", str(folder)], check=False)
        else: subprocess.run(["xdg-open", str(folder)], check=False)

if __name__ == "__main__":
    DownloaderApp().mainloop()
