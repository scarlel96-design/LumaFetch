# Security notes

Luma Fetch is designed to download raster image files from user-provided public HTTPS URLs. It rejects local/private network targets, non-image extensions, unexpected image content types, invalid basic image signatures, and files larger than 30 MiB.

If you find a security issue, do not publish exploit details in an issue. Contact the repository owner privately with a minimal reproduction and the affected version.

Windows SmartScreen and antivirus products can flag unsigned PyInstaller-based executables heuristically. Treat every alert seriously, verify the source and release SHA-256 value, and submit suspected false positives to the detecting vendor.
