"""
Optimizador de fotos y videos
-----------------------------
Le pasas fotos y videos sueltos o carpetas completas y los deja ligeros sin que se
note la diferencia: los endereza, los reduce, les quita los metadatos (incluido el
GPS) y los guarda en WebP/JPG (fotos) o MP4 (videos). Los originales NUNCA se tocan.

Uso con ventana:   python optimizador.py
Uso en terminal:   python optimizador.py --perfil plantas FOTOS_VIDEOS_O_CARPETAS... [--salida carpeta]
                   python optimizador.py --ayuda
"""
from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

import fondo as fon
import videos as vid

# HEIC/HEIF (fotos de iPhone). Si no está instalado, simplemente no se aceptan.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC = True
except ImportError:
    HEIC = False

Image.MAX_IMAGE_PIXELS = 200_000_000  # fotos de cámara grandes sin que Pillow se queje

EXTENSIONES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
if HEIC:
    EXTENSIONES |= {".heic", ".heif"}


# ---------------------------------------------------------------------------
# Perfiles
# ---------------------------------------------------------------------------
# Los del vivero coinciden con lo que hace el panel (1600 px fichas, 2400 px
# carrusel/portadas). Se sube con calidad 88 porque el panel vuelve a
# comprimir a 80: así la segunda compresión no se nota.
@dataclass
class Perfil:
    lado: int          # lado más largo en px (0 = no cambiar el tamaño)
    formato: str       # "WEBP" o "JPEG"
    calidad: int       # 1-100
    max_kb: int = 0    # 0 = sin límite de peso


PERFILES: dict[str, Perfil] = {
    "Vivero · plantas, fertilizantes y novedades (1600 px)": Perfil(1600, "WEBP", 88),
    "Vivero · carrusel y portadas (2400 px)": Perfil(2400, "WEBP", 88),
    "Web en general (1920 px)": Perfil(1920, "WEBP", 82),
    "WhatsApp y redes (1280 px, JPG)": Perfil(1280, "JPEG", 82),
    "Correo / muy ligero (1024 px, máx. 200 KB)": Perfil(1024, "JPEG", 80, 200),
    "Mismo tamaño, solo bajar el peso": Perfil(0, "WEBP", 85),
}
# Nombres cortos para la terminal
ATAJOS = {
    "plantas": "Vivero · plantas, fertilizantes y novedades (1600 px)",
    "carrusel": "Vivero · carrusel y portadas (2400 px)",
    "web": "Web en general (1920 px)",
    "whatsapp": "WhatsApp y redes (1280 px, JPG)",
    "correo": "Correo / muy ligero (1024 px, máx. 200 KB)",
    "mismo": "Mismo tamaño, solo bajar el peso",
}

SRGB = ImageCms.createProfile("sRGB")


# ---------------------------------------------------------------------------
# Lógica (sin ventana, para poder probarla y usarla desde la terminal)
# ---------------------------------------------------------------------------
@dataclass
class Resultado:
    antes: int
    despues: int
    medidas: tuple[int, int]
    calidad_usada: int
    copiada: bool = False  # True si el original ya era más ligero y se copió tal cual


