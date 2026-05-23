# Agente de Marketing — Novalyze
### Influencer Finder · Opción A (Web Search)

---

## Setup rápido

```bash
# 1. Mueve la carpeta a tu directorio de agentes
mv agente-marketing ~/mis-agentes/

# 2. Configura tu API key
export ANTHROPIC_API_KEY=sk-ant-tu-key-aqui

# 3. Da permisos y arranca
cd ~/mis-agentes/agente-marketing
chmod +x start.sh
./start.sh
```

Abre http://localhost:8002 en tu navegador.

---

## Estructura

```
agente-marketing/
├── main.py          # FastAPI backend
├── requirements.txt
├── start.sh         # Script de arranque
├── static/
│   └── index.html   # UI completa
└── README.md
```

---

## Filtros disponibles

| Filtro | Descripción |
|---|---|
| Plataforma | TikTok, Instagram, YouTube |
| Nicho | Texto libre (lifestyle, fitness, beauty...) |
| País | US, UK, CA, AU, MX, CO, ES, BR, AR |
| Seguidores | Rango mín/máx |
| Vistas promedio | Mínimo por video |
| Engagement % | Aproximado (exacto en Opción B) |
| Edad mínima | Del creador |
| Keywords | Palabras clave adicionales |
| Cantidad | 5, 10, 15, 20 resultados |

---

## Migración a Opción B (Phyllo/Modash)

Cuando tengas API key de Phyllo o Modash, solo debes:

1. Crear `providers/phyllo_provider.py`
2. Implementar la misma función `search_influencers(filters)`
3. Cambiar en `main.py`: `DATA_PROVIDER=phyllo` (variable de entorno)

La UI y los endpoints **no cambian**.

---

## Puerto y coexistencia

- Agente cotizaciones: puerto 5000
- Agente planos: puerto 8001  
- **Agente marketing: puerto 8002** ✅

---

## Deploy en Railway

```bash
# Mismo flujo que tus otros agentes
# Variables de entorno en Railway:
# ANTHROPIC_API_KEY=sk-ant-...
```
