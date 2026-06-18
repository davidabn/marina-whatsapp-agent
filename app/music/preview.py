"""45s audio preview — port of music-pipeline/extract-preview.py.

ffmpeg recipe (unchanged): afade in + afade out + EBU loudnorm, re-encoded to
MP3. ffprobe clamps the start when the track is shorter than start+duration.

Default start=0.0: the Suno songs in this pipeline are chorus-first (the prompts
ask for "very short intro under 3 seconds then chorus first"), so the hook sits
at the very start — there is no intro to skip, unlike the original CLI default of
15s. start is still a parameter for the rare track that needs an offset.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def make_preview(
    src: bytes | str,
    *,
    start: float = 0.0,
    duration: float = 45.0,
    fade_in: float = 1.0,
    fade_out: float = 2.0,
    bitrate: str = "192k",
) -> bytes:
    """Render a faded, loudness-normalized MP3 preview and return its bytes.

    `src` may be raw MP3 bytes or a path to an MP3. Temp files are always cleaned.
    """
    tmp_in: str | None = None
    if isinstance(src, (bytes, bytearray)):
        fd, tmp_in = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        Path(tmp_in).write_bytes(bytes(src))
        in_path = tmp_in
    else:
        in_path = str(src)

    fd, out_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    try:
        total = _probe_duration(in_path)
        if start + duration > total:
            start = max(0.0, total - duration)

        fade_out_start = max(0.0, duration - fade_out)
        af = (
            f"afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={fade_out_start}:d={fade_out},"
            f"loudnorm=I=-14:TP=-1.5:LRA=11"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}",
            "-i", in_path,
            "-t", f"{duration:.3f}",
            "-af", af,
            "-c:a", "libmp3lame", "-b:a", bitrate,
            out_path,
        ]
        subprocess.run(cmd, check=True)
        return Path(out_path).read_bytes()
    finally:
        for p in (tmp_in, out_path):
            if p and os.path.exists(p):
                os.remove(p)


def make_video_preview(
    audio: bytes | str,
    image: bytes | str,
    *,
    start: float = 0.0,
    duration: float = 45.0,
    fade_in: float = 1.0,
    fade_out: float = 2.0,
    bitrate: str = "192k",
    width: int = 720,
    height: int = 1280,
) -> bytes:
    """Render a 45s vertical (9:16) MP4 = cover art over a blurred fill of itself
    + faded, loudness-normalized audio.

    `audio`/`image` may be raw bytes or file paths. Returns the MP4 bytes. Used
    for the WhatsApp preview (a video the customer can watch, kept short so the
    full song stays the paid deliverable). Temp files are always cleaned.
    """
    cleanup: list[str] = []

    def _materialize(src: bytes | str, suffix: str) -> str:
        if isinstance(src, (bytes, bytearray)):
            fd, p = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            Path(p).write_bytes(bytes(src))
            cleanup.append(p)
            return p
        return str(src)

    audio_path = _materialize(audio, ".mp3")
    image_path = _materialize(image, ".jpg")
    fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    cleanup.append(out_path)

    try:
        total = _probe_duration(audio_path)
        if start + duration > total:
            start = max(0.0, total - duration)

        fade_out_start = max(0.0, duration - fade_out)
        # 9:16 vertical: a blurred, zoomed copy of the cover fills the frame, with
        # the (square) cover centered on top. Even dims + yuv420p for WhatsApp/iOS.
        filter_complex = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:4,setsar=1[bg];"
            f"[b]scale={width}:{width}:force_original_aspect_ratio=increase,"
            f"crop={width}:{width}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v];"
            f"[1:a]afade=t=in:st=0:d={fade_in},"
            f"afade=t=out:st={fade_out_start}:d={fade_out},"
            f"loudnorm=I=-14:TP=-1.5:LRA=11[a2]"
        )
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", image_path,
            "-ss", f"{start:.3f}", "-i", audio_path,
            "-t", f"{duration:.3f}",
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a2]",
            "-c:v", "libx264", "-tune", "stillimage", "-r", "5", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", bitrate,
            "-shortest", "-movflags", "+faststart",
            out_path,
        ]
        subprocess.run(cmd, check=True)
        return Path(out_path).read_bytes()
    finally:
        for p in cleanup:
            if p and os.path.exists(p):
                os.remove(p)