def legible(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def preparar(img: Image.Image, formato: str) -> Image.Image:
    """Endereza, pasa a sRGB y deja el modo de color listo para guardar."""
    img = ImageOps.exif_transpose(img)

    # Los iPhone guardan en Display P3: sin convertir, al quitar el perfil los colores se apagan
    icc = img.info.get("icc_profile")
    if icc:
        try:
            origen = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            modo = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
            img = ImageCms.profileToProfile(img.convert(modo), origen, SRGB, outputMode=modo)
        except Exception:
            pass  # perfil raro o dañado: se queda como venía

    transparente = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    if transparente:
        img = img.convert("RGBA")
        if formato == "JPEG":  # JPG no tiene transparencia: fondo blanco
            fondo = Image.new("RGB", img.size, "white")
            fondo.paste(img, mask=img.getchannel("A"))
            img = fondo
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def codificar(img: Image.Image, formato: str, calidad: int) -> bytes:
    buf = io.BytesIO()
    if formato == "WEBP":
        img.save(buf, "WEBP", quality=calidad, method=6)
    else:
        img.save(buf, "JPEG", quality=calidad, optimize=True, progressive=True,
                 subsampling=0 if calidad >= 90 else 2)
    return buf.getvalue()


def optimizar(entrada: Path, salida: Path, perfil: Perfil) -> Resultado:
    """Optimiza una foto y la guarda en `salida`. Nunca modifica `entrada`."""
    antes = entrada.stat().st_size
    with Image.open(entrada) as original:
        formato_original = (original.format or "").upper()
        img = preparar(original, perfil.formato)
        medidas_originales = img.size

    if perfil.lado and max(img.size) > perfil.lado:
        img.thumbnail((perfil.lado, perfil.lado), Image.LANCZOS)

    datos = codificar(img, perfil.formato, perfil.calidad)
    calidad = perfil.calidad

    # Peso máximo: primero baja la calidad (sin pasar de 60); si no alcanza, reduce el tamaño
    if perfil.max_kb:
        limite = perfil.max_kb * 1024
        while len(datos) > limite:
            bajo, alto, mejor = 60, calidad, None
            while bajo <= alto:  # búsqueda binaria de la calidad más alta que cabe
                medio = (bajo + alto) // 2
                prueba = codificar(img, perfil.formato, medio)
                if len(prueba) <= limite:
                    mejor, calidad, bajo = prueba, medio, medio + 1
                else:
                    alto = medio - 1
            if mejor:
                datos = mejor
                break
            if max(img.size) < 400:  # ya no tiene caso hacerla más chica
                datos = codificar(img, perfil.formato, 60)
                calidad = 60
                break
            img = img.resize((round(img.width * 0.85), round(img.height * 0.85)), Image.LANCZOS)
            calidad = perfil.calidad
            datos = codificar(img, perfil.formato, calidad)

    salida.parent.mkdir(parents=True, exist_ok=True)

    # Si ya venía optimizada (mismo formato, mismo tamaño y pesa menos), se copia tal cual
    mismo_formato = formato_original == perfil.formato or (formato_original == "MPO" and perfil.formato == "JPEG")
    if mismo_formato and img.size == medidas_originales and antes <= len(datos):
        salida.write_bytes(entrada.read_bytes())
        return Resultado(antes, antes, img.size, calidad, copiada=True)

    salida.write_bytes(datos)
    return Resultado(antes, len(datos), img.size, calidad)


def es_video(ruta: Path) -> bool:
    return ruta.suffix.lower() in vid.EXT_VIDEO


def buscar_archivos(rutas: list[Path]) -> list[tuple[Path, Path]]:
    """Devuelve (foto o video, ruta relativa de salida). Las carpetas conservan sus subcarpetas."""
    aceptadas = EXTENSIONES | vid.EXT_VIDEO
    encontradas: list[tuple[Path, Path]] = []
    for ruta in rutas:
        if ruta.is_dir():
            for f in sorted(ruta.rglob("*")):
                if f.is_file() and f.suffix.lower() in aceptadas and not f.name.startswith(".") \
                        and ".parcial." not in f.name:
                    encontradas.append((f, Path(ruta.name) / f.relative_to(ruta)))
        elif ruta.is_file() and ruta.suffix.lower() in aceptadas:
            encontradas.append((ruta, Path(ruta.name)))
    return encontradas


def ruta_destino(carpeta_salida: Path, relativa: Path, ext: str,
                 nombre_base: str = "", numero: int = 0, usadas: set | None = None) -> Path:
    if nombre_base:
        destino = carpeta_salida / relativa.parent / f"{nombre_base}-{numero:03d}{ext}"
    else:
        destino = carpeta_salida / relativa.with_suffix(ext)
    # Evita encimar archivos (ej. foto.jpg y foto.png terminan los dos en foto.webp)
    usadas = usadas if usadas is not None else set()
    base, n = destino, 2
    while destino in usadas or destino.exists() or destino.with_name(destino.stem + "-espera.webp") in usadas:
        destino = base.with_name(f"{base.stem}_{n}{base.suffix}")
        n += 1
    usadas.add(destino)
    return destino


def nombre_seguro(texto: str) -> str:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", texto.strip().lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def carpeta_salida_por_defecto() -> Path:
    for candidata in (Path.home() / "Pictures", Path.home() / "Imágenes", Path.home() / "Desktop"):
        if candidata.is_dir():
            return candidata / "Optimizados"
    return Path.home() / "Optimizados"


def abrir_carpeta(ruta: Path) -> None:
    ruta.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(ruta)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(ruta)])
    else:
        subprocess.Popen(["xdg-open", str(ruta)])


# ---------------------------------------------------------------------------
# Un solo archivo (lo usan la terminal y la ventana)
# ---------------------------------------------------------------------------
@dataclass
class Hecho:
    antes: int
    despues: int
    medidas: str
    destino: Path
    nota: str = ""


def procesar(archivo: Path, rel: Path, salida: Path, perfil: Perfil, perfil_v: vid.PerfilVideo,
             base: str, numero: int, usadas: set,
             progreso=None, cancelar: threading.Event | None = None) -> Hecho:
    if es_video(archivo):
        destino = ruta_destino(salida, rel, ".mp4", base, numero, usadas)
        r = vid.optimizar_video(archivo, destino, perfil_v, progreso, cancelar)
        nota = " + imagen de espera" if r.poster else ""
        if r.copiado:
            nota += " (ya era ligero, solo se limpió)"
        return Hecho(r.antes, r.despues, f"{r.medidas[0]}×{r.medidas[1]} · {vid.duracion_legible(r.duracion)}",
                     destino, nota)
    destino = ruta_destino(salida, rel, ".webp" if perfil.formato == "WEBP" else ".jpg", base, numero, usadas)
    r = optimizar(archivo, destino, perfil)
    return Hecho(r.antes, r.despues, f"{r.medidas[0]}×{r.medidas[1]}", destino,
                 " (ya era ligera, se copió)" if r.copiada else "")


