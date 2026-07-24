"""Verify Referer hotlink-bypass behavior for Luma Fetch."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

try:
    import customtkinter  # noqa: F401
except ModuleNotFoundError:
    fake = types.ModuleType("customtkinter")
    fake.CTkFrame = type("CTkFrame", (), {})
    fake.CTk = type("CTk", (), {})
    fake.CTkBaseClass = object
    fake.CTkFont = object
    sys.modules["customtkinter"] = fake

from app import (
    AUTO_REFERER_CANDIDATES,
    AUTO_REFERER_PLATFORMS,
    DownloadConfig,
    FavoritePreset,
    ImageRequestPolicy,
    make_request_headers,
    platform_label_for_referer,
)


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

EXPECTED_PLATFORMS = {
    ("BabeChat", "https://babechat.ai/"),
    ("Crack", "https://crack.wrtn.ai/"),
    ("Elyn", "https://elyn.ai/"),
    ("CAVEDUCK", "https://caveduck.io/"),
    ("EdenChat", "https://eden-chat.com/"),
    ("LUNATALK", "https://lunatalk.chat/"),
    ("Teapot", "https://teapotchat.com/"),
    ("ChuuChat", "https://chuu.ai/"),
    ("BoriChat", "https://bori.chat/"),
}


class HotlinkServer:
    """Simulates a CDN that only serves images with an approved Referer."""

    def __init__(self, *, allowed_referers: set[str | None]) -> None:
        self.allowed_referers = allowed_referers
        self.requests: list[dict[str, str | None]] = []
        self.runner: web.AppRunner | None = None
        self.base_url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/img/{name}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        port = sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        referer = request.headers.get("Referer")
        self.requests.append(
            {
                "path": request.path,
                "referer": referer,
                "user_agent": request.headers.get("User-Agent"),
                "accept": request.headers.get("Accept"),
                "sec_fetch_dest": request.headers.get("Sec-Fetch-Dest"),
            }
        )
        if referer not in self.allowed_referers:
            return web.Response(status=403, text="hotlink denied")
        return web.Response(body=PNG_1X1, content_type="image/png")


def test_builtin_platform_list_matches_product_spec() -> None:
    assert set(AUTO_REFERER_PLATFORMS) == EXPECTED_PLATFORMS
    assert AUTO_REFERER_CANDIDATES == tuple(url for _name, url in AUTO_REFERER_PLATFORMS)
    assert platform_label_for_referer("https://elyn.ai/") == "Elyn"


def test_make_request_headers_includes_browser_like_fields() -> None:
    headers = make_request_headers("https://babechat.ai/")
    assert headers["Referer"] == "https://babechat.ai/"
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Sec-Fetch-Dest"] == "image"
    assert "image/" in headers["Accept"]


def test_make_request_headers_omits_referer_when_absent() -> None:
    headers = make_request_headers(None)
    assert "Referer" not in headers


def test_explicit_referer_is_sent_and_unlocks_hotlink() -> None:
    async def scenario() -> None:
        server = HotlinkServer(allowed_referers={"https://platform.example/"})
        await server.start()
        try:
            policy = ImageRequestPolicy("https://platform.example/")
            async with ClientSession(headers=make_request_headers(None)) as session:
                response = await policy.get(session, f"{server.base_url}/img/a.png")
                async with response:
                    body = await response.read()
                    assert response.status == 200
                    assert body.startswith(b"\x89PNG")
            assert server.requests[0]["referer"] == "https://platform.example/"
            assert server.requests[0]["user_agent"] and "Mozilla" in server.requests[0]["user_agent"]
            assert server.requests[0]["sec_fetch_dest"] == "image"
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_explicit_referer_wrong_value_stays_forbidden() -> None:
    async def scenario() -> None:
        server = HotlinkServer(allowed_referers={"https://platform.example/"})
        await server.start()
        try:
            policy = ImageRequestPolicy("https://wrong.example/")
            async with ClientSession(headers=make_request_headers(None)) as session:
                response = await policy.get(session, f"{server.base_url}/img/a.png")
                async with response:
                    assert response.status == 403
            assert len(server.requests) == 1
            assert server.requests[0]["referer"] == "https://wrong.example/"
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_auto_referer_parallel_probe_finds_non_first_platform() -> None:
    async def scenario() -> None:
        # Only mid-list platform is allowed — forces a real multi-candidate probe.
        allowed = "https://elyn.ai/"
        server = HotlinkServer(allowed_referers={allowed})
        await server.start()
        detected: list[tuple[str, str]] = []
        policy = ImageRequestPolicy(None, on_detected=lambda h, r: detected.append((h, r)))
        try:
            async with ClientSession(headers=make_request_headers(None)) as session:
                response = await policy.get(session, f"{server.base_url}/img/one.png")
                async with response:
                    assert response.status == 200
                    assert await response.read() == PNG_1X1

                response = await policy.get(session, f"{server.base_url}/img/two.png")
                async with response:
                    assert response.status == 200

            assert server.requests[0]["referer"] is None
            probe_referers = {req["referer"] for req in server.requests[1:-1]}
            assert allowed in probe_referers
            assert server.requests[-1]["referer"] == allowed
            assert detected == [("127.0.0.1", allowed)]
            assert policy._resolved["127.0.0.1"] == allowed
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_auto_referer_keeps_none_when_open_cdn() -> None:
    async def scenario() -> None:
        server = HotlinkServer(allowed_referers={None, "https://babechat.ai/"})
        await server.start()
        detected: list[tuple[str, str]] = []
        policy = ImageRequestPolicy(None, on_detected=lambda h, r: detected.append((h, r)))
        try:
            async with ClientSession(headers=make_request_headers(None)) as session:
                response = await policy.get(session, f"{server.base_url}/img/open.png")
                async with response:
                    assert response.status == 200
                response = await policy.get(session, f"{server.base_url}/img/open2.png")
                async with response:
                    assert response.status == 200
            assert [req["referer"] for req in server.requests] == [None, None]
            assert policy._resolved["127.0.0.1"] is None
            assert detected == []
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_auto_referer_concurrent_first_hits_use_single_probe() -> None:
    async def scenario() -> None:
        allowed = "https://caveduck.io/"
        server = HotlinkServer(allowed_referers={allowed})
        await server.start()
        policy = ImageRequestPolicy(None)
        try:
            async with ClientSession(headers=make_request_headers(None)) as session:
                results = await asyncio.gather(
                    *(
                        policy.get(session, f"{server.base_url}/img/{index}.png")
                        for index in range(8)
                    )
                )
                for response in results:
                    async with response:
                        assert response.status == 200
                        assert await response.read() == PNG_1X1

            no_ref = sum(1 for req in server.requests if req["referer"] is None)
            with_ref = sum(1 for req in server.requests if req["referer"] == allowed)
            assert no_ref == 1
            # One successful probe response + 7 cached follow-ups = 8
            assert with_ref >= 8
            assert policy._resolved["127.0.0.1"] == allowed
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_download_config_auto_mode_ignores_typed_referer_for_policy(tmp_path: Path) -> None:
    config = DownloadConfig(
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A",
        ranges="01",
        destination=tmp_path,
        referer_mode="auto",
        referer="https://should-not-be-used.example/",
    )
    assert config.explicit_referer() is None


def test_download_config_manual_mode_uses_typed_referer(tmp_path: Path) -> None:
    config = DownloadConfig(
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A",
        ranges="01",
        destination=tmp_path,
        referer_mode="manual",
        referer="babechat.ai/",
    )
    assert config.explicit_referer() == "https://babechat.ai/"


def test_download_config_normalizes_referer_without_scheme(tmp_path: Path) -> None:
    config = DownloadConfig(
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A",
        ranges="01",
        destination=tmp_path,
        referer_mode="manual",
        referer="si-ran.com/",
    )
    assert config.referer == "https://si-ran.com/"


def test_download_config_rejects_http_referer(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        DownloadConfig(
            template_url="https://cdn.example.com/{char}/{situation}.webp",
            character="A",
            ranges="01",
            destination=tmp_path,
            referer_mode="manual",
            referer="http://insecure.example/",
        )


def test_download_config_blank_referer_becomes_none(tmp_path: Path) -> None:
    config = DownloadConfig(
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A",
        ranges="01",
        destination=tmp_path,
        referer="   ",
    )
    assert config.referer is None
    assert config.referer_mode == "auto"
    assert config.explicit_referer() is None


def test_favorites_roundtrip_preserves_referer_mode() -> None:
    favorite = FavoritePreset(
        name="hotlink",
        template_url="https://cdn.example.com/{char}/{situation}.webp",
        character="A",
        ranges="01..02",
        referer_mode="manual",
        referer="https://babechat.ai/",
    )
    restored = FavoritePreset.model_validate(favorite.model_dump(mode="json"))
    assert restored.referer_mode == "manual"
    assert restored.referer == "https://babechat.ai/"


def test_legacy_favorite_with_referer_opens_as_manual() -> None:
    restored = FavoritePreset.model_validate(
        {
            "name": "old",
            "template_url": "https://cdn.example.com/{char}/{situation}.webp",
            "character": "A",
            "ranges": "01",
            "referer": "https://babechat.ai/",
        }
    )
    assert restored.referer_mode == "manual"


def test_legacy_favorite_without_referer_opens_as_auto() -> None:
    restored = FavoritePreset.model_validate(
        {
            "name": "old-auto",
            "template_url": "https://cdn.example.com/{char}/{situation}.webp",
            "character": "A",
            "ranges": "01",
        }
    )
    assert restored.referer_mode == "auto"
