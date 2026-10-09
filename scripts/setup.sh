#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

echo "==> Criando o ambiente virtual .venv"
"$PYTHON" -m venv .venv

echo "==> Instalando as dependências"
if ! .venv/bin/python -m pip install --upgrade pip; then
  echo >&2 "ERRO: não foi possível acessar o PyPI. Verifique sua internet/proxy."
  echo >&2 "Em rede corporativa, peça ao responsável o endereço do índice PyPI interno."
  echo >&2 "Depois execute: PIP_INDEX_URL=https://ENDERECO/simple ./scripts/setup.sh"
  exit 1
fi
if ! .venv/bin/python -m pip install -r requirements.txt; then
  echo >&2 "ERRO: as dependências não puderam ser baixadas."
  echo >&2 "Se a mensagem acima contém '403 Forbidden', sua rede/proxy bloqueou o PyPI."
  echo >&2 "Leia a seção 'Se aparecer 403 Forbidden' no README.md."
  exit 1
fi

echo "==> Executando os testes"
.venv/bin/python -m pytest -q
echo
echo "Pronto. Inicie com: .venv/bin/python app.py"
