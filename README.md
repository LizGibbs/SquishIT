# SquishIT

Standalone Windows batch compressor for images, video and PDF files across large directory trees.

No Python required. No installation. Just extract and run.

---

## Download

Go to [Releases](https://github.com/LizGibbs/SquishIT/releases) and download the latest zip.

---

## Installation

1. Download `SquishIT-v1.0-windows.zip` from the [latest release](https://github.com/LizGibbs/SquishIT/releases/latest)
2. Extract to any folder (e.g. `C:\Tools\SquishIT\`)
3. Double-click `SquishIT.exe`
4. On first run, the app will ask to download **Ghostscript** (~30 MB) for PDF compression — click Yes
5. Done — all features are ready

> **FFmpeg** (video compression) is included in the zip. Only Ghostscript downloads on first run.

---

## What it compresses

| Type | Formats |
|------|---------|
| Images | JPG, JPEG, PNG, TIFF, BMP, WebP |
| Video | MP4, MKV, AVI, MOV, WMV, FLV, M4V, MPEG, MPG, 3GP, TS |
| PDF | All PDF files via Ghostscript |

---

## How to use

1. **Select Folders tab** — Browse to a root folder. The tree shows all subfolders. Click to check/uncheck folders. Tri-state checkboxes let you include partial trees.
2. **Settings tab** — Set image quality, PDF preset, video codec (H.264 or H.265) and CRF. Configure overwrite behaviour, minimum saving threshold, and temp folder for phone/MTP storage.
3. **Compress tab** — Click **Go Ahead**. Watch live progress, current file, and running stats. Pause or cancel at any time.
4. **Report tab** — Populates live as files complete. Shows original size, compressed size, and saving per file. Export as `.txt` or `.csv`.

---

## FAQ

### Does it overwrite the original files?

Yes — but you control when. Three modes in Settings:

- **Ask each time** — a dialog appears before each overwrite, with a *Yes to all* button to stop asking
- **Yes to all** — overwrites without asking
- **Dry run** — scans and reports what *would* happen, no files changed

### What if compression makes a file larger?

It won't overwrite. By default, files must shrink by at least **5%** to be replaced. You can adjust this threshold (0–30%) in Settings. Files that don't meet the threshold are logged as `skipped-no-gain` and skipped on future runs too.

### Does it keep the original video container format?

Yes. A `.mkv` stays `.mkv`, `.avi` stays `.avi`, etc. Only the codec inside changes (H.264 or H.265). Subtitles, multiple audio tracks and chapters are preserved.

> **Note:** AVI + H.265 is not supported — the app automatically falls back to H.264 for AVI files and logs a warning.

### Does it preserve photo metadata (EXIF)?

Yes. GPS location, timestamps, camera settings and other EXIF data are preserved for JPEG and WebP files.

### Can I pause and resume a run?

Yes. Click **Pause** — the app finishes the current file and stops. You can close the app safely at that point. When you reopen it, click **Resume previous run** to continue from where it left off. Settings from the original run are restored automatically.

### Will it re-compress already-compressed files on the next run?

No. Each compressed file is recorded in a local database (`squishit_db.json`). On subsequent runs, files whose size matches the database record are skipped automatically (shown as `db-skip` in the report, in blue).

### Does it work with phones connected via USB?

Yes, with a caveat. Put the phone in **File Transfer (MTP)** mode. Enable **Use temp folder** in Settings and point it to a local drive. The app writes the compressed file to the local temp folder first, then copies it back to the phone. This avoids MTP write failures that happen when compressing directly to phone storage.

### What if a file disappears during a run?

The app handles it gracefully. If a file is moved, deleted or becomes inaccessible mid-run, it logs `GONE` and continues with the next file — no crash.

### Is the app portable?

Yes. The entire working folder is self-contained. Copy it to a USB drive or another PC and it works. Runtime files (`squishit_db.json`, `squishit_settings.json`, logs) are created next to the exe.

### What are the system requirements?

- Windows 10 or later (64-bit)
- No Python, no .NET, no other installs required
- Internet connection on first run (to download Ghostscript ~30 MB)

---

## Settings reference

| Setting | Default | Description |
|---------|---------|-------------|
| Image quality | 85% | JPEG/WebP quality. Lower = smaller file, more visible loss |
| PDF preset | ebook | screen (72 dpi) · ebook (150 dpi) · printer (300 dpi) · prepress (300 dpi + colour) |
| Video codec | H.264 | H.264 = broad compatibility · H.265 = ~50% smaller, slower to encode |
| Video CRF | 23 | 18 = visually lossless · 28 = noticeable quality loss |
| Min saving | 5% | Files that compress by less than this are skipped |
| Overwrite mode | Ask | Ask · Yes to all · Dry run |
| Throttle delay | Off | Milliseconds to wait between files — reduces PC interference |
| Temp folder | Off | Write temp files locally before copying back (use for MTP/phone storage) |
| Scan threshold | 100 MB | Folders with less than this much new content are auto-unchecked after a badge scan |

---

## Building from source

Requirements: Python 3.10+, FFmpeg in `tools\`, Ghostscript installed system-wide.

```
install.bat    # first-time setup: creates venv, installs dependencies
run.bat        # run from source (no build needed)
build_exe.bat  # build dist\SquishIT.exe with PyInstaller
```

---

## Licence

MIT
