# Luma Fetch

Luma Fetch is a Windows desktop batch downloader for image URLs that follow a predictable template. It provides an async CustomTkinter interface and is intended only for content you are permitted to download.

## Features

- Async downloads using `aiohttp` and `aiofiles`
- Multiple character codes and mixed situation expressions such as `01..50,s01..83`
- Korean template aliases such as `캐릭터`, `상황`, and `의상`
- Manual character gallery with first-image covers and a Canvas-native virtualized thumbnail grid
- Original-image viewer with fit/fill/100% modes, wheel zoom, drag panning, fullscreen, and navigation
- Split network/decode pipeline, non-blocking UI event bus, disk-backed preview cache, and lifecycle-safe cleanup
- Named favorites that restore every input; damaged entries are skipped individually without hiding valid favorites
- Referer auto/manual modes: auto silently discovers built-in chat platforms on HTTP 403; manual uses a typed origin URL
- Version browser with scrollable GitHub release history for upgrade, reinstall, or downgrade
- Optional character subfolders, retries, cancellation, Microsoft Defender scanning, and update verification
- Automatic cleanup of stale `.part` files after interrupted downloads

## Run from source

Requires Python 3.12 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Template and range examples

```text
https://example.org/images/{char}/{situation}.webp
https://example.org/images/캐릭터/상황.webp
```

Situation values are comma-separated and may mix literals, numeric ranges, and prefixed ranges:

```text
01..50,s01..83
s01..s83,bonus
0001..0500,1001..1420
```

## Test

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

## Build the Windows installer

On Windows x64 with Python 3.12 or later, run the root build command:

```powershell
.\BUILD_RELEASE.cmd
```

The command creates an isolated build environment, installs required build packages, runs tests, builds the application with PyInstaller, compiles the Inno Setup installer, and writes SHA-256/build metadata. The repository's `installer/LumaFetch.ico` must be present.

Generated files:

```text
outputs\LumaFetch-Setup-1.13.2.exe
outputs\SHA256SUMS.txt
outputs\BUILD_INFO.txt
```

The GitHub Actions workflow performs the same Windows build and uploads the installer as an artifact. Build artifacts are deliberately not committed.

## Security and distribution

- Only public HTTPS image URLs with approved image extensions are accepted.
- MIME type, image signature, and a 30 MiB image limit are enforced.
- The updater accepts only the versioned installer from this repository, verifies GitHub's SHA-256 digest and size, and then launches Inno Setup.
- Do not use the app to download content without permission from its owner or host.

## License

No license has been selected yet. Do not reuse or redistribute the source beyond the repository owner's intended terms until a license is added.