# ---------------------------------------------------------------------------
# Modo terminal
# ---------------------------------------------------------------------------
def modo_terminal(argv: list[str]) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="optimizador", add_help=False,
                                description="Optimiza fotos y videos sin que se note la diferencia.")
    p.add_argument("rutas", nargs="+", type=Path, help="fotos, videos o carpetas")
    p.add_argument("--perfil", choices=ATAJOS, default="plantas", help="perfil de fotos")
    p.add_argument("--perfil-video", choices=vid.ATAJOS_VIDEO, default="carrusel")
    p.add_argument("--salida", type=Path, default=None)
    p.add_argument("--calidad", type=int, help="calidad de fotos (1-100)")
    p.add_argument("--lado", type=int, help="lado más largo de fotos en px")
    p.add_argument("--formato", choices=["webp", "jpg"])
    p.add_argument("--max-kb", type=int, help="peso máximo por foto")
    p.add_argument("--calidad-video", choices=vid.CALIDADES)
    p.add_argument("--lado-video", type=int)
    p.add_argument("--max-mb", type=int, help="peso máximo por video")
    p.add_argument("--audio", choices=["si", "no"], help="conservar el sonido de los videos")
    p.add_argument("--nombre", default="", help="renombrar como nombre-001, nombre-002…")
    p.add_argument("--quitar-fondo", choices=list(fon.MODELOS), help="quitar el fondo a las fotos con este modelo")
    p.add_argument("--fondo", choices=["blanco", "transparente", "crema", "verde"], default="blanco")
    p.add_argument("--ayuda", "-h", action="help")
    a = p.parse_args(argv)

    base = PERFILES[ATAJOS[a.perfil]]
    perfil = Perfil(
        lado=a.lado if a.lado is not None else base.lado,
        formato={"webp": "WEBP", "jpg": "JPEG"}.get(a.formato or "", base.formato),
        calidad=a.calidad or base.calidad,
        max_kb=a.max_kb if a.max_kb is not None else base.max_kb,
    )
    bv = vid.PERFILES_VIDEO[vid.ATAJOS_VIDEO[a.perfil_video]]
    perfil_v = vid.PerfilVideo(
        lado=a.lado_video if a.lado_video is not None else bv.lado,
        calidad=a.calidad_video or bv.calidad,
        audio=bv.audio if a.audio is None else a.audio == "si",
        fps=bv.fps,
        max_mb=a.max_mb if a.max_mb is not None else bv.max_mb,
        poster=bv.poster,
    )
    op_fondo = None
    if a.quitar_fondo:
        nombres_fondo = {"blanco": "Blanco", "transparente": "Transparente (toma el color de la card)",
                         "crema": "Crema", "verde": "Verde claro"}
        op_fondo = fon.OpcionesFondo(a.quitar_fondo, nombres_fondo[a.fondo], calidad=perfil.calidad)
    salida = a.salida or carpeta_salida_por_defecto()
    archivos = buscar_archivos(a.rutas)
    if not archivos:
        print("No encontré fotos ni videos en lo que me pasaste.")
        return
    usadas: set = set()
    total_a = total_d = 0
    for i, (archivo, rel) in enumerate(archivos, 1):
        try:
            def avance(x: float, rel=rel, i=i) -> None:
                print(f"\r[{i}/{len(archivos)}] {rel}: {x * 100:.0f} %", end="", flush=True)
            if op_fondo and not es_video(archivo):
                img = fon.abrir_para_fondo(archivo, preparar)
                compuesta = fon.componer(fon.quitar(img, op_fondo.modo), op_fondo)
                if compuesta is None:
                    raise RuntimeError("no se encontró la planta en la foto")
                destino = ruta_destino(salida, rel, ".webp", nombre_seguro(a.nombre), i, usadas)
                h = Hecho(archivo.stat().st_size, fon.guardar(compuesta, destino, op_fondo.calidad),
                          f"{compuesta.width}×{compuesta.height}", destino, " · sin fondo")
            else:
                h = procesar(archivo, rel, salida, perfil, perfil_v, nombre_seguro(a.nombre), i, usadas,
                             avance if es_video(archivo) else None)
            total_a += h.antes
            total_d += h.despues
            print(f"\r[{i}/{len(archivos)}] {rel}: {legible(h.antes)} -> {legible(h.despues)} {h.medidas}{h.nota}")
        except Exception as e:
            print(f"\r[{i}/{len(archivos)}] {rel}: ERROR {e}")
    if total_a:
        print(f"\nTotal: {legible(total_a)} -> {legible(total_d)} "
              f"(-{100 - total_d * 100 / total_a:.0f} %)  Guardados en: {salida}")


