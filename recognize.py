"""Identify a song from a short clip (voice message, video, video note or audio file) with Shazam's
public recognition service via the `shazamio` library. Needs ffmpeg on the server."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

from core import log

CLIP_SECONDS = 25
MAX_BYTES = 20 * 1024 * 1024  # Telegram bots can download up to 20 MB


def available() -> bool:
    try:
        import shazamio  # noqa: F401
    except Exception:
        return False
    return shutil.which("ffmpeg") is not None


async def _to_wav(src: str, dst: str) -> bool:
    """Mono 16 kHz wav of the loudest-looking part: skip 0 s, keep CLIP_SECONDS."""
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vn", "-t", str(CLIP_SECONDS), "-ac", "1", "-ar", "16000", dst,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    if p.returncode != 0:
        log.warning("ffmpeg failed: %s", err.decode(errors="ignore")[-200:])
    return p.returncode == 0


def parse(result: dict | None) -> dict | None:
    t = (result or {}).get("track")
    if not t or not t.get("title"):
        return None
    return {"title": t["title"], "artist": t.get("subtitle") or "", "url": t.get("url"),
            "cover": (t.get("images") or {}).get("coverart")}


async def identify_file(path: str) -> dict | None:
    from shazamio import Shazam
    tmp = tempfile.mkdtemp(prefix="telspot_rec_")
    try:
        wav = os.path.join(tmp, "clip.wav")
        if not await _to_wav(path, wav):
            raise RuntimeError("could not read that audio")
        return parse(await asyncio.wait_for(Shazam().recognize(wav), 60))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def identify(bot, file_id: str, size: int = 0, suffix: str = ".ogg") -> dict | None:
    if size and size > MAX_BYTES:
        raise ValueError("too big")
    tmp = tempfile.mkdtemp(prefix="telspot_dl_")
    try:
        path = os.path.join(tmp, "in" + suffix)
        await bot.download(file_id, destination=path)
        return await identify_file(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
