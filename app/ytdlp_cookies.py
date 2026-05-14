import os
import shutil

from app.config import YTDLP_COOKIES_FILE


def _has_cookie_rows(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                text = line.strip()
                if text and not text.startswith("#"):
                    return True
    except OSError:
        return False
    return False


def prepare_ytdlp_cookiefile(runtime_dir, source_path=None):
    source = str(source_path or YTDLP_COOKIES_FILE or "").strip()
    if not source or not os.path.isfile(source):
        return None
    try:
        if os.path.getsize(source) <= 0:
            return None
    except OSError:
        return None
    if not _has_cookie_rows(source):
        return None
    target_dir = str(runtime_dir or "").strip()
    if not target_dir:
        return source
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, "yt-dlp.cookies.txt")
    if os.path.abspath(source) != os.path.abspath(target):
        try:
            shutil.copy2(source, target)
        except OSError:
            return None
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
    return target
