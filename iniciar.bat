@echo off
setlocal
cd /d "%~dp0"
title Ponto de Coleta Adelinha

echo ==============================================
echo   PONTO DE COLETA ADELINHA - INICIALIZACAO
echo ==============================================

where py >nul 2>nul
if errorlevel 1 (
  echo ERRO: Python nao foi encontrado.
  echo Instale o Python 3.11 ou mais recente e marque "Add Python to PATH".
  goto :erro
)

if not exist ".venv-local\Scripts\python.exe" (
  echo Criando ambiente virtual .venv-local...
  py -3 -m venv .venv-local
  if errorlevel 1 goto :erro
)

echo Instalando ou atualizando dependencias...
".venv-local\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :erro

echo.
echo Sistema disponivel em: http://localhost:5000
echo Para encerrar, pressione Ctrl+C.
echo.
".venv-local\Scripts\python.exe" app.py
if errorlevel 1 goto :erro
goto :fim

:erro
echo.
echo Nao foi possivel iniciar o sistema. Leia a mensagem acima.
pause
exit /b 1

:fim
endlocal
