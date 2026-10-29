@echo off
rem Gera dist\DigooLCD\DigooLCD.exe (precisa de Python 3.10+, .NET SDK 8 e as libs do requirements.txt)
setlocal
cd /d "%~dp0"

echo [1/3] Compilando o leitor de sensores (.NET / LibreHardwareMonitor)...
dotnet publish sensorhelper -c Release -o bin\sensorhelper --nologo || goto :erro
del /q bin\sensorhelper\*.pdb 2>nul

echo [2/3] Conferindo dependencias Python...
python -c "import PySide6, PIL, serial, psutil, mss, av, numpy, PyInstaller" 2>nul || python -m pip install -q -r requirements.txt pyinstaller || goto :erro

echo [3/3] Empacotando o programa...
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
  DigooLCD.pyw || goto :erro

echo.
echo Pronto: dist\DigooLCD\DigooLCD.exe
exit /b 0

:erro
echo.
echo *** Falhou. Veja a mensagem acima. ***
exit /b 1
