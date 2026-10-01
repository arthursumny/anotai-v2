"""AnotAI — barreira de audibilidade: silêncio nem chega ao Whisper.

Usa `ffmpeg -af volumedetect` (já instalado na imagem) para medir o volume
máximo de qualquer formato aceito (wav, webm, m4a, opus...). Barato, local,
sem custo de API.
"""

import re
import shutil
import subprocess
from pathlib import Path

MIN_BYTES = 1024
MIN_DB = -50.0  # max_volume abaixo disso = mudo na prática
_INSPECT_TIMEOUT = 30

_VOLUME_RE = re.compile(r"max_volume:\s*(-inf|-\d+(?:\.\d+)?)\s*dB")


def check_audible(path: Path) -> tuple[bool, str]:
    """Retorna `(ok, motivo)`. `ok=False` → rejeitar antes do Whisper."""
    try:
        size = path.stat().st_size
    except OSError:
        return False, "arquivo de áudio não encontrado"
    if size < MIN_BYTES:
        return False, f"arquivo de áudio muito pequeno ({size} bytes) — sem som aproveitável"

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return True, "ffmpeg ausente; verificação de silêncio ignorada"

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=_INSPECT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return True, f"falha ao inspecionar áudio ({exc}); seguindo sem bloqueio"

    match = _VOLUME_RE.search(proc.stderr)
    if not match:
        return True, "volume indeterminado; seguindo sem bloqueio"
    value = match.group(1)
    if value == "-inf":
        return False, "áudio em silêncio total — nada enviado ao Whisper"
    if float(value) < MIN_DB:
        return False, f"áudio praticamente mudo (pico {value} dB) — nada enviado ao Whisper"
    return True, "ok"
