"""Modern async image batch downloader (Python 3.12+)."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
import customtkinter as ctk
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn


RANGE_PATTERN = re.compile(r"^\s*(\d+)\s*\.\.\s*(\d+)\s*$")
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
    character: str = Field(min_length=1, max_length=64)
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
        return SecurityGuard.validate_url(value.strip()) if value else None

    @field_validator("character")
    @classmethod
    def normalize_characters(cls, value: str) -> str:
        codes = [code.strip() for code in value.split(",") if code.strip()]
        if not codes:
            raise ValueError("캐릭터 코드를 하나 이상 입력하세요.")
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


@dataclass(slots=True)
class DownloadStats:
    total: int
    success: int = 0
    failed: int = 0
    cancelled: int = 0


class Downloader:
    def __init__(self, config: DownloadConfig, cancelled: threading.Event, notify: Callable[[str, object], None]):
        self.config = config
        self.cancelled = cancelled
        self.notify = notify
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
                        async with session.get(url, allow_redirects=False) as response:
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
        headers = {"User-Agent": "Mozilla/5.0 (Image Batch Downloader)"}
        if self.config.referer:
            headers["Referer"] = self.config.referer
        progress = Progress(SpinnerColumn(), TextColumn("다운로드"), BarColumn(), "{task.completed}/{task.total}", TimeRemainingColumn())
        with progress:
            task_id = progress.add_task("images", total=self.stats.total)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
                semaphore = asyncio.Semaphore(self.config.concurrency)
                tasks = [asyncio.create_task(self.download_one(session, semaphore, character, situation))
                         for character in self.config.expand_characters()
                         for situation in self.config.expand_situations()]
                for task in asyncio.as_completed(tasks):
                    result = await task
                    match result:
                        case "success": self.stats.success += 1
                        case "failed": self.stats.failed += 1
                        case _: self.stats.cancelled += 1
                    progress.advance(task_id)
                    self.notify("progress", self.stats)
        return self.stats


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
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.entries: dict[str, ctk.CTkEntry] = {}
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

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
        nav = self._card(sidebar)
        nav.configure(fg_color="#17213A", border_width=0)
        nav.pack(fill="x", padx=14)
        ctk.CTkLabel(nav, text="↓  일괄 다운로드", font=self._font(11, "bold"), height=38, anchor="w").pack(fill="x", padx=12, pady=5)
        ctk.CTkLabel(sidebar, text="ASYNC · RETRY · FAST", font=self._font(9, "bold"), text_color=self.COLORS["muted"]).pack(anchor="w", padx=22, pady=(28, 8))
        ctk.CTkLabel(sidebar, text="여러 캐릭터와\n상황 범위를 한 번에 처리합니다.", justify="left", font=self._font(11), text_color=self.COLORS["muted"]).pack(anchor="w", padx=22)
        ctk.CTkLabel(sidebar, text="v1.3", font=self._font(10), text_color="#5F6E92").pack(side="bottom", anchor="w", padx=22, pady=22)

        main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=22, pady=16)
        main.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="이미지 수집", font=self._font(25, "bold"), text_color=self.COLORS["text"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="템플릿 · 캐릭터 · 범위를 입력하세요.", font=self._font(12), text_color=self.COLORS["muted"]).grid(row=1, column=0, sticky="w")
        self.state_badge = ctk.CTkLabel(header, text="●  준비됨", corner_radius=14, fg_color="#16372D", text_color=self.COLORS["success"], font=self._font(11, "bold"), padx=12, pady=6)
        self.state_badge.grid(row=0, column=1, rowspan=2, sticky="e")

        form = self._card(main)
        form.grid(row=1, column=0, sticky="ew")
        form.grid_columnconfigure((0, 1), weight=1)
        self._entry(form, "템플릿 URL", "치환 토큰: 캐릭터 · 상황 · 의상", 0, 0, span=2)
        self._entry(form, "캐릭터 코드", "쉼표로 여러 코드", 1, 0)
        self._entry(form, "의상 코드", "공란 = X", 1, 1)
        self._entry(form, "상황 코드 범위", "시작..끝, 시작..끝", 2, 0)
        self._entry(form, "동시 다운로드", "공란 = 20", 2, 1)
        self._entry(form, "Referer", "필요 시 원본 페이지 주소", 3, 0, span=2)
        self.preview = ctk.CTkLabel(form, text="URL 미리보기는 입력 후 표시됩니다.", text_color="#AAB7D8", font=self._font(10))
        self.preview.grid(row=4, column=0, columnspan=2, padx=18, pady=(0, 12), sticky="w")

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
        lower.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        lower.grid_columnconfigure(0, weight=1)
        activity = self._card(lower)
        activity.configure(height=92)
        activity.grid(row=0, column=0, sticky="ew")
        activity.grid_propagate(False)
        activity.grid_columnconfigure(0, weight=1); activity.grid_rowconfigure(0, weight=1)
        self.log = ctk.CTkTextbox(activity, corner_radius=14, fg_color=self.COLORS["input"], border_width=0, font=self._font(11), text_color="#B9C5E3")
        self.log.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        actions = ctk.CTkFrame(main, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="e", pady=(10, 0))
        self.cancel_button = ctk.CTkButton(actions, text="취소", width=92, height=40, corner_radius=13, state="disabled", fg_color="#342133", hover_color="#49283C", text_color="#FFB3C0", command=self._cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.start_button = ctk.CTkButton(actions, text="↓  다운로드 시작", width=155, height=40, corner_radius=13, fg_color=self.COLORS["accent"], hover_color=self.COLORS["accent_hover"], font=self._font(11, "bold"), command=self._start)
        self.start_button.pack(side="right")
    def _entry(self, parent: ctk.CTkFrame, label: str, placeholder: str, row: int, column: int, *, span: int = 1, initial: str = "") -> None:
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.grid(row=row, column=column, columnspan=span, padx=18, pady=(9 if row == 0 else 3, 3), sticky="ew")
        holder.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(holder, text=label, font=self._font(11, "bold"), text_color="#DDE5FF").grid(row=0, column=0, sticky="w", pady=(0, 3))
        entry = ctk.CTkEntry(holder, placeholder_text=placeholder, height=34, corner_radius=11, fg_color=self.COLORS["input"], border_color="#2A3655", font=self._font(11))
        if initial: entry.insert(0, initial)
        entry.grid(row=1, column=0, sticky="ew")
        entry.bind("<KeyRelease>", self._update_preview)
        self.entries[label] = entry

    def _update_preview(self, _event: object | None = None) -> None:
        template = self.entries["템플릿 URL"].get().strip()
        if not template:
            self.preview.configure(text="URL 미리보기는 템플릿과 코드를 입력하면 표시됩니다.")
            return
        try:
            sample = normalize_template_url(template).format(
                char=(self.entries["캐릭터 코드"].get().split(",")[0].strip() or "CHAR"),
                situation="0001", outfit=self.entries["의상 코드"].get().strip() or "X",
                pose=f"0001{self.entries['의상 코드'].get().strip() or 'X'}",
            )
            self.preview.configure(text=f"URL 미리보기: {sample}")
        except (KeyError, ValueError):
            self.preview.configure(text="URL 미리보기: 지원하는 표기({char}, {pose}, 캐릭터, 상황, 의상)를 확인하세요.")

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
        try: config = self._config()
        except (ValidationError, ValueError) as error:
            self._write_log(f"입력 오류: {error}"); self.state_badge.configure(text="●  확인 필요", fg_color="#452536", text_color="#FF9CAD"); return
        self.running, self.cancel_event = True, threading.Event()
        self.progress.set(0); self.log.delete("1.0", "end")
        self.start_button.configure(state="disabled"); self.cancel_button.configure(state="normal")
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
        while not self.events.empty():
            kind, payload = self.events.get_nowait()
            if kind == "log": self._write_log(str(payload))
            elif kind == "progress":
                stats = payload; assert isinstance(stats, DownloadStats)
                done = stats.success + stats.failed + stats.cancelled
                self.progress.set(done / stats.total if stats.total else 0)
                self.status.configure(text=f"{done}/{stats.total}개 처리 중")
                self.stats_label.configure(text=f"성공 {stats.success}   실패 {stats.failed}   취소 {stats.cancelled}")
            elif kind in {"done", "error"}:
                self.running = False; self.start_button.configure(state="normal"); self.cancel_button.configure(state="disabled")
                if kind == "done":
                    stats = payload; self.state_badge.configure(text="●  완료", fg_color="#16372D", text_color=self.COLORS["success"])
                    self._write_log(f"완료 — 성공 {stats.success}, 실패 {stats.failed}, 취소 {stats.cancelled}")
                else:
                    self.state_badge.configure(text="●  오류", fg_color="#452536", text_color="#FF9CAD"); self._write_log(f"오류: {payload}")
        self.after(100, self._poll_events)

    def _write_log(self, message: str) -> None:
        self.log.insert("end", message + "\n"); self.log.see("end")

    def _on_close(self) -> None:
        """Stop active transfers before the GUI process exits."""
        self.cancel_event.set()
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