# ---------------------------------------------------------------------------
# Ventana
# ---------------------------------------------------------------------------
def modo_ventana() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        raiz = TkinterDnD.Tk()
        arrastrar = True
    except Exception:
        raiz = tk.Tk()
        arrastrar = False

    VERDE = "#2f6b3a"
    GRIS = "#666"
    raiz.title("Optimizador de fotos y videos")
    raiz.geometry("1000x700")
    raiz.minsize(860, 580)
    estilo = ttk.Style(raiz)
    if "vista" in estilo.theme_names():
        estilo.theme_use("vista")
    elif "clam" in estilo.theme_names():
        estilo.theme_use("clam")
    estilo.configure("Grande.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
    estilo.configure("Treeview", rowheight=24)
    estilo.configure("Horizontal.TProgressbar", background=VERDE)

    archivos: dict[str, tuple[Path, Path]] = {}  # id de fila -> (archivo, ruta relativa)
    cola: queue.Queue = queue.Queue()
    trabajando = threading.Event()
    cancelar = threading.Event()
    ffmpeg_ok = vid.hay_ffmpeg()

    # ---------- Barra superior ----------
    arriba = ttk.Frame(raiz, padding=(12, 12, 12, 6))
    arriba.pack(fill="x")

    def agregar(rutas: list[Path]) -> None:
        if trabajando.is_set():
            return
        ya = {f for f, _ in archivos.values()}
        nuevas = 0
        for archivo, rel in buscar_archivos(rutas):
            if archivo in ya:
                continue
            tipo = "Video" if es_video(archivo) else "Foto"
            iid = tabla.insert("", "end", values=(str(rel), tipo, legible(archivo.stat().st_size), "", "", "", "Pendiente"))
            archivos[iid] = (archivo, rel)
            nuevas += 1
        actualizar_resumen()
        if rutas and not nuevas and not archivos:
            messagebox.showinfo("Nada que agregar", "No encontré fotos ni videos ahí.")

    def elegir_archivos() -> None:
        fotos = " ".join(f"*{e} *{e.upper()}" for e in sorted(EXTENSIONES))
        videos = " ".join(f"*{e} *{e.upper()}" for e in sorted(vid.EXT_VIDEO))
        elegidos = filedialog.askopenfilenames(title="Elige fotos o videos", filetypes=[
            ("Fotos y videos", f"{fotos} {videos}"), ("Fotos", fotos), ("Videos", videos), ("Todos", "*.*")])
        agregar([Path(f) for f in elegidos])

    def elegir_carpeta() -> None:
        c = filedialog.askdirectory(title="Elige una carpeta")
        if c:
            agregar([Path(c)])

    def quitar() -> None:
        if trabajando.is_set():
            return
        for iid in tabla.selection():
            tabla.delete(iid)
            archivos.pop(iid, None)
        actualizar_resumen()

    def limpiar() -> None:
        if trabajando.is_set():
            return
        tabla.delete(*tabla.get_children())
        archivos.clear()
        actualizar_resumen()

    ttk.Button(arriba, text="+ Agregar fotos o videos", command=elegir_archivos).pack(side="left")
    ttk.Button(arriba, text="+ Agregar carpeta", command=elegir_carpeta).pack(side="left", padx=6)
    ttk.Button(arriba, text="Quitar seleccionados", command=quitar).pack(side="left")
    ttk.Button(arriba, text="Limpiar lista", command=limpiar).pack(side="left", padx=6)
    ttk.Label(arriba, text=("También puedes arrastrarlos a la lista" if arrastrar
                            else "Para arrastrar archivos instala tkinterdnd2"),
              foreground=GRIS).pack(side="right")

    # ---------- Lista ----------
    medio = ttk.Frame(raiz, padding=(12, 0))
    medio.pack(fill="both", expand=True)
    columnas = ("archivo", "tipo", "antes", "despues", "ahorro", "medidas", "estado")
    tabla = ttk.Treeview(medio, columns=columnas, show="headings", selectmode="extended")
    for col, titulo, ancho, ancla in [
        ("archivo", "Archivo", 300, "w"), ("tipo", "Tipo", 55, "center"), ("antes", "Antes", 75, "e"),
        ("despues", "Después", 75, "e"), ("ahorro", "Ahorro", 65, "e"), ("medidas", "Medidas", 130, "center"),
        ("estado", "Estado", 220, "w"),
    ]:
        tabla.heading(col, text=titulo)
        tabla.column(col, width=ancho, anchor=ancla, stretch=(col in ("archivo", "estado")))
    tabla.tag_configure("ok", foreground=VERDE)
    tabla.tag_configure("error", foreground="#b3261e")
    barra = ttk.Scrollbar(medio, orient="vertical", command=tabla.yview)
    tabla.configure(yscrollcommand=barra.set)
    tabla.pack(side="left", fill="both", expand=True)
    barra.pack(side="right", fill="y")
    tabla.bind("<Delete>", lambda e: quitar())

    if arrastrar:
        def al_soltar(evento) -> None:
            agregar([Path(p) for p in raiz.tk.splitlist(evento.data)])
        tabla.drop_target_register(DND_FILES)
        tabla.dnd_bind("<<Drop>>", al_soltar)

    # ---------- Ajustes ----------
    ajustes = ttk.Frame(raiz, padding=(12, 8, 12, 0))
    ajustes.pack(fill="x")
    pestanas = ttk.Notebook(ajustes)
    pestanas.pack(fill="x")
    tab_f = ttk.Frame(pestanas, padding=10)
    tab_v = ttk.Frame(pestanas, padding=10)
    pestanas.add(tab_f, text="  Ajustes de fotos  ")
    pestanas.add(tab_v, text="  Ajustes de videos  ")
    tab_q = ttk.Frame(pestanas, padding=10)
    pestanas.add(tab_q, text="  Quitar fondo (plantas)  ")

    # Fotos
    v_perfil = tk.StringVar(value=next(iter(PERFILES)))
    v_lado = tk.StringVar()
    v_formato = tk.StringVar()
    v_calidad = tk.IntVar()
    v_maxkb = tk.StringVar()

    def aplicar_perfil(*_) -> None:
        p = PERFILES[v_perfil.get()]
        v_lado.set(str(p.lado) if p.lado else "")
        v_formato.set("WebP" if p.formato == "WEBP" else "JPG")
        v_calidad.set(p.calidad)
        v_maxkb.set(str(p.max_kb) if p.max_kb else "")

    ttk.Label(tab_f, text="Perfil").grid(row=0, column=0, sticky="w")
    cb = ttk.Combobox(tab_f, textvariable=v_perfil, values=list(PERFILES), state="readonly", width=50)
    cb.grid(row=0, column=1, columnspan=2, sticky="w", padx=(6, 18))
    cb.bind("<<ComboboxSelected>>", aplicar_perfil)
    ttk.Label(tab_f, text="Formato").grid(row=0, column=3, sticky="w")
    ttk.Combobox(tab_f, textvariable=v_formato, values=["WebP", "JPG"], state="readonly", width=6) \
        .grid(row=0, column=4, sticky="w", padx=6)

    ttk.Label(tab_f, text="Lado más largo (px)").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(tab_f, textvariable=v_lado, width=8).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_f, text="vacío = no cambiar", foreground=GRIS).grid(row=1, column=2, sticky="w", pady=(8, 0))
    ttk.Label(tab_f, text="Calidad").grid(row=1, column=3, sticky="w", pady=(8, 0))
    marco_cal = ttk.Frame(tab_f)
    marco_cal.grid(row=1, column=4, sticky="w", padx=6, pady=(8, 0))
    lbl_cal = ttk.Label(marco_cal, width=3)
    ttk.Scale(marco_cal, from_=50, to=100, variable=v_calidad, length=140,
              command=lambda v: v_calidad.set(round(float(v)))).pack(side="left")
    lbl_cal.pack(side="left", padx=4)
    v_calidad.trace_add("write", lambda *_: lbl_cal.config(text=str(v_calidad.get())))

    ttk.Label(tab_f, text="Peso máximo (KB)").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(tab_f, textvariable=v_maxkb, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_f, text="vacío = sin límite", foreground=GRIS).grid(row=2, column=2, sticky="w", pady=(8, 0))
    aplicar_perfil()

    # Videos
    v_perfil_v = tk.StringVar(value=next(iter(vid.PERFILES_VIDEO)))
    v_lado_v = tk.StringVar()
    v_cal_v = tk.StringVar()
    v_maxmb = tk.StringVar()
    v_audio = tk.BooleanVar()
    v_poster = tk.BooleanVar()
    v_fps30 = tk.BooleanVar()

    def aplicar_perfil_v(*_) -> None:
        p = vid.PERFILES_VIDEO[v_perfil_v.get()]
        v_lado_v.set(str(p.lado) if p.lado else "")
        v_cal_v.set(p.calidad)
        v_maxmb.set(str(p.max_mb) if p.max_mb else "")
        v_audio.set(p.audio)
        v_poster.set(p.poster)
        v_fps30.set(bool(p.fps))

    ttk.Label(tab_v, text="Perfil").grid(row=0, column=0, sticky="w")
    cbv = ttk.Combobox(tab_v, textvariable=v_perfil_v, values=list(vid.PERFILES_VIDEO), state="readonly", width=50)
    cbv.grid(row=0, column=1, columnspan=2, sticky="w", padx=(6, 18))
    cbv.bind("<<ComboboxSelected>>", aplicar_perfil_v)
    ttk.Label(tab_v, text="Calidad").grid(row=0, column=3, sticky="w")
    ttk.Combobox(tab_v, textvariable=v_cal_v, values=vid.CALIDADES, state="readonly", width=8) \
        .grid(row=0, column=4, sticky="w", padx=6)

    ttk.Label(tab_v, text="Lado más largo (px)").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(tab_v, textvariable=v_lado_v, width=8).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_v, text="vacío = no cambiar", foreground=GRIS).grid(row=1, column=2, sticky="w", pady=(8, 0))
    ttk.Checkbutton(tab_v, text="Conservar el sonido", variable=v_audio).grid(row=1, column=3, columnspan=2, sticky="w", pady=(8, 0))

    ttk.Label(tab_v, text="Peso máximo (MB)").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(tab_v, textvariable=v_maxmb, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_v, text="vacío = sin límite", foreground=GRIS).grid(row=2, column=2, sticky="w", pady=(8, 0))
    ttk.Checkbutton(tab_v, text="Sacar imagen de espera (WebP)", variable=v_poster).grid(row=2, column=3, columnspan=2, sticky="w", pady=(8, 0))
    ttk.Checkbutton(tab_v, text="Máximo 30 cuadros por segundo", variable=v_fps30).grid(row=3, column=3, columnspan=2, sticky="w", pady=(4, 0))
    if not ffmpeg_ok:
        ttk.Label(tab_v, text="⚠ Falta ffmpeg: abre el programa con «Abrir optimizador.bat» o instala imageio-ffmpeg.",
                  foreground="#b3261e").grid(row=4, column=0, columnspan=5, sticky="w", pady=(8, 0))
    aplicar_perfil_v()

    # Quitar fondo
    rembg_ok = fon.disponible()
    MODOS = {"Elegir en vista previa (recomendado)": "elegir", "Siempre ISNet (hojas finas)": "isnet",
             "Siempre U2Net (macetas oscuras)": "u2net"}
    v_q_on = tk.BooleanVar(value=False)
    v_q_modo = tk.StringVar(value=next(iter(MODOS)))
    v_q_fondo = tk.StringVar(value="Blanco")
    v_q_lado = tk.StringVar(value="1600")
    v_q_margen = tk.StringVar(value="7")
    v_q_sombra = tk.BooleanVar(value=False)

    ttk.Checkbutton(tab_q, text="Quitarle el fondo a las fotos (para las cards de Plantas)", variable=v_q_on,
                    state="normal" if rembg_ok else "disabled").grid(row=0, column=0, columnspan=4, sticky="w")
    ttk.Label(tab_q, text="Modelo").grid(row=1, column=0, sticky="w", pady=(8, 0))
    ttk.Combobox(tab_q, textvariable=v_q_modo, values=list(MODOS), state="readonly", width=36) \
        .grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_q, text="Fondo").grid(row=1, column=2, sticky="w", padx=(18, 0), pady=(8, 0))
    ttk.Combobox(tab_q, textvariable=v_q_fondo, values=list(fon.FONDOS), state="readonly", width=34) \
        .grid(row=1, column=3, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_q, text="Cuadro de salida (px)").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Entry(tab_q, textvariable=v_q_lado, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
    ttk.Label(tab_q, text="Margen (%)").grid(row=2, column=2, sticky="w", padx=(18, 0), pady=(8, 0))
    marco_m = ttk.Frame(tab_q)
    marco_m.grid(row=2, column=3, sticky="w", padx=6, pady=(8, 0))
    ttk.Entry(marco_m, textvariable=v_q_margen, width=5).pack(side="left")
    ttk.Checkbutton(marco_m, text="Sombra suave abajo", variable=v_q_sombra).pack(side="left", padx=(18, 0))
    ttk.Label(tab_q, foreground=GRIS, text=(
        "Se quita el fondo a la foto original y después se optimiza. Salen en cuadro (como las cards) "
        "en WebP con la calidad de la pestaña Fotos. Los videos no cambian.") if rembg_ok else
        "⚠ Falta instalar rembg: abre el programa con «Abrir optimizador.bat».",
        wraplength=820).grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))

    # Comunes
    comunes = ttk.Frame(ajustes, padding=(0, 8, 0, 0))
    comunes.pack(fill="x")
    v_nombre = tk.StringVar()
    v_salida = tk.StringVar(value=str(carpeta_salida_por_defecto()))
    ttk.Label(comunes, text="Guardar en").grid(row=0, column=0, sticky="w")
    ttk.Entry(comunes, textvariable=v_salida, width=62).grid(row=0, column=1, sticky="we", padx=6)

    def elegir_salida() -> None:
        c = filedialog.askdirectory(title="¿Dónde guardo los archivos optimizados?")
        if c:
            v_salida.set(c)
    ttk.Button(comunes, text="Cambiar…", command=elegir_salida).grid(row=0, column=2, sticky="w")
    ttk.Label(comunes, text="Renombrar como").grid(row=0, column=3, sticky="w", padx=(18, 0))
    ttk.Entry(comunes, textvariable=v_nombre, width=20).grid(row=0, column=4, sticky="w", padx=6)
    ttk.Label(comunes, text="queda nombre-001, nombre-002…", foreground=GRIS).grid(row=1, column=4, sticky="w", padx=6)
    comunes.columnconfigure(1, weight=1)

    # ---------- Abajo ----------
    abajo = ttk.Frame(raiz, padding=(12, 6, 12, 12))
    abajo.pack(fill="x")
    progreso = ttk.Progressbar(abajo, mode="determinate")
    progreso.pack(fill="x", pady=(0, 8))
    resumen = ttk.Label(abajo, text="", font=("Segoe UI", 10))
    resumen.pack(side="left")
    ttk.Button(abajo, text="Abrir carpeta", command=lambda: abrir_carpeta(Path(v_salida.get()))).pack(side="right")
    btn_detener = ttk.Button(abajo, text="Detener", state="disabled")
    btn_detener.pack(side="right", padx=(8, 0))
    btn_ir = ttk.Button(abajo, text="Optimizar", style="Grande.TButton")
    btn_ir.pack(side="right", padx=8)

    def actualizar_resumen(texto: str | None = None) -> None:
        if texto is None:
            n = len(archivos)
            nv = sum(1 for f, _ in archivos.values() if es_video(f))
            peso = sum(f.stat().st_size for f, _ in archivos.values() if f.exists())
            partes = []
            if n - nv:
                partes.append(f"{n - nv} foto{'s' if n - nv != 1 else ''}")
            if nv:
                partes.append(f"{nv} video{'s' if nv != 1 else ''}")
            texto = (" y ".join(partes) + f" en la lista · {legible(peso)}") if n else \
                "Agrega fotos, videos o carpetas para empezar."
        resumen.config(text=texto)

    def leer_perfiles():
        try:
            lado = int(v_lado.get()) if v_lado.get().strip() else 0
            max_kb = int(v_maxkb.get()) if v_maxkb.get().strip() else 0
            lado_v = int(v_lado_v.get()) if v_lado_v.get().strip() else 0
            max_mb = int(v_maxmb.get()) if v_maxmb.get().strip() else 0
            if min(lado, max_kb, lado_v, max_mb) < 0 or 0 < lado < 100 or 0 < lado_v < 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("Revisa los ajustes", "Los lados y pesos máximos deben ser números enteros "
                                 "(los lados de al menos 100 px), o dejarse vacíos.")
            return None
        try:
            q_lado = int(v_q_lado.get() or 1600)
            q_margen = float(v_q_margen.get() or 7)
            if not (200 <= q_lado <= 5000 and 0 <= q_margen <= 30):
                raise ValueError
        except ValueError:
            messagebox.showerror("Revisa «Quitar fondo»", "El cuadro debe ir de 200 a 5000 px y el margen de 0 a 30 %.")
            return None
        opciones_fondo = fon.OpcionesFondo(MODOS[v_q_modo.get()], v_q_fondo.get(), q_lado, q_margen / 100,
                                           v_q_sombra.get(), int(v_calidad.get())) if v_q_on.get() else None
        return (Perfil(lado, "WEBP" if v_formato.get() == "WebP" else "JPEG", int(v_calidad.get()), max_kb),
                vid.PerfilVideo(lado_v, v_cal_v.get(), v_audio.get(), 30 if v_fps30.get() else 0, max_mb, v_poster.get()),
                opciones_fondo)

    respuestas: queue.Queue = queue.Queue()  # lo que eliges en la vista previa

    def quitar_fondo(iid, archivo: Path, rel: Path, op: fon.OpcionesFondo, fijo: dict, salida: Path,
                     base: str, numero: int, usadas: set, pos: str):
        """Quita el fondo; si hay que elegir, pregunta en la vista previa. None = descartada."""
        claves = [fijo["modelo"]] if fijo.get("modelo") else (["isnet", "u2net"] if op.modo == "elegir" else [op.modo])
        for k in claves:
            if not fon.modelo_descargado(k):
                cola.put(("estado", iid, f"Descargando modelo {k} (~170 MB, solo la primera vez)…"))
        cola.put(("estado", iid, "Quitando fondo…"))
        img = fon.abrir_para_fondo(archivo, preparar)
        resultados = {}
        for k in claves:
            if cancelar.is_set():
                raise vid.Cancelado()
            r = fon.componer(fon.quitar(img, k), op)
            if r is not None:
                resultados[k] = r
        if not resultados:
            raise RuntimeError("no se encontró la planta en la foto")
        if len(resultados) == 1:
            elegido = next(iter(resultados))
        else:
            cola.put(("elegir", iid, rel, pos, img, resultados))
            while True:
                try:
                    elegido, siempre = respuestas.get(timeout=0.2)
                    break
                except queue.Empty:
                    if cancelar.is_set():
                        raise vid.Cancelado()
            if elegido == "detener":
                cancelar.set()
                raise vid.Cancelado()
            if elegido == "descartar":
                return None
            if siempre:
                fijo["modelo"] = elegido
        destino = ruta_destino(salida, rel, ".webp", base, numero, usadas)
        despues = fon.guardar(resultados[elegido], destino, op.calidad)
        nombre = fon.MODELOS[elegido][1].split(" (")[0]
        lado = resultados[elegido].width
        return Hecho(archivo.stat().st_size, despues, f"{lado}×{lado}", destino, f" · sin fondo ({nombre})")

    def trabajador(trabajos: list, perfil: Perfil, perfil_v: vid.PerfilVideo, op_fondo, salida: Path,
                   base: str) -> None:
        usadas: set = set()
        fijo: dict = {}
        while not respuestas.empty():
            respuestas.get_nowait()
        for i, (iid, archivo, rel) in enumerate(trabajos, 1):
            if cancelar.is_set():
                cola.put(("estado", iid, "Detenido"))
                continue
            video = es_video(archivo)
            if video and not ffmpeg_ok:
                cola.put(("error", iid, "falta ffmpeg"))
                continue
            if not video and op_fondo:
                try:
                    h = quitar_fondo(iid, archivo, rel, op_fondo, fijo, salida, base, i, usadas,
                                     f"{i} de {len(trabajos)}")
                    cola.put(("listo", iid, h) if h else ("descartada", iid))
                except vid.Cancelado:
                    cola.put(("estado", iid, "Detenido"))
                except Exception as e:
                    cola.put(("error", iid, str(e) or e.__class__.__name__))
                continue
            cola.put(("estado", iid, "Optimizando… 0 %" if video else "Optimizando…"))
            avance = (lambda x, iid=iid: cola.put(("avance", iid, x))) if video else None
            try:
                h = procesar(archivo, rel, salida, perfil, perfil_v, base, i, usadas, avance, cancelar)
                cola.put(("listo", iid, h))
            except vid.Cancelado:
                cola.put(("estado", iid, "Detenido"))
            except Exception as e:
                cola.put(("error", iid, str(e) or e.__class__.__name__))
        cola.put(("fin",))

    totales = {"antes": 0, "despues": 0, "hechas": 0, "errores": 0, "descartadas": 0, "n": 0}

    def revisar_cola() -> None:
        try:
            while True:
                m = cola.get_nowait()
                if m[0] == "estado":
                    tabla.set(m[1], "estado", m[2])
                    tabla.see(m[1])
                elif m[0] == "avance":
                    tabla.set(m[1], "estado", f"Optimizando… {m[2] * 100:.0f} %")
                    progreso["value"] = totales["hechas"] + totales["errores"] + m[2]
                elif m[0] == "listo":
                    _, iid, h = m
                    ahorro = 100 - h.despues * 100 / h.antes if h.antes else 0
                    tabla.item(iid, tags=("ok",))
                    tabla.set(iid, "despues", legible(h.despues))
                    tabla.set(iid, "ahorro", f"-{ahorro:.0f} %" if ahorro >= 0.5 else "—")
                    tabla.set(iid, "medidas", h.medidas)
                    tabla.set(iid, "estado", f"✓ {h.destino.name}{h.nota}")
                    totales["antes"] += h.antes
                    totales["despues"] += h.despues
                    totales["hechas"] += 1
                    progreso["value"] = totales["hechas"] + totales["errores"]
                elif m[0] == "elegir":
                    abrir_vista_previa(*m[1:])
                elif m[0] == "descartada":
                    tabla.set(m[1], "estado", "Descartada (no se guardó)")
                    totales["errores"] += 1
                    totales["descartadas"] += 1
                    progreso["value"] = totales["hechas"] + totales["errores"]
                elif m[0] == "error":
                    tabla.item(m[1], tags=("error",))
                    tabla.set(m[1], "estado", f"Error: {m[2]}")
                    totales["errores"] += 1
                    progreso["value"] = totales["hechas"] + totales["errores"]
                elif m[0] == "fin":
                    trabajando.clear()
                    btn_ir.config(state="normal", text="Optimizar")
                    btn_detener.config(state="disabled")
                    a, d = totales["antes"], totales["despues"]
                    txt = ("Detenido: " if cancelar.is_set() else "Listo: ") + f"{totales['hechas']} de {totales['n']}"
                    if a:
                        txt += f" · {legible(a)} → {legible(d)} (-{100 - d * 100 / a:.0f} %)"
                    if totales["descartadas"]:
                        txt += f" · {totales['descartadas']} descartada{'s' if totales['descartadas'] != 1 else ''}"
                    if totales["errores"] - totales["descartadas"]:
                        txt += f" · {totales['errores'] - totales['descartadas']} con error"
                    actualizar_resumen(txt)
                    return
        except queue.Empty:
            pass
        if trabajando.is_set():
            raiz.after(100, revisar_cola)

    def abrir_vista_previa(iid, rel: Path, pos: str, original: Image.Image, resultados: dict) -> None:
        from PIL import ImageTk
        tabla.set(iid, "estado", "Esperando que elijas…")
        tabla.see(iid)
        v = tk.Toplevel(raiz)
        v.title(f"Elige el mejor recorte · {rel.name} ({pos})")
        v.transient(raiz)
        v.resizable(False, False)
        cuerpo = ttk.Frame(v, padding=14)
        cuerpo.pack()
        imagenes = []  # tkinter necesita guardar las imágenes para que no desaparezcan
        respondido = {"listo": False}
        siempre = tk.BooleanVar(value=False)

        def responder(eleccion: str) -> None:
            if respondido["listo"]:
                return
            respondido["listo"] = True
            respuestas.put((eleccion, siempre.get()))
            v.grab_release()
            v.destroy()
            if eleccion not in ("descartar", "detener"):
                tabla.set(iid, "estado", "Guardando…")

        columnas_vp = [("Original", fon.vista(original), None)]
        for n, (k, img) in enumerate(resultados.items(), 1):
            columnas_vp.append((fon.MODELOS[k][1], fon.vista(img), (k, n)))
        for c, (titulo, miniatura, dato) in enumerate(columnas_vp):
            ttk.Label(cuerpo, text=titulo, font=("Segoe UI", 10, "bold")).grid(row=0, column=c, pady=(0, 6))
            foto = ImageTk.PhotoImage(miniatura)
            imagenes.append(foto)
            tk.Label(cuerpo, image=foto, borderwidth=1, relief="solid", background="white",
                     width=340, height=340).grid(row=1, column=c, padx=6)
            if dato:
                k, n = dato
                ttk.Button(cuerpo, text=f"Usar este  ({n})", command=lambda k=k: responder(k)) \
                    .grid(row=2, column=c, pady=(8, 0), sticky="we", padx=6)
                v.bind(str(n), lambda e, k=k: responder(k))
        v.imagenes = imagenes  # type: ignore[attr-defined]
        pie = ttk.Frame(cuerpo, padding=(0, 12, 0, 0))
        pie.grid(row=3, column=0, columnspan=len(columnas_vp), sticky="we")
        ttk.Checkbutton(pie, text="Usar el modelo que elija para todas las demás fotos (sin preguntar)",
                        variable=siempre).pack(side="left")
        ttk.Button(pie, text="Detener todo", command=lambda: responder("detener")).pack(side="right")
        ttk.Button(pie, text="Descartar esta foto (D)", command=lambda: responder("descartar")).pack(side="right", padx=6)
        v.bind("d", lambda e: responder("descartar"))
        v.bind("<Escape>", lambda e: responder("descartar"))
        v.protocol("WM_DELETE_WINDOW", lambda: responder("descartar"))
        v.update_idletasks()
        x = raiz.winfo_rootx() + max(0, (raiz.winfo_width() - v.winfo_width()) // 2)
        y = raiz.winfo_rooty() + 40
        v.geometry(f"+{x}+{y}")
        v.grab_set()
        v.focus_set()

    def iniciar() -> None:
        if trabajando.is_set():
            return
        if not archivos:
            messagebox.showinfo("Lista vacía", "Primero agrega fotos, videos o una carpeta.")
            return
        perfiles = leer_perfiles()
        if not perfiles:
            return
        salida = Path(v_salida.get().strip() or carpeta_salida_por_defecto())
        trabajos = [(iid, *archivos[iid]) for iid in tabla.get_children()]
        for iid, _, _ in trabajos:
            tabla.item(iid, tags=())
            for col in ("despues", "ahorro", "medidas"):
                tabla.set(iid, col, "")
            tabla.set(iid, "estado", "En espera")
        totales.update(antes=0, despues=0, hechas=0, errores=0, descartadas=0, n=len(trabajos))
        progreso.config(maximum=len(trabajos), value=0)
        btn_ir.config(state="disabled", text="Trabajando…")
        btn_detener.config(state="normal")
        actualizar_resumen("Optimizando…")
        cancelar.clear()
        trabajando.set()
        threading.Thread(target=trabajador, args=(trabajos, *perfiles, salida, nombre_seguro(v_nombre.get())),
                         daemon=True).start()
        raiz.after(100, revisar_cola)

    def detener() -> None:
        cancelar.set()
        for w in raiz.winfo_children():  # cierra la vista previa si estaba abierta
            if isinstance(w, tk.Toplevel):
                w.destroy()
        btn_detener.config(state="disabled")
        actualizar_resumen("Deteniendo…")

    def al_cerrar() -> None:
        if trabajando.is_set():
            if not messagebox.askyesno("¿Salir?", "Todavía estoy trabajando. ¿Detener y salir?"):
                return
            cancelar.set()
            respuestas.put(("detener", False))
            vid.detener_todo()
        raiz.destroy()

    btn_ir.config(command=iniciar)
    btn_detener.config(command=detener)
    raiz.protocol("WM_DELETE_WINDOW", al_cerrar)
    actualizar_resumen()

    # Archivos pasados al abrir (por ejemplo, arrastrados sobre el .bat)
    iniciales = [Path(a) for a in sys.argv[1:] if Path(a).exists()]
    if iniciales:
        agregar(iniciales)

    raiz.mainloop()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and any(a.startswith("-") for a in args):
        modo_terminal(args)
    else:
        modo_ventana()
