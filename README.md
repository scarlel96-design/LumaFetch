# Luma Fetch

Luma Fetch is a Windows desktop batch downloader for image URLs that follow a predictable template. It provides an async CustomTkinter interface and is intended only for content you are permitted to download.

## Features

- Async downloads using `aiohttp` and `aiofiles`
- Multiple character codes and numeric situation ranges
- Korean template aliases such as `캐릭터`, `상황`, and `의상`
- Manual character gallery with first-image covers and an uncapped, virtualized thumbnail grid
- Large original-image viewer with mouse selection and Left/Right keyboard navigation
- Off-thread thumbnail decoding, bounded queues, disk-backed preview cache, and automatic cache cleanup
- Optional Referer header support for hosts that restrict cross-site images
- GitHub Releases update checks with in-app installer download and SHA-256 verification
- Optional character subfolders, retry handling, cancellation, progress, and error summaries
- HTTPS/public-network/image-type validation and bounded download size
- Optional Microsoft Defender scan request

## Run from source

Requires Python 3.12 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Template examples

Use either token style below. Values entered in character code keep their original letter case.

```text
https://example.org/images/{char}/{situation}.webp
https://example.org/images/캐릭터/상황.webp
```

Situation ranges accept comma-separated values such as `0001..0500, 1001..1420`.

## Build a Windows installer

Install PyInstaller and Inno Setup 6, then run from PowerShell:

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name LumaFetch --icon installer\LumaFetch.ico --add-data "installer\LumaFetch.ico;." app.py
Copy-Item dist\LumaFetch.exe installer\LumaFetch.exe -Force
ISCC installer\LumaFetch.iss
```

The generated installer is placed in `outputs/` by the Inno Setup script. Build artifacts are deliberately not committed.

## Security and distribution

- The app accepts only public HTTPS image URLs with approved image extensions.
- It checks response MIME type, basic image signatures, and a 30 MiB maximum file size.
- The updater accepts only the versioned installer asset from this repository's GitHub Release, requires GitHub's SHA-256 asset digest, verifies size and hash, and only then starts the installer.
- Release binaries should be published with a SHA-256 hash. A third-party detection result is not, by itself, a proof that a file is safe or malicious.
- Do not use the app to download content without permission from its owner or host.

## License

No license has been selected yet. Do not reuse or redistribute the source beyond the repository owner's intended terms until a license is added.
