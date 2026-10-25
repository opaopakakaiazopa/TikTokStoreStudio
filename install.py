"""Prepare the local environment and portable FFmpeg tools."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def install_ffmpeg():
    target = ROOT / "tools" / "bin"
    target.mkdir(parents=True, exist_ok=True)
    if all((target / f"{name}.exe").exists() or shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        return
    archive = ROOT / "tools" / "ffmpeg.zip.download"
    print("Downloading FFmpeg from gyan.dev (portable Windows build)...", flush=True)
    request = urllib.request.Request(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        headers={"User-Agent": "TikTokStoreStudio/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as source, archive.open("wb") as dest:
            shutil.copyfileobj(source, dest, 1024 * 1024)
        with zipfile.ZipFile(archive) as bundle:
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                member = next(item for item in bundle.infolist() if item.filename.endswith("/bin/" + name))
                temporary = target / (name + ".download")
                with bundle.open(member) as source, temporary.open("wb") as dest:
                    shutil.copyfileobj(source, dest)
                os.replace(temporary, target / name)
        print("FFmpeg ready.", flush=True)
    finally:
        archive.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    if "--ffmpeg-only" not in sys.argv:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    install_ffmpeg()
