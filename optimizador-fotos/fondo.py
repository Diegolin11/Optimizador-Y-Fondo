"""
Parte de "quitar fondo" del optimizador. Usa rembg (inteligencia artificial que
corre en tu compu, sin internet después de la primera vez).

Orden del proceso (importa):
  1. Se abre la foto ORIGINAL, se endereza y se pasa a sRGB.
  2. Se le quita el fondo con la foto en buena resolución (así las orillas salen finas).
  3. Se limpian manchas sueltas que el modelo haya dejado lejos de la planta.
  4. Se recorta, se centra en un cuadro con margen parejo y se pone el fondo.
  5. AL FINAL se reduce al tamaño de salida y se comprime en WebP.
Si se comprimiera primero, el modelo trabajaría sobre una foto más chica y con
artefactos de compresión, y las orillas de las hojas saldrían peor.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

# Dos modelos: ninguno gana siempre (ISNet es mejor con hojas finas tipo romero;
# U2Net respeta mejor macetas oscuras). Por eso se puede elegir en la vista previa.
MODELOS = {
    "isnet": ("isnet-general-use", "ISNet (hojas finas)"),
    "u2net": ("u2net", "U2Net (macetas)"),
}
LADO_MINIMO = 600     # las cards del sitio miden 600 px
LADO_ENTRADA = 2400  # se trabaja con la foto a este tamaño máximo (rápido y con buen detalle)

FONDOS = {
    "Blanco": (255, 255, 255, 255),
    "Transparente (toma el color de la card)": None,
    "Crema": (243, 238, 226, 255),
    "Verde claro": (228, 238, 224, 255),
}


@dataclass
class OpcionesFondo:
    modo: str = "elegir"  # "elegir" (vista previa), "isnet" o "u2net"
    fondo: str = "Blanco"
    lado: int = 1600      # cuadro de salida en px (las cards del vivero son cuadradas)
    margen: float = 0.07  # espacio alrededor de la planta (0.07 = 7 %)
    sombra: bool = False
    calidad: int = 88


_sesiones: dict = {}
_candado = threading.Lock()


def disponible() -> bool:
    try:
        import rembg  # noqa: F401
        return True
    except Exception:
        return False


def modelo_descargado(clave: str) -> bool:
    import os
    nombre = MODELOS[clave][0]
    base = Path(os.environ.get("U2NET_HOME", Path.home() / ".u2net"))
    for carpeta in (base, Path.home() / ".rembg" / "models" / nombre):
        if any(carpeta.glob(f"{nombre}*.onnx")) if carpeta.exists() else False:
            return True
    return False


def sesion(clave: str):
    """Carga el modelo una sola vez (la primera vez lo descarga, ~170 MB)."""
    with _candado:
        if clave not in _sesiones:
            from rembg import new_session
            _sesiones[clave] = new_session(MODELOS[clave][0])
        return _sesiones[clave]


def abrir_para_fondo(ruta: Path, preparar) -> Image.Image:
    """Abre la foto original enderezada, en sRGB y a un tamaño cómodo para el modelo.
    `preparar` es la función del optimizador que endereza y corrige el color."""
    with Image.open(ruta) as original:
        img = preparar(original, "WEBP").convert("RGB")
    if max(img.size) > LADO_ENTRADA:
        img.thumbnail((LADO_ENTRADA, LADO_ENTRADA), Image.LANCZOS)
    return img


def limpiar_mascara(alfa: Image.Image) -> Image.Image:
    """Quita manchas sueltas lejos de la planta y el velo casi invisible."""
    import numpy as np
    from scipy import ndimage

    a = np.asarray(alfa, dtype=np.uint8).copy()
    a[a < 12] = 0  # velo casi transparente que ensucia el recorte
    # Se buscan las piezas en una versión chica (rápido) y se conservan las importantes
    escala = max(1, max(a.shape) // 600)
    chica = a[::escala, ::escala] > 40
    etiquetas, n = ndimage.label(chica)
    if n > 1:
        areas = ndimage.sum(chica, etiquetas, range(1, n + 1))
        mayor = areas.max()
        # Piezas grandes (≥ 3 % de la principal) se quedan: ramas o macetas separadas por una hoja
        conservar = np.isin(etiquetas, [i + 1 for i, ar in enumerate(areas) if ar >= mayor * 0.03])
        conservar = ndimage.binary_dilation(conservar, iterations=2)
        grande = np.kron(conservar, np.ones((escala, escala), dtype=bool))[: a.shape[0], : a.shape[1]]
        if grande.shape != a.shape:  # por si el redondeo dejó orillas sin cubrir
            pad = np.zeros(a.shape, dtype=bool)
            pad[: grande.shape[0], : grande.shape[1]] = grande
            grande = pad
        a[~grande] = 0
    return Image.fromarray(a, "L")


def quitar(img: Image.Image, clave: str) -> Image.Image:
    """Devuelve la planta en RGBA (fondo transparente)."""
    from rembg import remove
    mascara = remove(img, session=sesion(clave), only_mask=True)
    if mascara.size != img.size:
        mascara = mascara.resize(img.size, Image.LANCZOS)
    rgba = img.convert("RGBA")
    rgba.putalpha(limpiar_mascara(mascara.convert("L")))
    return rgba


def componer(planta: Image.Image, op: OpcionesFondo) -> Image.Image | None:
    """Recorta la planta, la centra en un cuadro con margen parejo y pone el fondo.
    Devuelve None si no quedó nada (el modelo no encontró la planta)."""
    bb = planta.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    if not bb:
        return None
    planta = planta.crop(bb)
    # No se agranda la planta más de lo que mide en la foto (se vería borrosa): si es chica,
    # el cuadro sale más chico, pero nunca de menos de 600 px (lo que mide la card del sitio)
    necesario = int(max(planta.size) / (1 - 2 * op.margen))
    L = max(min(op.lado, necesario), min(op.lado, LADO_MINIMO))
    util = int(L * (1 - 2 * op.margen))
    escala = min(util / planta.width, util / planta.height)
    planta = planta.resize((max(1, round(planta.width * escala)), max(1, round(planta.height * escala))), Image.LANCZOS)

    color = FONDOS.get(op.fondo)
    lienzo = Image.new("RGBA", (L, L), color if color else (0, 0, 0, 0))
    x = (L - planta.width) // 2
    y = (L - planta.height) // 2

    if op.sombra:
        sombra = Image.new("L", (L, L), 0)
        base = y + planta.height
        ancho = planta.width * 0.42
        cx = L / 2
        ImageDraw.Draw(sombra).ellipse([cx - ancho, base - L * 0.018, cx + ancho, base + L * 0.018], fill=95)
        sombra = sombra.filter(ImageFilter.GaussianBlur(L * 0.014))
        capa = Image.new("RGBA", (L, L), (40, 45, 35, 0))
        capa.putalpha(sombra)
        lienzo.alpha_composite(capa)

    lienzo.alpha_composite(planta, (x, y))
    return lienzo if color is None else lienzo.convert("RGB")


def guardar(img: Image.Image, destino: Path, calidad: int) -> int:
    destino.parent.mkdir(parents=True, exist_ok=True)
    img.save(destino, "WEBP", quality=calidad, method=6)
    return destino.stat().st_size


def vista(img: Image.Image, lado: int = 340) -> Image.Image:
    """Miniatura para la vista previa; lo transparente se ve como cuadritos."""
    v = img.copy()
    v.thumbnail((lado, lado), Image.LANCZOS)
    if v.mode == "RGBA":
        cuadros = Image.new("RGBA", v.size, (255, 255, 255, 255))
        d = ImageDraw.Draw(cuadros)
        t = 12
        for yy in range(0, v.height, t):
            for xx in range(0, v.width, t):
                if (xx // t + yy // t) % 2:
                    d.rectangle([xx, yy, xx + t - 1, yy + t - 1], fill=(225, 225, 225, 255))
        cuadros.alpha_composite(v)
        v = cuadros.convert("RGB")
    return v
