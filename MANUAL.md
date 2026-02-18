# 🏠 Nido — Manual de Despliegue
## Guía paso a paso para publicar la web en Railway

---

## ¿Qué vas a necesitar?

- Cuenta en **GitHub** (gratis) → https://github.com
- Cuenta en **Railway** (gratis) → https://railway.app
- Tu **API Key de Anthropic** → https://console.anthropic.com
- *(Opcional)* Una cuenta de Gmail para las alertas por email

Tiempo estimado: **20-30 minutos**

---

## PASO 1 — Subir el proyecto a GitHub

1. Ve a https://github.com y haz login (o crea una cuenta gratis)
2. Haz clic en el botón verde **"New"** (arriba a la izquierda)
3. Ponle de nombre: `nido-inmobiliario`
4. Déjalo en **Public** y haz clic en **"Create repository"**
5. En la página del repositorio vacío, busca el link que dice **"uploading an existing file"**
6. **Arrastra toda la carpeta `nido-app`** al área de carga
7. Escribe un mensaje como "Primer commit" y haz clic en **"Commit changes"**

✅ Tu código ya está en GitHub.

---

## PASO 2 — Crear el proyecto en Railway

1. Ve a https://railway.app y haz clic en **"Login"** → usa tu cuenta de GitHub
2. Haz clic en **"New Project"**
3. Selecciona **"Deploy from GitHub repo"**
4. Busca y selecciona `nido-inmobiliario`
5. Railway detectará automáticamente que es un proyecto Python
6. Haz clic en **"Deploy Now"**

⏳ Railway tardará ~2 minutos en construir y desplegar la app.

---

## PASO 3 — Configurar las variables de entorno

> ⚠️ **MUY IMPORTANTE** — Sin este paso la IA y las alertas no funcionarán.

1. En Railway, haz clic en tu proyecto → pestaña **"Variables"**
2. Haz clic en **"New Variable"** y agrega estas variables una por una:

| Variable | Valor | Descripción |
|----------|-------|-------------|
| `ANTHROPIC_API_KEY` | tu-key-aqui | API Key de Anthropic (obligatorio para IA) |
| `SMTP_USER` | tu@gmail.com | Tu Gmail (para alertas por email) |
| `SMTP_PASS` | xxxx xxxx xxxx xxxx | Contraseña de aplicación de Gmail* |
| `SMTP_HOST` | smtp.gmail.com | Servidor de correo (no cambiar) |
| `SMTP_PORT` | 587 | Puerto SMTP (no cambiar) |

### ¿Cómo obtener la contraseña de aplicación de Gmail?
1. Entra a tu cuenta de Google → https://myaccount.google.com
2. Busca **"Seguridad"** → **"Verificación en 2 pasos"** (debe estar activada)
3. Al final de esa página busca **"Contraseñas de aplicaciones"**
4. Crea una nueva con nombre "Nido" → Google te dará una clave de 16 caracteres
5. Esa clave (con espacios) es tu `SMTP_PASS`

---

## PASO 4 — Obtener la URL de tu web

1. En Railway, ve a la pestaña **"Settings"** de tu proyecto
2. Busca **"Networking"** → **"Generate Domain"**
3. Railway te dará una URL como: `nido-inmobiliario-production.up.railway.app`

🎉 **¡Esa es tu web!** Ábrela en el navegador.

---

## PASO 5 — Usar la web

1. **Búsqueda**: Configura los filtros en el panel izquierdo y haz clic en "Buscar propiedades"
   - La búsqueda tarda ~30 segundos porque consulta varios portales y analiza con IA

2. **Favoritos**: Haz clic en ☆ en cualquier propiedad para guardarla
   - Ve a la pestaña "⭐ Favoritos" para verlas todas

3. **Comparador**: Marca ✓ en 2 o 3 propiedades y haz clic en "Comparar"
   - La IA te recomendará cuál comprar y por qué

4. **Alertas**: Ve a "🔔 Alertas" → "Nueva alerta"
   - Ingresa tu email y recibirás un correo automático cada 6 horas con propiedades nuevas

---

## Solución de problemas frecuentes

### La búsqueda no devuelve resultados
- Amplía el rango de precio o área
- Los portales a veces cambian su estructura — es normal, el scraper se adapta

### No llegan los emails de alerta
- Verifica que `SMTP_USER` y `SMTP_PASS` estén bien configurados en Railway
- Revisa la carpeta de Spam de tu correo
- La contraseña debe ser la de **aplicación** de Google, no tu contraseña normal

### La IA no analiza las propiedades
- Verifica que `ANTHROPIC_API_KEY` esté configurada en Railway
- Consigue tu key gratis en https://console.anthropic.com → "Create API Key"

### Railway detiene la app (plan gratuito)
- El plan gratuito de Railway tiene $5/mes de créditos gratis
- Si se acaban, actualiza a un plan de pago o usa Render.com como alternativa

---

## Actualizar la web con cambios

Si modificas algún archivo:
1. Vuelve a GitHub y sube los archivos modificados
2. Railway detectará el cambio y redesplegará automáticamente en ~1 minuto

---

## Estructura del proyecto

```
nido-app/
├── backend/
│   ├── main.py          ← El servidor (scrapers + API + alertas)
│   └── __init__.py
├── frontend/
│   └── index.html       ← La interfaz web completa
├── requirements.txt     ← Librerías de Python necesarias
├── Procfile             ← Comando de inicio para Railway
└── railway.toml         ← Configuración de Railway
```

---

## Soporte

Si tienes problemas, revisa los **logs** de Railway:
- Haz clic en tu proyecto → pestaña **"Deployments"** → haz clic en el deployment → **"View Logs"**
- Ahí verás cualquier error con detalle

---

*Nido — Agente Inmobiliario IA · Colombia*
