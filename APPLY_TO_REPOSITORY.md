# Apply and build Luma Fetch 1.13.0

This archive contains the completed Luma Fetch 1.13.0 source overlay and a one-command Windows release build.

## Important existing asset

The archive intentionally does not replace the repository's binary icon. Keep this file from the existing repository:

```text
installer/LumaFetch.ico
```

## Apply

1. Clone or open `https://github.com/scarlel96-design/LumaFetch` on Windows x64.
2. Copy the archive contents over the repository root.
3. Confirm that `installer/LumaFetch.ico` still exists.
4. Double-click `BUILD_RELEASE.cmd` or run it from a terminal.

The command automatically prepares an isolated `.venv-build`, installs Python build dependencies, detects or installs Inno Setup 6, runs all tests, builds with PyInstaller, compiles the Inno Setup installer, and verifies versions and SHA-256.

## Expected outputs

```text
outputs\LumaFetch-Setup-1.13.0.exe
outputs\SHA256SUMS.txt
outputs\BUILD_INFO.txt
```

Do not publish a GitHub Release until the generated installer has been installed and manually smoke-tested on Windows 10 or Windows 11.
