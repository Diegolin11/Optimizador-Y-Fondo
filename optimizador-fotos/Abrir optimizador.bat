@echo off
rem Abre el optimizador. La primera vez instala lo que necesita.
rem Tambien puedes arrastrar fotos, videos o carpetas encima de este archivo.
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo No encontre Python. Instalalo desde https://www.python.org/downloads/
  echo y marca la casilla "Add python.exe to PATH" al instalar.
  pause
  exit /b
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Tu Python es muy viejo. Instala Python 3.11 o mas nuevo desde https://www.python.org/downloads/
  pause
  exit /b
)

python -c "import PIL, pillow_heif, tkinterdnd2, imageio_ffmpeg, rembg" >nul 2>&1
if errorlevel 1 (
  echo Instalando lo necesario, solo pasa la primera vez, puede tardar unos minutos...
  python -m pip install -r requirements.txt
)

start "" pythonw optimizador.py %*
