@echo off
rem Gera dist\DigooLCD\DigooLCD.exe (precisa de Python 3.10+, .NET SDK 8 e as libs do requirements.txt)
setlocal
cd /d "%~dp0"

echo [1/4] Compilando o leitor de sensores (.NET / LibreHardwareMonitor)...
dotnet publish sensorhelper -c Release -o bin\sensorhelper --nologo || goto :erro
del /q bin\sensorhelper\*.pdb 2>nul

echo [2/4] Conferindo dependencias Python...
python -c "import PySide6, PIL, serial, psutil, mss, av, numpy, certifi, PyInstaller" 2>nul || python -m pip install -q -r requirements.txt pyinstaller || goto :erro

echo [3/4] Empacotando o programa...
rem sem --uac-admin: o atalho abre sem UAC e chama o app aberto (ou a tarefa de boot, que ja e admin)
python -m PyInstaller --noconfirm --clean --windowed ^
  --name DigooLCD --icon assets\icon.ico ^
  --add-data "assets;assets" ^
  --add-binary "bin\sensorhelper\sensorhelper.exe;bin\sensorhelper" ^
  --exclude-module PyQt5 --exclude-module tkinter --exclude-module customtkinter ^
  --exclude-module cv2 --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  --exclude-module pythonnet --exclude-module clr_loader ^
  --exclude-module setuptools --exclude-module pkg_resources --exclude-module distutils ^
  --exclude-module IPython --exclude-module ipykernel --exclude-module ipywidgets --exclude-module jupyter_client ^
  --exclude-module jupyter_core --exclude-module nbformat --exclude-module traitlets --exclude-module matplotlib_inline ^
  --exclude-module pytest --exclude-module _pytest --exclude-module py --exclude-module Cython --exclude-module pyximport ^
  --exclude-module jedi --exclude-module parso --exclude-module prompt_toolkit --exclude-module pygments ^
  --exclude-module zmq --exclude-module tornado --exclude-module debugpy --exclude-module trio ^
  --exclude-module lxml --exclude-module bs4 --exclude-module paramiko --exclude-module nacl --exclude-module invoke ^
  --exclude-module cryptography --exclude-module OpenSSL --exclude-module bcrypt --exclude-module requests ^
  --exclude-module urllib3 --exclude-module jsonschema --exclude-module rich --exclude-module lark --exclude-module regex ^
  --exclude-module jinja2 --exclude-module pydot --exclude-module pyparsing --exclude-module yaml --exclude-module arrow ^
  --exclude-module pyreadline3 --exclude-module curses --exclude-module pydoc ^
  --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuickWidgets --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.QtPdf --exclude-module PySide6.QtPdfWidgets ^
  --exclude-module PySide6.QtVirtualKeyboard ^
  --exclude-module PySide6.QtOpenGL --exclude-module PySide6.QtOpenGLWidgets ^
  DigooLCD.pyw || goto :erro

rem Qml/Quick/Pdf/VirtualKeyboard e o opengl32sw.dll (20 MB) sao copiados pelo hook nativo do PySide6,
rem nao pela analise de imports; o --exclude-module geralmente basta, mas varia por versao do PyInstaller.
rem Removemos os .dll mortos se sobreviverem (o app e 100%% QtWidgets/Fusion, nao carrega nada disso).
for %%d in (opengl32sw.dll Qt6Qml.dll Qt6Quick.dll Qt6QmlModels.dll Qt6QmlWorkerScript.dll Qt6Quick3D.dll Qt6VirtualKeyboard.dll Qt6Pdf.dll) do del /q "dist\DigooLCD\_internal\PySide6\%%d" 2>nul

echo [4/4] Compactando release com o numero da versao...
for /f "delims=" %%v in ('python -c "import digoo_lcd;print(digoo_lcd.__version__)"') do set "VER=%%v"
if not defined VER goto :skipzip
del /q "dist\DigooLCD-v%VER%-win64.zip" 2>nul
powershell -NoProfile -Command "Compress-Archive -Path 'dist\DigooLCD' -DestinationPath 'dist\DigooLCD-v%VER%-win64.zip' -Force" || goto :erro
echo Zip da release: dist\DigooLCD-v%VER%-win64.zip
:skipzip

echo.
echo Pronto: dist\DigooLCD\DigooLCD.exe
echo IMPORTANTE: rode dist\DigooLCD\DigooLCD.exe uma vez para confirmar que nenhum .dll removido fazia falta.
exit /b 0

:erro
echo.
echo *** Falhou. Veja a mensagem acima. ***
exit /b 1
