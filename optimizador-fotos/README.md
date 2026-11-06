# Optimizador de fotos y videos

Programa de escritorio para dejar ligeras las fotos y videos antes de subirlos al panel de **Vivero El Encanto**, y para quitarle el fondo a las fotos de plantas.

- **Fotos:** las endereza, las reduce (1600 px o 2400 px), les quita los metadatos y el GPS, y las guarda en WebP.
- **Videos:** los pasa a MP4 a 1920 px y 30 fps, les quita el GPS y, para el carrusel, el sonido. También saca la imagen de espera.
- **Quitar fondo:** deja la planta sola en un cuadro, con fondo blanco o transparente, para las cards de Plantas.

Los originales nunca se modifican.

## Cómo usarlo (Windows)

1. Instala [Python](https://www.python.org/downloads/) 3.11 o más nuevo y marca **"Add python.exe to PATH"**.
2. Descarga el repo (`git clone` o *Code → Download ZIP*).
3. Da doble clic en **`Abrir optimizador.bat`**. La primera vez instala lo necesario.

Desde la terminal:

```
pip install -r requirements.txt
python optimizador.py
```

Las instrucciones completas están en [`LEEME.txt`](LEEME.txt).