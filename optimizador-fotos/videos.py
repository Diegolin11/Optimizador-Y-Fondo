"""
Parte de videos del optimizador. Usa ffmpeg (viene con el paquete imageio-ffmpeg,
o el que tengas instalado en el sistema).

Cada video se endereza, se reduce, se limita a 30 cuadros por segundo, se pasa a
MP4 (H.264, se reproduce en cualquier navegador y celular), se le quitan los
metadatos (incluido el GPS) y queda listo para empezar a verse antes de terminar
de descargarse (faststart). Los videos HDR de iPhone se convierten a color normal
para que no se vean deslavados.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

EXT_VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".wmv", ".mts", ".m2ts"}

# Sin ventana negra de consola en Windows cuando se abre con pythonw
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


@dataclass
class PerfilVideo:
    lado: int            # lado más largo en px (0 = no cambiar)
    calidad: str         # "Alta", "Normal" o "Ligera"
    audio: bool          # False = se quita el sonido
    fps: int = 30        # máximo de cuadros por segundo (0 = no cambiar)
    max_mb: int = 0      # 0 = sin límite
    poster: bool = False  # sacar la "imagen de espera" (primer cuadro) en WebP


# CRF de x264: más bajo = más calidad y más peso. 23 es el estándar.
CRF = {"Alta": 21, "Normal": 25, "Ligera": 29}
CALIDADES = list(CRF)

PERFILES_VIDEO: dict[str, PerfilVideo] = {
    "Vivero · carrusel (1920 px, sin audio, con imagen de espera)": PerfilVideo(1920, "Normal", False, 30, 0, True),
    "Web y redes (1280 px, con audio)": PerfilVideo(1280, "Normal", True, 30),
    "Muy ligero (720 px, con audio)": PerfilVideo(720, "Ligera", True, 30),
    "Mismo tamaño, solo bajar el peso": PerfilVideo(0, "Normal", True, 0),
}
ATAJOS_VIDEO = {
    "carrusel": "Vivero · carrusel (1920 px, sin audio, con imagen de espera)",
    "web": "Web y redes (1280 px, con audio)",
    "ligero": "Muy ligero (720 px, con audio)",
    "mismo": "Mismo tamaño, solo bajar el peso",
}


class Cancelado(Exception):
    pass


class FaltaFfmpeg(RuntimeError):
    pass


_ffmpeg: str | None = None
_activos: set = set()  # procesos de ffmpeg corriendo, para poder cerrarlos al salir


def detener_todo() -> None:
    for proc in list(_activos):
        try:
            proc.kill()
        except Exception:
            pass
_filtros: set[str] | None = None


def ruta_ffmpeg() -> str:
    global _ffmpeg
    if _ffmpeg:
        return _ffmpeg
    try:
        import imageio_ffmpeg
        _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _ffmpeg = shutil.which("ffmpeg")
    if not _ffmpeg:
        raise FaltaFfmpeg("falta ffmpeg (pip install imageio-ffmpeg)")
    return _ffmpeg


def hay_ffmpeg() -> bool:
    try:
        ruta_ffmpeg()
        return True
    except FaltaFfmpeg:
        return False


def tiene_filtro(nombre: str) -> bool:
    global _filtros
    if _filtros is None:
        r = subprocess.run([ruta_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=SIN_VENTANA)
        _filtros = {l.split()[1] for l in r.stdout.splitlines() if len(l.split()) > 2}
    return nombre in _filtros


@dataclass
class InfoVideo:
    duracion: float
    ancho: int
    alto: int
    fps: float
    codec: str
    audio: bool
    hdr: bool


def leer_info(ruta: Path) -> InfoVideo:
    """Lee duración, medidas (ya giradas), fps, códec, si tiene audio y si es HDR."""
    r = subprocess.run([ruta_ffmpeg(), "-hide_banner", "-i", str(ruta)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=SIN_VENTANA)
    t = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", t)
    if not m:
        raise RuntimeError("no parece un video válido")
    duracion = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    linea = next((l for l in t.splitlines() if re.search(r"Stream #.*Video:", l)), "")
    if not linea:
        raise RuntimeError("el archivo no tiene video")
    codec = re.search(r"Video: (\w+)", linea)[1]
    medidas = re.search(r", (\d{2,5})x(\d{2,5})", linea)
    ancho, alto = (int(medidas[1]), int(medidas[2])) if medidas else (0, 0)
    fps_m = re.search(r"([\d.]+) fps", linea) or re.search(r"([\d.]+) tbr", linea)
    fps = float(fps_m[1]) if fps_m else 0.0
    giro = re.search(r"rotation of (-?[\d.]+)", t)
    if giro and abs(round(float(giro[1]))) % 180 == 90:
        ancho, alto = alto, ancho
    return InfoVideo(duracion, ancho, alto, fps, codec, bool(re.search(r"Stream #.*Audio:", t)),
                     bool(re.search(r"arib-std-b67|smpte2084", linea)))


def _correr(cmd: list[str], duracion: float, progreso: Callable[[float], None] | None,
            cancelar: threading.Event | None) -> None:
    """Corre ffmpeg leyendo su avance; si piden cancelar, lo detiene."""
    proc = subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats", "-loglevel", "error"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=SIN_VENTANA)
    _activos.add(proc)
    errores: list[str] = []
    lector = threading.Thread(target=lambda: errores.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
    lector.start()
    assert proc.stdout
    try:
        for linea in proc.stdout:
            if cancelar is not None and cancelar.is_set():
                proc.kill()
                proc.wait()
                raise Cancelado()
            if linea.startswith("out_time_us=") and progreso and duracion > 0:
                try:
                    progreso(min(0.99, int(linea.split("=")[1]) / 1e6 / duracion))
                except ValueError:
                    pass
        proc.wait()
    finally:
        _activos.discard(proc)
    lector.join(timeout=2)
    if cancelar is not None and cancelar.is_set():
        raise Cancelado()
    if proc.returncode != 0:
        detalle = "".join(errores).strip().splitlines()
        raise RuntimeError(detalle[-1] if detalle else f"ffmpeg terminó con error {proc.returncode}")


def _filtros_video(info: InfoVideo, perfil: PerfilVideo) -> str:
    cadena: list[str] = []
    if info.hdr and tiene_filtro("zscale") and tiene_filtro("tonemap"):
        cadena += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                   "tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    if perfil.lado and max(info.ancho, info.alto) > perfil.lado:
        L = perfil.lado
        cadena.append(f"scale=w='min({L},iw)':h='min({L},ih)':force_original_aspect_ratio=decrease"
                      ":force_divisible_by=2:flags=lanczos")
    else:
        cadena.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # H.264 pide medidas pares
    if perfil.fps and info.fps > perfil.fps + 0.5:
        cadena.append(f"fps={perfil.fps}")
    cadena.append("format=yuv420p")
    return ",".join(cadena)


def _comando(entrada: Path, salida: Path, info: InfoVideo, perfil: PerfilVideo,
             bitrate_k: int = 0) -> list[str]:
    cmd = [ruta_ffmpeg(), "-hide_banner", "-y", "-i", str(entrada), "-map", "0:v:0"]
    con_audio = perfil.audio and info.audio
    if con_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    else:
        cmd += ["-an"]
    preset = "medium" if perfil.calidad == "Alta" else "veryfast"
    cmd += ["-vf", _filtros_video(info, perfil), "-c:v", "libx264", "-preset", preset, "-profile:v", "high"]
    if bitrate_k:
        cmd += ["-b:v", f"{bitrate_k}k", "-maxrate", f"{int(bitrate_k * 1.3)}k", "-bufsize", f"{bitrate_k * 2}k"]
    else:
        cmd += ["-crf", str(CRF[perfil.calidad])]
    cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart", str(salida)]
    return cmd


@dataclass
class ResultadoVideo:
    antes: int
    despues: int
    medidas: tuple[int, int]
    duracion: float
    poster: Path | None = None
    copiado: bool = False


def guardar_poster(video: Path, destino: Path, calidad: int = 85) -> Path:
    """Saca el primer cuadro del video ya optimizado y lo guarda en WebP."""
    from PIL import Image
    r = subprocess.run([ruta_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(video),
                        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True, creationflags=SIN_VENTANA)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("no se pudo sacar la imagen de espera")
    with Image.open(io.BytesIO(r.stdout)) as img:
        img.convert("RGB").save(destino, "WEBP", quality=calidad, method=6)
    return destino


def optimizar_video(entrada: Path, salida: Path, perfil: PerfilVideo,
                    progreso: Callable[[float], None] | None = None,
                    cancelar: threading.Event | None = None) -> ResultadoVideo:
    antes = entrada.stat().st_size
    info = leer_info(entrada)
    salida.parent.mkdir(parents=True, exist_ok=True)
    temporal = salida.with_name(salida.stem + ".parcial.mp4")
    try:
        _correr(_comando(entrada, temporal, info, perfil), info.duracion, progreso, cancelar)

        # Peso máximo: si con la calidad elegida no cupo, se repite calculando el bitrate exacto
        if perfil.max_mb and temporal.stat().st_size > perfil.max_mb * 1024 * 1024 and info.duracion > 0:
            audio_k = 128 if (perfil.audio and info.audio) else 0
            for margen in (0.92, 0.80, 0.65):
                total_k = perfil.max_mb * 8 * 1024 * margen / info.duracion
                video_k = max(150, int(total_k - audio_k))
                _correr(_comando(entrada, temporal, info, perfil, video_k), info.duracion, progreso, cancelar)
                if temporal.stat().st_size <= perfil.max_mb * 1024 * 1024:
                    break

        copiado = False
        # Si salió más pesado que el original y el original ya es H.264, se reempaqueta sin
        # recomprimir: misma imagen, pero sin metadatos y listo para web
        cambia_tamano = bool(perfil.lado) and max(info.ancho, info.alto) > perfil.lado
        cambia_fps = bool(perfil.fps) and info.fps > perfil.fps + 0.5
        if temporal.stat().st_size >= antes and info.codec == "h264" and not (cambia_tamano or cambia_fps or info.hdr):
            copia = salida.with_name(salida.stem + ".copia.mp4")
            cmd = [ruta_ffmpeg(), "-hide_banner", "-y", "-i", str(entrada), "-map", "0:v:0"]
            cmd += ["-map", "0:a:0?", "-c", "copy"] if perfil.audio else ["-an", "-c", "copy"]
            cmd += ["-map_metadata", "-1", "-movflags", "+faststart", str(copia)]
            try:
                _correr(cmd, info.duracion, None, cancelar)
                if copia.stat().st_size < temporal.stat().st_size:
                    copia.replace(temporal)
                    copiado = True
            finally:
                copia.unlink(missing_ok=True)

        temporal.replace(salida)
    finally:
        temporal.unlink(missing_ok=True)

    final = leer_info(salida)
    poster = None
    if perfil.poster:
        poster = guardar_poster(salida, salida.with_name(salida.stem + "-espera.webp"))
    if progreso:
        progreso(1.0)
    return ResultadoVideo(antes, salida.stat().st_size, (final.ancho, final.alto), final.duracion, poster, copiado)


def duracion_legible(s: float) -> str:
    s = round(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
