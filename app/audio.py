import asyncio

import structlog

log = structlog.get_logger()

FFMPEG_TIMEOUT_SECONDS = 30


async def transcode_to_pcm(audio_bytes: bytes) -> bytes:
    """Convert any audio format to raw PCM 16kHz 16-bit mono via ffmpeg.

    Args:
        audio_bytes: Raw audio file bytes (any format ffmpeg supports).

    Returns:
        PCM audio bytes (16kHz, 16-bit, mono, little-endian).

    Raises:
        RuntimeError: If ffmpeg fails or times out.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "s16le",
        "-ar", "16000",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=audio_bytes),
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        log.error("ffmpeg.timeout", timeout=FFMPEG_TIMEOUT_SECONDS)
        raise RuntimeError("Audio transcoding timed out") from e
    if proc.returncode != 0:
        log.error("ffmpeg.failed", stderr=stderr.decode(errors="replace"))
        raise RuntimeError("Audio transcoding failed")
    return stdout
