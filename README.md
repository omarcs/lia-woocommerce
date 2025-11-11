# Pipeline de Sincronización WooCommerce ↔ Google Merchant Center

**Sistema completo de exportación de catálogos desde WooCommerce a Google Merchant Center con soporte para Local Inventory Ads (LIA).**

**API:** Google Content API v2.1  
**Lenguaje:** Python 3.8+  
**Base de Datos:** MySQL/MariaDB + WordPress + WooCommerce

---

## 📋 Tabla de Contenidos

1. [Descripción General](#-descripción-general)
2. [Prerequisitos](#-prerequisitos)
3. [Configuración de Google Cloud](#-configuración-de-google-cloud)
4. [Configuración de Google My Business](#-configuración-de-google-my-business)
5. [Configuración de Google Merchant Center](#-configuración-de-google-merchant-center)
6. [Configuración de WordPress y WooCommerce](#-configuración-de-wordpress-y-woocommerce)
7. [Instalación y Setup](#-instalación-y-setup)
8. [Configuración del Pipeline](#-configuración-del-pipeline)
9. [Ejecución del Pipeline](#️-ejecución-del-pipeline)
10. [Troubleshooting](#-troubleshooting)

---

## 📝 Descripción General

Este pipeline sincroniza productos de una tienda WooCommerce con dos canales de distribución en Google:

- **Canal ONLINE:** Productos visibles en búsqueda y shopping (audience: clientes en línea)
- **Canal LOCAL:** Productos con inventario local para Local Inventory Ads (audience: clientes cercanos a tiendas físicas)

### ¿Qué hace el pipeline?

1. **Lee productos de WooCommerce** usando taxonomía nativa de visibilidad
2. **Clasifica en ONLINE/LOCAL** según el término `exclude-from-catalog`
3. **Enriquece con inventario local** desde JSON multi-tienda
4. **Rastrea cambios** en tabla `wp_product_sync_tracking` (incremental sync)
5. **Envía a Google Content API v2.1** en batches con reintentos
6. **Detecta y elimina productos** que ya no existen

### Migración Importante

Este pipeline **ya fue migrado de Merchant API v1beta a Content API v2.1** (Scope: `content`, no `merchant.products`). Ver `framework_docs/iniciales/SOLUCION-CONTENT-API.md` para detalles técnicos.

---

## 🔧 Prerequisitos

### Requisitos de Sistema

- **Ubuntu 20.04+** o servidor Linux compatible
- **Python 3.8+**
- **MySQL/MariaDB 10.4+**
- **WordPress 5.9+** con WooCommerce 3.0+
- **Acceso root o sudo** para instalaciones globales

### Requisitos de Credenciales

- ✅ **Cuenta de Google Cloud Platform (GCP)** activa
- ✅ **Google Merchant Center** con cuenta de comerciante verificada
- ✅ **Google My Business** con ubicación(es) física(s) verificada(s)
- ✅ **Service Account JSON** de Google Cloud con permisos Content API

### Base de Datos

Una instalación funcional de **WordPress 5.9+** con **WooCommerce 3.0+** que tenga:
- Tabla `wp_posts` (productos)
- Tabla `wp_postmeta` (metadatos: SKU, precio, stock, imágenes)
- Tabla `wp_term_taxonomy` con taxonomía `product_visibility` configurada
- Tabla `wp_terms` con término `exclude-from-catalog` para productos locales

---

## ☁️ Configuración de Google Cloud

### Paso 1: Crear Proyecto en Google Cloud Console

1. Ve a [Google Cloud Console](https://console.cloud.google.com/)
2. Haz clic en el selector de proyectos (arriba a la izquierda)
3. Haz clic en **NUEVO PROYECTO**
4. Asigna un nombre (ej: `WooCommerce-GMC-Pipeline`)
5. Haz clic en **CREAR**
6. Espera a que se inicialize el proyecto

### Paso 2: Habilitar Content API v2.1

1. Ve a **APIs y servicios** → **Biblioteca**
2. Busca `Content API for Shopping`
3. Selecciona **Content API for Shopping v2.1**
4. Haz clic en **HABILITAR**
5. Espera a que se active (suele tardar 1-2 minutos)

### Paso 3: Crear Service Account

1. Ve a **APIs y servicios** → **Credenciales**
2. Haz clic en **CREAR CREDENCIALES** → **Cuenta de servicio**
3. Asigna nombre: `woocommerce-pipeline`
4. Asigna ID: `woocommerce-pipeline` (auto-generado)
5. Haz clic en **CREAR Y CONTINUAR**
6. **Paso 2 (opcional):** Haz clic en **CONTINUAR**
7. **Paso 3 (opcional):** Haz clic en **DONE**

### Paso 4: Crear JSON Key

1. Ve a **APIs y servicios** → **Credenciales**
2. Busca la cuenta de servicio `woocommerce-pipeline`
3. Haz clic en ella
4. Ve a pestaña **CLAVES**
5. Haz clic en **AGREGAR CLAVE** → **Crear clave nueva**
6. Selecciona **JSON**
7. Haz clic en **CREAR**
8. Se descargará automáticamente un archivo `woocommerce-pipeline-*.json`
9. **Guarda este archivo en un lugar seguro** (lo necesitarás más adelante)

```bash
# Copiar el archivo a la carpeta del pipeline (en el servidor)
scp ~/Downloads/woocommerce-pipeline-*.json user@server:/ruta/al/pipeline/service-account.json
```

### Paso 5: Habilitar Propietario del Proyecto a Service Account

1. Ve a **IAM y administración** → **Políticas**
2. Haz clic en **EDITAR POLÍTICAS DE ACCESO**
3. Haz clic en **AGREGAR**
4. En **Nuevos miembros**, pega el email de la Service Account:
   ```
   woocommerce-pipeline@<TU-PROJECT-ID>.iam.gserviceaccount.com
   ```
5. Asigna rol: **Editor** (o más restrictivo: **Roles personalizados** con solo permisos de Content API)
6. Haz clic en **GUARDAR**

---

## 🏪 Configuración de Google My Business

### Paso 1: Verificar Ubicación Física

Si aún no has verificado tu ubicación:

1. Ve a [Google My Business](https://business.google.com/)
2. Haz clic en **Administrador**
3. Selecciona o crea tu ubicación
4. Completa el perfil con:
   - Nombre exacto del negocio
   - Dirección completa
   - Teléfono
   - Horario de atención
5. Verifica la ubicación (por correo o teléfono)

### Paso 2: Obtener Store Code

El **Store Code** es el identificador único de tu ubicación en Google. Es necesario para Local Inventory Ads.

```bash
# El Store Code tiene formato: "GID123456789" o similar
# Lo encuentras en Google My Business:
# 1. Ve a tu ubicación en https://business.google.com/
# 2. Ve a Información → URL compartible
# 3. El código está en la URL: gid=XXXXXXXX
```

**O usa el prefijo sugerido en la configuración:**
```bash
STORE_CODE="MI-TIENDA-001"  # Puedes usar un código personalizado
```

---

## 💳 Configuración de Google Merchant Center

### Paso 1: Crear Cuenta de Merchant Center

1. Ve a [Google Merchant Center](https://merchants.google.com/)
2. Haz clic en **Crear cuenta**
3. Selecciona tu país (ej: México)
4. Ingresa tu nombre de empresa
5. Completa los datos de contacto
6. Verifica tu sitio web

### Paso 2: Obtener Merchant ID

1. Inicia sesión en [Google Merchant Center](https://merchants.google.com/)
2. Ve a **Configuración** → **Información de la cuenta**
3. Busca **ID de la cuenta de comerciante** (número de 10 dígitos)
4. Copia este número (lo necesitarás en la configuración del pipeline)

### Paso 3: Vincular Service Account

1. Ve a **Configuración** → **Acceso de usuarios**
2. Haz clic en **Administrar usuarios**
3. Haz clic en **INVITAR USUARIO**
4. Ingresa el email de tu Service Account:
   ```
   woocommerce-pipeline@<TU-PROJECT-ID>.iam.gserviceaccount.com
   ```
5. Asigna rol: **Editor**
6. Haz clic en **INVITAR**

### Paso 4: Habilitar Local Inventory Ads (LIA)

1. Ve a **Herramientas** → **Programas**
2. Busca **Local Inventory Ads**
3. Haz clic en **HABILITAR** (si no está habilitado)
4. Selecciona tu ubicación (importantísimo para LIA)
5. Completa los datos de la tienda física:
   - Dirección
   - Horario de atención
   - Teléfono
   - Categoría de productos

### Paso 5: Crear Feeds (ONLINE y LOCAL)

Los feeds son los "canales" de distribución en Google.

#### Feed ONLINE (Productos Visibles)

1. Ve a **Productos** → **Feeds**
2. Haz clic en **CREAR FEED**
3. Nombre: `ONLINE - Catálogo Web`
4. País: Tu país de operación (ej: México)
5. Lenguaje: `es` (español)
6. Tipo: **Productos**
7. Contenido: Este feed lo alimentará el pipeline automáticamente
8. Haz clic en **CREAR**

#### Feed LOCAL (Inventario Local para LIA)

1. Haz clic nuevamente en **CREAR FEED**
2. Nombre: `LOCAL - Inventario Tienda Física`
3. País: Tu país
4. Lenguaje: `es`
5. Tipo: **Inventario Local**
6. Tienda: Selecciona tu ubicación física verificada
7. Contenido: Este feed lo alimentará el pipeline para LIA
8. Haz clic en **CREAR**

**Nota:** Los feeds se llenarán automáticamente cuando ejecutes el pipeline.

---

## 🎯 Configuración de WordPress y WooCommerce

### Requisito Previo: Estructura de Productos

Tu tienda WooCommerce debe tener:

**Productos ONLINE:**
- SKU: `PROD-ON-001`, `PROD-ON-002`, etc.
- Visibilidad: **Visible en catálogo y búsqueda**
- Precio: Configurado
- Stock: Configurado
- Imágenes: Preferiblemente 1+ imagen

**Productos LOCAL (solo tienda física):**
- SKU: `PROD-LOC-001`, `PROD-LOC-002`, etc.
- Visibilidad: **Oculto en catálogo y búsqueda** (usa término `exclude-from-catalog`)
- Precio: Configurado
- Stock: Configurado en `local_stock.json` (ver paso 3)
- Imágenes: Preferiblemente 1+ imagen

### Paso 1: Instalar y Activar WooCommerce

Si ya no tienes WooCommerce instalado:

1. Ve a WordPress Admin → **Plugins** → **Añadir nuevo**
2. Busca `WooCommerce`
3. Haz clic en **Instalar ahora**
4. Haz clic en **Activar**
5. Completa el setup inicial de WooCommerce

### Paso 2: Crear Productos de Prueba

```bash
# Crear 5 productos ONLINE
for i in {1..5}; do
  wp post create \
    --post_type=product \
    --post_title="Producto Online $i" \
    --post_status=publish
done

# Crear 5 productos LOCAL (con visibilidad oculta)
for i in {1..5}; do
  wp post create \
    --post_type=product \
    --post_title="Producto Local $i" \
    --post_status=publish
done
```

O crear manualmente a través de WordPress Admin → **Productos** → **Añadir nuevo**

### Paso 3: Configurar Taxonomía de Visibilidad

WordPress/WooCommerce usa un sistema de **taxonomías** para manejar visibilidad:

- **Productos ONLINE** (visibles): NO tienen el término `exclude-from-catalog`
- **Productos LOCAL** (ocultos): TIENEN el término `exclude-from-catalog` en `product_visibility`

Para configurar manualmente:

1. Ve a WordPress Admin → **Productos** → **Editar producto**
2. Desplázate a **Visibilidad del producto**
3. **ONLINE:** Marcar **Visible en catálogo** y **Visible en búsqueda**
4. **LOCAL:** Desmarcar ambas (se agrega automáticamente `exclude-from-catalog`)
5. Haz clic en **Actualizar**

### Paso 4: Asignar SKUs y Precios

1. Ve a **Productos** → **Editar producto**
2. En el panel derecho, busca **Datos del producto** → **General**
3. Asigna:
   - **SKU:** `PROD-ON-001` (para online) o `PROD-LOC-001` (para local)
   - **Precio:** (en tu moneda)
   - **Stock:** (cantidad disponible para ONLINE; para LOCAL ver paso 5)
4. Haz clic en **Actualizar**

### Paso 5: Configurar Inventario Local (LOCAL)

Para productos LOCAL, el stock se configura en un archivo `local_stock.json`:

```json
{
  "MI-TIENDA-001": {
    "PROD-LOC-1": 10,
    "PROD-LOC-2": 5,
    "PROD-LOC-3": 12,
    "PROD-LOC-4": 7,
    "PROD-LOC-5": 8
  }
}
```

Este archivo se copiará al servidor en el paso de instalación del pipeline (ver sección "Instalación y Setup").

---

## 🚀 Instalación y Setup

### Paso 1: Descargar el Pipeline

```bash
# Opción 1: Clonar desde GitHub
git clone https://github.com/tuusuario/woocommerce-merchant-pipeline.git
cd woocommerce-merchant-pipeline

# Opción 2: Descargar archivo ZIP
wget https://github.com/tuusuario/woocommerce-merchant-pipeline/archive/refs/heads/main.zip
unzip main.zip
cd woocommerce-merchant-pipeline-main
```

### Paso 2: Crear Entorno Virtual

```bash
# Crear virtualenv
python3 -m venv .venv

# Activar
source .venv/bin/activate

# En Windows:
# .venv\Scripts\activate
```

### Paso 3: Instalar Dependencias

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### Paso 4: Copiar y Configurar .env

```bash
# Copiar archivo de ejemplo
cp .env.example .env

# Editar con tus valores
nano .env  # o tu editor favorito
```

**Variables requeridas en `.env`:**

```bash
# === MYSQL / WordPress ===
DB_HOST=localhost
DB_PORT=3306
DB_USER=wp_user
DB_PASSWORD=tu_contraseña_segura
DB_NAME=wordpress_db

# === Google Cloud ===
MERCHANT_ID=1234567890          # Tu ID de Merchant Center (10 dígitos)
STORE_CODE=MI-TIENDA-001        # Tu código de tienda (GMB o personalizado)
SERVICE_ACCOUNT_FILE_PATH=./service-account.json  # Ruta al JSON de Service Account

# === Comportamiento del Pipeline ===
DRY_RUN=true                    # true = no escribe en BD; false = actualiza tracking
BATCH_SIZE=50                   # Tamaño de lotes para Google API (max 100)

# === Archivo de Inventario Local ===
LOCAL_STOCK_FILE=./local_stock.json
```

### Paso 5: Copiar JSON de Service Account

```bash
# Copia el archivo descargado desde Google Cloud
cp ~/Downloads/woocommerce-pipeline-*.json ./service-account.json

# Verifica que exista
ls -la service-account.json
```

### Paso 6: Copiar local_stock.json

```bash
# Copia el archivo de ejemplo y edítalo con tus datos
cp local_stock.example.json local_stock.json

# Editar con tus SKUs y cantidades
nano local_stock.json
```

Formato:
```json
{
  "MI-TIENDA-001": {
    "PROD-LOC-1": 10,
    "PROD-LOC-2": 5
  }
}
```

### Paso 7: Inicializar Base de Datos

El pipeline usa una tabla `wp_product_sync_tracking` para rastrear cambios:

```bash
# Opción 1: Usar script Python
python init_database.py

# Opción 2: Aplicar SQL manualmente
mysql -u wp_user -p wordpress_db < init_database.sql
```

---

## ⚙️ Configuración del Pipeline

### Archivo .env Completo (Referencia)

```bash
# ============================================
# CONFIGURACIÓN DE BASE DE DATOS
# ============================================
DB_HOST=localhost
DB_PORT=3306
DB_USER=wp_user
DB_PASSWORD=ContraseñaSegura123!
DB_NAME=wordpress_db

# ============================================
# CONFIGURACIÓN DE GOOGLE CLOUD / MERCHANT
# ============================================
MERCHANT_ID=5572590833
STORE_CODE=MI-TIENDA-001
SERVICE_ACCOUNT_FILE_PATH=./service-account.json

# ============================================
# COMPORTAMIENTO DEL PIPELINE
# ============================================
DRY_RUN=true                # true=pruebas (sin escritura a BD); false=producción
BATCH_SIZE=50               # Tamaño de lote para Google API
LOCAL_STOCK_FILE=./local_stock.json

# ============================================
# CONFIGURACIÓN DE RUTA (Opcional)
# ============================================
LAST_SYNC_FILE=./.last_sync_timestamp.json  # Archivo de timestamp de última sync

```

---

## ▶️ Ejecución del Pipeline

### Prueba Segura (DRY-RUN)

```bash
# Activar virtualenv
source .venv/bin/activate

# PRUEBA SEGURA: modo dry-run (sin escritura a BD)
DRY_RUN=true python upload_to_merchant_api.py --debug --batch 50
```

### Ejecución Producción (Con Google Content API v2.1)

```bash
# Activar virtualenv
source .venv/bin/activate

# Si el dry-run funciona, ejecutar SIN dry-run
DRY_RUN=false python upload_to_merchant_api.py --debug --batch 50
```

### Opciones de Línea de Comandos

```bash
python upload_to_merchant_api.py [OPCIONES]

Opciones:
  --debug             Imprime logs detallados
  --batch SIZE        Tamaño de lote (default: 100, max: 100)
  --full              Sincronización completa (ignorar tracking)
  --skip-cleanup      Omitir detección de productos eliminados
```

### Ejemplos de Ejecución

```bash
# Sincronización incremental (solo cambios)
python upload_to_merchant_api.py

# Sincronización completa (todos los productos)
python upload_to_merchant_api.py --full

# Con logs detallados
python upload_to_merchant_api.py --debug

# Con batch tamaño 25
python upload_to_merchant_api.py --batch 25

# Combinado
python upload_to_merchant_api.py --debug --full --batch 50
```

### Salida Esperada

```
================================================================================
INICIO: Pipeline WooCommerce → Google Content API v2.1
Store Code: MI-TIENDA-001 | Batch Size: 50
================================================================================
Inicializando Google Content API v2.1...
✓ Google Content API v2.1 inicializado (MIGRACION desde v1beta)
Obteniendo productos que necesitan sincronización...
✓ Productos que necesitan sync: 112
✓ Total: 112 | Online: 56 | Locales: 56
Procesando 56 productos Online...
Procesando 56 productos Locales...
Detectando productos eliminados...
✓ Timestamp de sincronización guardado: 2025-11-11 14:30:45

================================================================================
FIN: Pipeline completado exitosamente
Duración: 4.23 segundos
================================================================================

============================================================
                 ESTADÍSTICAS DEL PIPELINE                  
============================================================
  Total procesados:           112
  Válidos:                    112
  Inválidos:                    0
  Errores:                      0
------------------------------------------------------------
  Con precios válidos:        112
  Con imágenes válidas:       112
  Con inventario:             112
------------------------------------------------------------
  Enviados (Online):           56
  Enviados (Local):            56
============================================================
```

### Automatizar con Cron (Linux)

Para ejecutar el pipeline automáticamente cada hora:

```bash
# Editar crontab
crontab -e

# Añadir esta línea:
0 * * * * cd /ruta/al/pipeline && source .venv/bin/activate && python upload_to_merchant_api.py >> pipeline.log 2>&1
```

---

## 🐛 Troubleshooting

### Problema: "No se puede conectar a MySQL"

```
Error: MySQL Connection Error: Access denied for user 'wp_user'@'localhost'
```

**Soluciones:**
1. Verifica credenciales en `.env` (DB_USER, DB_PASSWORD, DB_HOST)
2. Verifica que MySQL esté corriendo: `systemctl status mysql`
3. Verifica permisos en MySQL:
   ```sql
   mysql -u root -p
   SELECT User, Host FROM mysql.user WHERE User='wp_user';
   GRANT ALL PRIVILEGES ON wordpress_db.* TO 'wp_user'@'localhost';
   FLUSH PRIVILEGES;
   ```

### Problema: "Token inválido o expirado (401)"

```
Error: Token inválido o expirado (401). Intento 1 de 7
```

**Soluciones:**
1. Regenera el JSON de Service Account (ver Paso 4 de "Configuración de Google Cloud")
2. Verifica que el archivo service-account.json exista y sea válido:
   ```bash
   cat service-account.json | jq '.type'  # Debe imprimir "service_account"
   ```
3. Verifica MERCHANT_ID en `.env` (debe ser un número de 10 dígitos)
4. Verifica que la Service Account tenga permisos en Merchant Center (ver "Paso 3: Vincular Service Account")

### Problema: "Archivo local_stock.json no encontrado"

```
Error obteniendo stock local del JSON: [Errno 2] No such file or directory: './local_stock.json'
```

**Solución:**
```bash
cp local_stock.example.json local_stock.json
# Editar local_stock.json con tus datos
nano local_stock.json
```

### Problema: "Tabla wp_product_sync_tracking no existe"

```
Error obteniendo productos: Table 'wordpress_db.wp_product_sync_tracking' doesn't exist
```

**Solución:**
```bash
python init_database.py
# o
mysql -u wp_user -p wordpress_db < init_database.sql
```

### Problema: "Ningún producto fue sincronizado"

Si ejecutas el pipeline y no se sincronizan productos:

1. Verifica que existan productos en WooCommerce:
   ```bash
   mysql -u wp_user -p wordpress_db -e "SELECT COUNT(*) FROM wp_posts WHERE post_type='product';"
   ```

2. Verifica que tengan SKU:
   ```bash
   mysql -u wp_user -p wordpress_db -e "SELECT COUNT(*) FROM wp_postmeta WHERE meta_key='_sku';"
   ```

3. Ejecuta con --full para forzar sincronización completa:
   ```bash
   python upload_to_merchant_api.py --debug --full
   ```

4. Revisa logs:
   ```bash
   python upload_to_merchant_api.py --debug 2>&1 | tee pipeline.log
   ```

### Problema: Productos no aparecen en Google Merchant Center

1. Verifica que el feed esté configurado:
   - Ve a Google Merchant Center → Productos → Feeds
   - Confirma que existan feeds ONLINE y LOCAL

2. Espera 1-2 horas (Google tarda en procesar)

3. Verifica el estado en Merchant Center:
   - Ve a Productos → Diagnósticos
   - Busca errores o advertencias

---

## 📄 Licencia

MIT - Ver LICENSE para más detalles

## ⚠️ Notas de Seguridad

- **NUNCA** comitas `.env` o `service-account.json` a Git
- **NUNCA** compartas credenciales en público
- Usa permisos mínimos en Service Account (solo Content API)
- Cambia contraseñas de MySQL regularmente
- Usa HTTPS para todas las comunicaciones

## 🤝 Soporte

Para reportar bugs o contribuir:
1. Crea un issue en GitHub
2. Incluye logs (ejecuta con `--debug`)
3. Incluye tu versión de Python y MySQL
4. NO incluyas credenciales ni datos sensibles
