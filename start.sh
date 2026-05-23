#!/bin/bash
PYTHON=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  ╔═══════════════════════════════════════╗"
echo "  ║   Novalyze · Agente de Marketing      ║"
echo "  ║   Influencer Finder — Opción A        ║"
echo "  ╚═══════════════════════════════════════╝"
echo ""

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "  ⚠️  ANTHROPIC_API_KEY no está configurada"
  echo "  Ejecuta: export ANTHROPIC_API_KEY=sk-ant-..."
  echo ""
  exit 1
fi

cd "$DIR"

if [ ! -d "venv" ]; then
  echo "  📦 Creando entorno virtual..."
  $PYTHON -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt -q
  echo "  ✅ Dependencias instaladas"
else
  source venv/bin/activate
fi

echo "  🚀 Iniciando servidor en http://localhost:8002"
echo "  🌐 Abre tu navegador en: http://localhost:8002"
echo ""

uvicorn main:app --host 0.0.0.0 --port 8002 --reload
