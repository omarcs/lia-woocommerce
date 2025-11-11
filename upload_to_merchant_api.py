#!/usr/bin/env python3
# ============================================================================
# Pipeline: upload_to_merchant_api.py (v1.0.0)
# Propósito: Sincronizar productos WooCommerce → Google Merchant Content API v2.1
# ============================================================================
# Uso:
#   python3 upload_to_merchant_api.py                    (modo incremental)
#   python3 upload_to_merchant_api.py --debug            (con logs detallados)
#   python3 upload_to_merchant_api.py --full             (sincronización completa)
#   python3 upload_to_merchant_api.py --batch 50         (tamaño de lote customizado)
#   python3 upload_to_merchant_api.py --skip-cleanup     (omitir limpieza de eliminados)
# ============================================================================

import os
import sys
import argparse
import logging
import json
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from dotenv import load_dotenv

# Importar utilidades del framework
try:
    from app.utils import (
        setup_logging,
        retry_with_backoff,
        ValidationStatus,
        BatchProcessor,
        PipelineStats
    )
except ImportError:
    from utils import (
        setup_logging,
        retry_with_backoff,
        ValidationStatus,
        BatchProcessor,
        PipelineStats
    )

# APIs externas
import mysql.connector
from mysql.connector import Error

# Los imports de Google Merchant API se mueven dentro de las funciones que los requieren

# ============================================================================
# CONFIGURACIÓN GLOBAL
# ============================================================================

# Variables de entorno
load_dotenv('/home/devlia/app_pipeline/.env')

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_NAME = os.getenv('DB_NAME')

MERCHANT_ID = os.getenv('MERCHANT_ID')
STORE_CODE = os.getenv('STORE_CODE', 'MI-TIENDA-001')
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE_PATH', '/home/devlia/app_pipeline/service-account.json')

# Tracking de modificaciones
LAST_SYNC_FILE = os.getenv('LAST_SYNC_FILE', '/home/devlia/app_pipeline/.last_sync_timestamp.json')

# Logger global (se inicializa en main())
logger: Optional[logging.Logger] = None

# ============================================================================
# TRACKING DE MODIFICACIONES
# ============================================================================

def get_last_sync_timestamp() -> Optional[str]:
    """
    Obtiene el timestamp de la última sincronización exitosa.
    
    Returns:
        Timestamp en formato 'YYYY-MM-DD HH:MM:SS' o None si no existe
    """
    if not os.path.exists(LAST_SYNC_FILE):
        logger.info("No existe historial de sincronización (primera ejecución)")
        return None
    
    try:
        with open(LAST_SYNC_FILE, 'r') as f:
            data = json.load(f)
            timestamp = data.get('last_sync')
            if timestamp:
                logger.info(f"Última sincronización: {timestamp}")
            return timestamp
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error leyendo timestamp de última sync: {e}")
        return None


def save_last_sync_timestamp(timestamp: str = None):
    """
    Guarda el timestamp de la sincronización exitosa.
    
    Args:
        timestamp: Timestamp en formato 'YYYY-MM-DD HH:MM:SS' 
                   (default: timestamp actual)
    """
    if timestamp is None:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        data = {
            'last_sync': timestamp,
            'updated_at': datetime.now().isoformat()
        }
        with open(LAST_SYNC_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info(f"✓ Timestamp de sincronización guardado: {timestamp}")
    except IOError as e:
        logger.error(f"Error guardando timestamp de sincronización: {e}")


# ============================================================================
# TRACKING DE PRODUCTOS (Tabla wp_product_sync_tracking)
# ============================================================================

def get_products_needing_sync(force_full: bool = False) -> Tuple[List[Dict], List[Dict]]:
    """
    Obtiene productos que necesitan sincronización basándose en la tabla de tracking.
    
    Args:
        force_full: Si es True, ignora tracking y retorna todos los productos
    
    Detecta 3 casos (si force_full=False):
    1. Productos nuevos (no están en tracking)
    2. Productos modificados (last_modified_at > last_sent_at)
    3. Productos con errores que requieren reintento
    
    Si force_full=True: Retorna todos los productos publicados
    
    Returns:
        Tupla (online_products, local_products) que necesitan sync
    """
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor(dictionary=True)
        
        if force_full:
            # Modo FULL: Obtener TODOS los productos publicados
            query = """
            SELECT 
                p.ID as product_id,
                p.post_title as name,
                p.post_modified as last_modified,
                COALESCE(sku_meta.meta_value, '') as sku,
                COALESCE(price_meta.meta_value, '0') as price,
                CASE 
                    WHEN EXISTS (
                        SELECT 1 
                        FROM wp_term_relationships tr2
                        JOIN wp_term_taxonomy tt2 ON tr2.term_taxonomy_id = tt2.term_taxonomy_id
                        JOIN wp_terms t2 ON tt2.term_id = t2.term_id
                        WHERE tr2.object_id = p.ID
                          AND tt2.taxonomy = 'product_visibility'
                          AND t2.slug = 'exclude-from-catalog'
                    ) THEN 'hidden'
                    ELSE 'visible'
                END as catalog_visibility,
                COALESCE(stock_meta.meta_value, '0') as stock_quantity,
                COALESCE(status_meta.meta_value, 'instock') as stock_status,
                COALESCE(image_meta.meta_value, '') as image_url
            FROM wp_posts p
            LEFT JOIN wp_postmeta sku_meta ON (p.ID = sku_meta.post_id AND sku_meta.meta_key = '_sku')
            LEFT JOIN wp_postmeta price_meta ON (p.ID = price_meta.post_id AND price_meta.meta_key = '_price')
            LEFT JOIN wp_postmeta stock_meta ON (p.ID = stock_meta.post_id AND stock_meta.meta_key = '_stock_quantity')
            LEFT JOIN wp_postmeta status_meta ON (p.ID = status_meta.post_id AND status_meta.meta_key = '_stock_status')
            LEFT JOIN wp_postmeta image_meta ON (p.ID = image_meta.post_id AND image_meta.meta_key = '_product_image_url')
            WHERE p.post_type = 'product' 
              AND p.post_status = 'publish'
              AND sku_meta.meta_value IS NOT NULL
            ORDER BY p.ID ASC;
            """
        else:
            # Modo INCREMENTAL: Solo productos que necesitan sincronización
            # - Productos nuevos (LEFT JOIN donde tracking.id IS NULL)
            # - Productos modificados (post_modified > last_sent_at)
            # - Productos con errores (sync_status = 'failed' y error_count < 5)
            query = """
            SELECT 
                p.ID as product_id,
                p.post_title as name,
                p.post_modified as last_modified,
                COALESCE(sku_meta.meta_value, '') as sku,
                COALESCE(price_meta.meta_value, '0') as price,
                CASE 
                    WHEN EXISTS (
                        SELECT 1 
                        FROM wp_term_relationships tr2
                        JOIN wp_term_taxonomy tt2 ON tr2.term_taxonomy_id = tt2.term_taxonomy_id
                        JOIN wp_terms t2 ON tt2.term_id = t2.term_id
                        WHERE tr2.object_id = p.ID
                          AND tt2.taxonomy = 'product_visibility'
                          AND t2.slug = 'exclude-from-catalog'
                    ) THEN 'hidden'
                    ELSE 'visible'
                END as catalog_visibility,
                COALESCE(stock_meta.meta_value, '0') as stock_quantity,
                COALESCE(status_meta.meta_value, 'instock') as stock_status,
                COALESCE(image_meta.meta_value, '') as image_url,
                t.last_sent_at,
                t.sync_status,
                t.error_count
            FROM wp_posts p
            LEFT JOIN wp_postmeta sku_meta ON (p.ID = sku_meta.post_id AND sku_meta.meta_key = '_sku')
            LEFT JOIN wp_postmeta price_meta ON (p.ID = price_meta.post_id AND price_meta.meta_key = '_price')
            LEFT JOIN wp_postmeta stock_meta ON (p.ID = stock_meta.post_id AND stock_meta.meta_key = '_stock_quantity')
            LEFT JOIN wp_postmeta status_meta ON (p.ID = status_meta.post_id AND status_meta.meta_key = '_stock_status')
            LEFT JOIN wp_postmeta image_meta ON (p.ID = image_meta.post_id AND image_meta.meta_key = '_product_image_url')
            LEFT JOIN wp_product_sync_tracking t ON (p.ID = t.product_id AND sku_meta.meta_value = t.sku)
            WHERE p.post_type = 'product' 
              AND p.post_status = 'publish'
              AND sku_meta.meta_value IS NOT NULL
              AND (
                  t.id IS NULL  -- Producto nuevo (no está en tracking)
                  OR p.post_modified > t.last_sent_at  -- Producto modificado
                  OR (t.sync_status = 'failed' AND t.error_count < 5)  -- Error pero aún reintentable
              )
            ORDER BY p.ID ASC;
            """
        
        cursor.execute(query)
        rows = cursor.fetchall()
        
        # Convertir a formato esperado
        all_products = []
        for row in rows:
            stock_qty = row['stock_quantity'] if row['stock_quantity'] else 0
            product = {
                'id': row['product_id'],
                'name': row['name'],
                'sku': row['sku'],
                'price': str(row['price']),
                'catalog_visibility': row['catalog_visibility'],
                'stock_quantity': int(stock_qty) if stock_qty else 0,
                'stock_status': row['stock_status'],
                'image_url': row['image_url'],
                'last_modified': row['last_modified']
            }
            all_products.append(product)
        
        cursor.close()
        conn.close()
        
        logger.info(f"✓ Productos que necesitan sync: {len(all_products)}")
        
    except Error as e:
        logger.error(f"Error obteniendo productos que necesitan sync: {e}")
        return ([], [])
    
    # Categorizar por visibilidad
    online_products = [
        p for p in all_products
        if p.get('catalog_visibility') in ['visible', 'catalog', 'search', '']
    ]
    local_products = [
        p for p in all_products
        if p.get('catalog_visibility') == 'hidden'
    ]
    
    logger.info(f"✓ Total: {len(all_products)} | Online: {len(online_products)} | Locales: {len(local_products)}")
    return (online_products, local_products)


def update_sync_tracking(product_id: int, sku: str, channel: str, 
                        success: bool, merchant_product_id: str = None,
                        error_message: str = None):
    """
    Actualiza o crea registro de tracking para un producto.
    
    Args:
        product_id: ID del producto en wp_posts
        sku: SKU del producto
        channel: 'online' o 'local'
        success: True si el envío fue exitoso
        merchant_product_id: ID del producto en Google (ej: online:SKU-123)
        error_message: Mensaje de error si success=False
    """
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor()
        
        # Obtener last_modified actual del producto
        cursor.execute("""
            SELECT post_modified FROM wp_posts WHERE ID = %s
        """, (product_id,))
        result = cursor.fetchone()
        
        if not result:
            logger.warning(f"Producto ID {product_id} no encontrado en wp_posts")
            return
        
        last_modified = result[0]
        
        if success:
            # Sync exitoso: actualizar o insertar con estado 'synced'
            query = """
            INSERT INTO wp_product_sync_tracking 
                (product_id, sku, channel, last_sent_at, last_modified_at, 
                 sync_status, merchant_product_id, error_count, last_error)
            VALUES 
                (%s, %s, %s, NOW(), %s, 'synced', %s, 0, NULL)
            ON DUPLICATE KEY UPDATE
                last_sent_at = NOW(),
                last_modified_at = %s,
                sync_status = 'synced',
                merchant_product_id = %s,
                error_count = 0,
                last_error = NULL
            """
            cursor.execute(query, (
                product_id, sku, channel, last_modified, merchant_product_id,
                last_modified, merchant_product_id
            ))
        else:
            # Sync fallido: incrementar error_count y guardar mensaje
            query = """
            INSERT INTO wp_product_sync_tracking 
                (product_id, sku, channel, last_sent_at, last_modified_at, 
                 sync_status, merchant_product_id, error_count, last_error)
            VALUES 
                (%s, %s, %s, NOW(), %s, 'failed', %s, 1, %s)
            ON DUPLICATE KEY UPDATE
                last_modified_at = %s,
                sync_status = 'failed',
                error_count = error_count + 1,
                last_error = %s
            """
            cursor.execute(query, (
                product_id, sku, channel, last_modified, merchant_product_id, error_message,
                last_modified, error_message
            ))
        
        conn.commit()
        cursor.close()
        conn.close()
        
    except Error as e:
        logger.error(f"Error actualizando tracking para SKU {sku}: {e}")


def get_deleted_products() -> List[Dict]:
    """
    Detecta productos que están en tracking pero ya no existen en wp_posts.
    
    Returns:
        Lista de dicts con: {product_id, sku, channel, merchant_product_id}
    """
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor(dictionary=True)
        
        # LEFT JOIN: productos en tracking pero no en wp_posts (eliminados)
        query = """
        SELECT 
            t.product_id,
            t.sku,
            t.channel,
            t.merchant_product_id
        FROM wp_product_sync_tracking t
        LEFT JOIN wp_posts p ON t.product_id = p.ID
        WHERE p.ID IS NULL 
          AND t.sync_status != 'deleted'
        """
        
        cursor.execute(query)
        deleted_products = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        if deleted_products:
            logger.warning(f"⚠️  Detectados {len(deleted_products)} productos eliminados")
        
        return deleted_products
        
    except Error as e:
        logger.error(f"Error detectando productos eliminados: {e}")
        return []


def mark_product_as_deleted(product_id: int, sku: str):
    """
    Marca un producto como 'deleted' en la tabla de tracking.
    
    Args:
        product_id: ID del producto
        sku: SKU del producto
    """
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor()
        
        query = """
        UPDATE wp_product_sync_tracking
        SET sync_status = 'deleted',
            updated_at = NOW()
        WHERE product_id = %s AND sku = %s
        """
        
        cursor.execute(query, (product_id, sku))
        conn.commit()
        
        cursor.close()
        conn.close()
        
        logger.info(f"✓ Producto {sku} marcado como eliminado en tracking")
        
    except Error as e:
        logger.error(f"Error marcando producto {sku} como eliminado: {e}")


# ============================================================================
# VALIDACIÓN DE ENTORNO
# ============================================================================

def validate_env() -> bool:
    """
    Valida que todas las variables de entorno requeridas estén definidas.
    CAMBIO: Ya no requiere WC_URL, WC_KEY, WC_SECRET (usamos SQL directo)
    
    Returns:
        True si válido, False en caso contrario
    """
    required_vars = {
        'DB_USER': DB_USER,
        'DB_PASSWORD': DB_PASSWORD,
        'DB_NAME': DB_NAME,
        'MERCHANT_ID': MERCHANT_ID,
    }
    
    missing_vars = [var for var, value in required_vars.items() if not value]
    
    if missing_vars:
        logger.error(f"Variables de entorno faltantes: {', '.join(missing_vars)}")
        return False
    
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        logger.error(f"Archivo de Service Account no encontrado: {SERVICE_ACCOUNT_FILE}")
        return False
    
    logger.info("✓ Variables de entorno validadas")
    return True


# ============================================================================
# LECTURA DE PRODUCTOS - VERSIÓN SQL (sin API REST)
# ============================================================================

def fetch_products_from_db(debug_mode: bool = False, since_timestamp: Optional[str] = None) -> Tuple[List[Dict], List[Dict]]:
    """
    Obtiene productos directamente de la BD WordPress.
    
    Args:
        debug_mode: Si True, imprime logs detallados
        since_timestamp: Si se proporciona, solo obtiene productos modificados después de este timestamp
                        Formato: 'YYYY-MM-DD HH:MM:SS'
    
    Returns:
        Tupla (online_products, local_products) con dicts
    """
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        
        cursor = conn.cursor(dictionary=True)
        
        # Consulta SQL que une posts + postmeta para obtener todos los datos
        query = """
        SELECT 
            p.ID as product_id,
            p.post_title as name,
            p.post_modified as last_modified,
            COALESCE(sku_meta.meta_value, '') as sku,
            COALESCE(price_meta.meta_value, '0') as price,
            CASE 
                WHEN sku_meta.meta_value LIKE 'PROD-LOC%' THEN 'hidden'
                WHEN sku_meta.meta_value LIKE 'PROD-ON%' THEN 'visible'
                ELSE 'visible'
            END as catalog_visibility,
            COALESCE(stock_meta.meta_value, '0') as stock_quantity,
            COALESCE(status_meta.meta_value, 'instock') as stock_status,
            COALESCE(image_meta.meta_value, '') as image_url
        FROM wp_posts p
        LEFT JOIN wp_postmeta sku_meta ON (p.ID = sku_meta.post_id AND sku_meta.meta_key = '_sku')
        LEFT JOIN wp_postmeta price_meta ON (p.ID = price_meta.post_id AND price_meta.meta_key = '_price')
        LEFT JOIN wp_postmeta stock_meta ON (p.ID = stock_meta.post_id AND stock_meta.meta_key = '_stock_quantity')
        LEFT JOIN wp_postmeta status_meta ON (p.ID = status_meta.post_id AND status_meta.meta_key = '_stock_status')
        LEFT JOIN wp_postmeta image_meta ON (p.ID = image_meta.post_id AND image_meta.meta_key = '_product_image_url')
        WHERE p.post_type = 'product' 
          AND p.post_status = 'publish'
          AND sku_meta.meta_value IS NOT NULL
        """
        
        # Agregar filtro de timestamp si se proporciona
        if since_timestamp:
            query += " AND p.post_modified > %s"
            cursor.execute(query + " ORDER BY p.ID ASC;", (since_timestamp,))
            logger.info(f"🔄 Modo incremental: solo productos modificados después de {since_timestamp}")
        else:
            query += " ORDER BY p.ID ASC;"
            cursor.execute(query)
            logger.info("🔄 Modo completo: procesando todos los productos")
        
        rows = cursor.fetchall()
        
        # Los resultados ya son dicts porque usamos cursor(dictionary=True)
        all_products = []
        for row in rows:
            stock_qty = row['stock_quantity'] if row['stock_quantity'] else 0
            product = {
                'id': row['product_id'],
                'name': row['name'],
                'sku': row['sku'],
                'price': str(row['price']),
                'catalog_visibility': row['catalog_visibility'],  # Ya clasificado por SKU en SQL
                'stock_quantity': int(stock_qty),
                'stock_status': row['stock_status'],
                'image_url': row['image_url']
            }
            all_products.append(product)
        
        cursor.close()
        conn.close()
        
        if debug_mode:
            logger.debug(f"✓ Consultados {len(all_products)} productos de la BD")
        
    except Error as e:
        logger.error(f"Error consultando BD para productos: {e}")
        return ([], [])
    
    # Categorizar por visibilidad
    online_products = [
        p for p in all_products
        if p.get('catalog_visibility') in ['visible', 'catalog', 'search', '']
    ]
    local_products = [
        p for p in all_products
        if p.get('catalog_visibility') == 'hidden'
    ]
    
    logger.info(f"✓ Total: {len(all_products)} | Online: {len(online_products)} | Locales: {len(local_products)}")
    return (online_products, local_products)


def init_google_clients():
    """
    Inicializa Google Content API v2.1 con reintentos.
    
    Returns:
        Tupla (service, 200) o (None, 500) en error
    """
    def connect():
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
            
            scopes = [
                'https://www.googleapis.com/auth/content'
            ]
            credentials = Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE,
                scopes=scopes
            )
            service = build('content', 'v2.1', credentials=credentials)
            logger.info("✓ Google Content API v2.1 inicializado")
            return (True, service, 200)
        except Exception as e:
            logger.error(f"Error al inicializar Google Content API v2.1: {e}")
            return (False, None, 500)
    return retry_with_backoff(connect, max_retries=5, debug_mode=False)


# ============================================================================
# OBTENCIÓN DE DATOS
# ============================================================================


# DEPRECATED: fetch_woocommerce_products() fue reemplazada por fetch_products_from_db()
# Ya no necesitamos API REST de WooCommerce, usamos SQL directo


def fetch_local_stock_from_json() -> Optional[Dict[str, int]]:
    """
    Obtiene stock local desde archivo JSON con reintentos.
    
    Returns:
        Diccionario {SKU: stock_quantity} o None si falla
    """
    def get_stock():
        try:
            import json
            
            stock_file = os.getenv('LOCAL_STOCK_FILE', '/home/devlia/app_pipeline/local_stock.json')
            if not os.path.exists(stock_file):
                logger.error(f"Archivo de stock local no encontrado: {stock_file}")
                return (False, None, 404)
            
            with open(stock_file, 'r') as f:
                stock_data = json.load(f)
            
            # Estructura: {"TIENDA-001": {SKU: qty}, "TIENDA-002": {SKU: qty}}
            # Este JSON es solo para productos locales (LIA - Local Inventory Ads)
            # Formato de salida: {SKU: {"store_code": qty}}
            stock_dict = {}
            
            for store_code, products in stock_data.items():
                for sku, qty in products.items():
                    if sku not in stock_dict:
                        stock_dict[sku] = {}
                    stock_dict[sku][store_code] = qty
            
            total_skus = len(stock_dict)
            total_stores = len(stock_data)
            logger.info(f"✓ Stock local cargado: {total_skus} SKUs en {total_stores} tiendas")
            return (True, stock_dict, 200)
        except Exception as e:
            logger.error(f"Error obteniendo stock local del JSON: {e}")
            return (False, None, 500)
    
    return retry_with_backoff(get_stock, max_retries=5, debug_mode=False)


# ============================================================================
# TRANSFORMACIÓN Y VALIDACIÓN
# ============================================================================

def price_to_micros(price_str: str) -> int:
    """
    Convierte precio string a micros (entero para Google API).
    
    Args:
        price_str: Precio como string (ej. "299.00")
    
    Returns:
        Precio en micros
    """
    try:
        price_float = float(price_str)
        return int(price_float * 1000000)
    except (ValueError, TypeError):
        logger.warning(f"Precio inválido: {price_str}, usando 0")
        return 0


def validate_product(wc_product: Dict) -> Tuple[bool, ValidationStatus]:
    """
    Valida un producto de WooCommerce con múltiples criterios.
    
    Args:
        wc_product: Diccionario con datos del producto
    
    Returns:
        Tupla (es_válido, ValidationStatus)
    """
    validation = ValidationStatus()
    
    # Validar título
    title = wc_product.get('name', '').strip()
    if title:
        validation.title_valid = True
    else:
        logger.warning(f"Producto sin título: SKU={wc_product.get('sku')}")
        return (False, validation)
    
    # Validar precio
    price = float(wc_product.get('price', 0))
    if price > 0:
        validation.price_valid = True
    else:
        logger.warning(f"Producto sin precio válido: SKU={wc_product.get('sku')}")
        return (False, validation)
    
    # Validar imágenes (siempre válido porque usamos placeholder si falta)
    images = wc_product.get('images', [])
    if images:
        validation.images_valid = True
    else:
        # Usaremos placeholder, por lo que es válido
        validation.images_valid = True
        if logger:
            logger.debug(f"Producto sin imagen, usando placeholder: SKU={wc_product.get('sku')}")
    
    # Validar inventario
    if wc_product.get('stock_status') == 'instock':
        validation.inventory_valid = True
    
    return (True, validation)


def wc_product_to_content_api_entry(
    wc_product: Dict,
    channel: str = 'online',
    batch_id: int = 1,
    store_code: str = 'TIENDA-001'
) -> Optional[Dict]:
    """
    Convierte producto WooCommerce a formato de entrada para Content API v2.1.
    
    Args:
        wc_product: Diccionario del producto
        channel: Canal ('online' o 'local')
        batch_id: ID del batch (para correlación)
        store_code: Código de tienda (ej: TIENDA-001)
    
    Returns:
        Dict con formato custombatch entry o None si inválido
    """
    # Validar primero
    is_valid, validation = validate_product(wc_product)
    if not is_valid:
        return None
    
    try:
        # Usar imagen de la BD o placeholder si está vacía
        PLACEHOLDER_IMAGE = 'https://devlia.l3m.mx/wp-content/uploads/woocommerce-placeholder.webp'
        image_link = wc_product.get('image_url') or PLACEHOLDER_IMAGE
        if not image_link or image_link.strip() == '':
            image_link = PLACEHOLDER_IMAGE
        
        availability = 'in stock' if wc_product.get('stock_status') == 'instock' else 'out of stock'
        
        # Content API v2.1 requiere precio en formato string (no micros)
        price_value = float(wc_product['price'])
        
        # Generar permalink si no existe
        permalink = wc_product.get('permalink', f"https://devlia.l3m.mx/producto/{wc_product.get('sku', 'sin-sku')}/")
        
        # Crear ID para Content API: channel:language:country:sku
        # Ejemplo: online:es:MX:SKU-001
        product_id = f"{channel.lower()}:es:MX:{wc_product['sku']}"
        
        # Estructura de entry para custombatch de Content API v2.1
        entry = {
            'batchId': batch_id,
            'merchantId': MERCHANT_ID,
            'method': 'insert',
            'product': {
                'id': product_id,
                'offerId': wc_product['sku'],
                'title': wc_product['name'][:150],
                'description': wc_product.get('description', '')[:5000],
                'link': permalink,
                'imageLink': image_link,
                'price': {
                    'value': f"{price_value:.2f}",
                    'currency': 'MXN'
                },
                'availability': availability,
                'channel': channel.upper(),  # ONLINE o LOCAL
                'contentLanguage': 'es',
                'targetCountry': 'MX',
                'condition': 'new'
            }
        }
        
        return entry
    except Exception as e:
        logger.error(f"Error transformando producto {wc_product.get('sku')} a Content API: {e}")
        return None


# ============================================================================
# ENVÍO A GOOGLE API
# ============================================================================

def upload_product_batch(
    service,
    products: List[Dict],
    channel: str = 'online',
    stats: Optional[PipelineStats] = None,
    debug_mode: bool = False,
    batch_size: int = 100,
    stock_dict: Optional[Dict] = None
) -> int:
    """
    Sube lotes de productos a Google Content API v2.1 usando custombatch.
    MIGRACIÓN: De insert_product_input() → custombatch()
    REFERENCIA: framework_docs/iniciales/SOLUCION-CONTENT-API.md
    
    Args:
        service: Servicio de Google Content API v2.1
        products: Lista de diccionarios de productos
        channel: Canal ('online' o 'local')
        stats: Objeto de estadísticas
        debug_mode: Si True, logs detallados
        batch_size: Tamaño del batch (max 100 por API Google)
        stock_dict: Dict con inventario local por tienda {SKU: {"TIENDA-001": qty}}
    
    Returns:
        Cantidad de productos enviados exitosamente
    """
    sent_count = 0
    batch_entries = []
    batch_products = []  # Mantener referencia a productos originales
    batch_id = 1
    
    for idx, product in enumerate(products):
        # Para productos locales, determinar store_code del stock_dict
        store_code = STORE_CODE  # Default
        if channel == 'local' and stock_dict:
            sku = product.get('sku', '')
            if sku in stock_dict:
                # Tomar el primer store_code disponible para este SKU
                available_stores = list(stock_dict[sku].keys())
                if available_stores:
                    store_code = available_stores[0]
        
        entry = wc_product_to_content_api_entry(
            product,
            channel=channel,
            batch_id=batch_id,
            store_code=store_code
        )
        if not entry:
            if stats:
                stats.add_invalid()
            continue
        
        batch_entries.append(entry)
        batch_products.append(product)  # Guardar producto original
        batch_id += 1
        
        # Enviar cuando alcancemos batch_size o sea el último producto
        if len(batch_entries) >= batch_size or idx == len(products) - 1:
            def send_batch():
                try:
                    request = service.products().custombatch(
                        body={'entries': batch_entries}
                    )
                    response = request.execute()
                    
                    if response and 'entries' in response:
                        errors = [e for e in response['entries'] if 'errors' in e]
                        if debug_mode:
                            logger.debug(f"Batch enviado: {len(batch_entries)} productos, {len(errors)} errores")
                        return (True, response, 200 if not errors else 207)
                    return (False, None, 400)
                except Exception as e:
                    logger.error(f"Error enviando batch a Content API: {e}")
                    return (False, None, 500)
            
            success, response, status = retry_with_backoff(
                send_batch,
                max_retries=5,
                debug_mode=debug_mode
            )
            
            if success and response and 'entries' in response:
                for i, entry_resp in enumerate(response['entries']):
                    # Obtener el producto original correspondiente
                    if i < len(batch_products):
                        orig_product = batch_products[i]
                        product_id = orig_product.get('id')
                        sku = orig_product.get('sku', '')
                        
                        # Construir merchant_product_id
                        if channel == 'online':
                            merchant_product_id = f"online:{sku}"
                        else:
                            merchant_product_id = f"local:{STORE_CODE}:{sku}"
                        
                        if 'product' in entry_resp and 'errors' not in entry_resp:
                            sent_count += 1
                            
                            # Actualizar tracking: sync exitoso
                            update_sync_tracking(
                                product_id=product_id,
                                sku=sku,
                                channel=channel,
                                success=True,
                                merchant_product_id=merchant_product_id
                            )
                            
                            if stats:
                                vs = ValidationStatus(
                                    price_valid=True,
                                    images_valid=True,
                                    inventory_valid=True
                                )
                                stats.add_valid(vs)
                                if channel == 'online':
                                    stats.sent_online += 1
                                else:
                                    stats.sent_local += 1
                        elif 'errors' in entry_resp:
                            # Actualizar tracking: error en sync
                            error_messages = [err.get('message', 'Unknown error') for err in entry_resp.get('errors', [])]
                            error_text = '; '.join(error_messages)
                            
                            update_sync_tracking(
                                product_id=product_id,
                                sku=sku,
                                channel=channel,
                                success=False,
                                merchant_product_id=merchant_product_id,
                                error_message=error_text[:500]  # Limitar a 500 chars
                            )
                            
                            if stats:
                                stats.add_invalid()
            
            # Reiniciar batch
            batch_entries = []
            batch_products = []
    
    return sent_count


def delete_products_from_google(service, deleted_products: List[Dict], debug_mode: bool = False) -> int:
    """
    Elimina productos del feed de Google Merchant Center.
    
    Args:
        service: Servicio de Google Content API v2.1
        deleted_products: Lista de dicts con {product_id, sku, channel, merchant_product_id}
        debug_mode: Si True, logs detallados
    
    Returns:
        Cantidad de productos eliminados exitosamente
    """
    if not deleted_products:
        return 0
    
    deleted_count = 0
    
    for product in deleted_products:
        sku = product.get('sku')
        channel = product.get('channel', 'online')
        merchant_product_id = product.get('merchant_product_id')
        product_id = product.get('product_id')
        
        # Si no tenemos merchant_product_id, construirlo
        if not merchant_product_id:
            if channel == 'online':
                merchant_product_id = f"online:{sku}"
            else:
                merchant_product_id = f"local:{STORE_CODE}:{sku}"
        
        def delete_product():
            try:
                service.products().delete(
                    merchantId=MERCHANT_ID,
                    productId=merchant_product_id
                ).execute()
                
                if debug_mode:
                    logger.debug(f"✓ Producto eliminado de Google: {merchant_product_id}")
                
                return (True, None, 200)
            except Exception as e:
                error_msg = str(e)
                # Error 404 significa que el producto ya no existe en Google (OK)
                if '404' in error_msg or 'not found' in error_msg.lower():
                    if debug_mode:
                        logger.debug(f"Producto {merchant_product_id} ya no existe en Google (404)")
                    return (True, None, 404)
                else:
                    logger.error(f"Error eliminando producto {merchant_product_id}: {e}")
                    return (False, None, 500)
        
        success, _, status = retry_with_backoff(
            delete_product,
            max_retries=3,
            debug_mode=debug_mode
        )
        
        if success:
            deleted_count += 1
            # Marcar como deleted en tracking
            mark_product_as_deleted(product_id, sku)
            logger.info(f"✓ Producto {sku} eliminado del feed de Google")
    
    return deleted_count


# ============================================================================
# ARGUMENTOS CLI
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    """
    Analiza los argumentos de línea de comandos.
    
    Returns:
        Objeto Namespace con los argumentos
    """
    parser = argparse.ArgumentParser(
        description='Pipeline: Sincronización WooCommerce → Google Content API v2.1'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Modo depuración con logs detallados'
    )
    parser.add_argument(
        '--batch',
        type=int,
        default=100,
        help='Tamaño del lote para Google API (default: 100)'
    )
    parser.add_argument(
        '--skip-cleanup',
        action='store_true',
        help='Omitir limpieza de productos obsoletos'
    )
    parser.add_argument(
        '--full',
        action='store_true',
        help='Sincronización completa (ignorar last_modified, enviar todos los productos)'
    )
    
    return parser.parse_args()


# ============================================================================
# FUNCIÓN PRINCIPAL
# ============================================================================

def main():
    """Función principal del script."""
    global logger
    
    # Parsear argumentos
    args = parse_arguments()
    
    # Inicializar logger
    logger = setup_logging(debug_mode=args.debug)
    
    logger.info("=" * 80)
    logger.info("INICIO: Pipeline WooCommerce → Google Content API v2.1")
    logger.info(f"Store Code: {STORE_CODE} | Batch Size: {args.batch}")
    logger.info("=" * 80)
    
    start_time = datetime.now()
    
    try:
        # Paso 1: Validar entorno
        if not validate_env():
            logger.error("Validación de entorno falló")
            return 1
        
        # Paso 2: Determinar timestamp para sincronización incremental
        last_sync = None if args.full else get_last_sync_timestamp()
        
        if args.full:
            logger.info("🔄 Modo: SINCRONIZACIÓN COMPLETA (--full)")
        elif last_sync:
            logger.info(f"🔄 Modo: SINCRONIZACIÓN INCREMENTAL (desde {last_sync})")
        else:
            logger.info("🔄 Modo: SINCRONIZACIÓN COMPLETA (primera ejecución)")
        
        # Paso 3: Inicializar servicio Google Content API v2.1
        logger.info("Inicializando Google Content API v2.1...")
        result = init_google_clients()
        
        # retry_with_backoff retorna tupla (success, service, status_code)
        if isinstance(result, tuple) and len(result) >= 3:
            success, service, status_code = result[0], result[1], result[2]
        else:
            # Si no es tupla, intentamos asumirlo como service directo
            success, service = True, result
        
        if not success or not service:
            logger.error("No se pudo inicializar Google Content API")
            return 1
        
        # Paso 4: Obtener productos que necesitan sincronización (usando tracking)
        logger.info("Obteniendo productos que necesitan sincronización...")
        online_products, local_products = get_products_needing_sync(force_full=args.full)
        
        # Obtener stock local para LIA (Local Inventory Ads)
        success, stock_dict, status = fetch_local_stock_from_json()
        
        if not success or not stock_dict:
            logger.error("No se pudo obtener stock local")
            return 1
        
        # Paso 5: Detectar productos eliminados
        if not args.skip_cleanup:
            logger.info("Detectando productos eliminados...")
            deleted_products = get_deleted_products()
            
            if deleted_products:
                logger.warning(f"⚠️  Encontrados {len(deleted_products)} productos eliminados")
                deleted_count = delete_products_from_google(service, deleted_products, debug_mode=args.debug)
                logger.info(f"✓ Eliminados {deleted_count} productos del feed de Google")
        else:
            logger.info("⏭️  Omitiendo detección de productos eliminados (--skip-cleanup)")
        
        # Paso 6: Procesar y enviar
        logger.info("Iniciando procesamiento y envío...")
        stats = PipelineStats()
        
        # Procesar productos online usando Content API v2.1 custombatch
        logger.info(f"Procesando {len(online_products)} productos Online...")
        processor_online = BatchProcessor(batch_size=args.batch)
        
        for product in online_products:
            batch = processor_online.add(product)
            if batch:
                upload_product_batch(
                    service,
                    batch,
                    channel='online',
                    stats=stats,
                    debug_mode=args.debug,
                    batch_size=args.batch,
                    stock_dict=None  # Online no usa stock_dict
                )
        
        final_batch = processor_online.flush()
        if final_batch:
            upload_product_batch(
                service,
                final_batch,
                channel='online',
                stats=stats,
                debug_mode=args.debug,
                batch_size=args.batch,
                stock_dict=None  # Online no usa stock_dict
            )
        
        # Procesar productos locales con inventario
        logger.info(f"Procesando {len(local_products)} productos Locales...")
        # NOTA: Para productos locales, también usamos Content API v2.1
        # Los "locales" son productos que solo vemos en la tienda física (hidden en WooCommerce)
        processor_local = BatchProcessor(batch_size=args.batch)
        
        for product in local_products:
            batch = processor_local.add(product)
            if batch:
                upload_product_batch(
                    service,
                    batch,
                    channel='local',
                    stats=stats,
                    debug_mode=args.debug,
                    batch_size=args.batch,
                    stock_dict=stock_dict  # Local usa stock_dict para LIA
                )
        
        final_batch = processor_local.flush()
        if final_batch:
            upload_product_batch(
                service,
                final_batch,
                channel='local',
                stats=stats,
                debug_mode=args.debug,
                batch_size=args.batch,
                stock_dict=stock_dict  # Local usa stock_dict para LIA
            )
        
        # Paso 7: Guardar timestamp de sincronización exitosa
        current_timestamp = start_time.strftime('%Y-%m-%d %H:%M:%S')
        save_last_sync_timestamp(current_timestamp)
        
        # Paso 8: Resumen final
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        logger.info("=" * 80)
        logger.info("FIN: Pipeline completado exitosamente")
        logger.info(f"Duración: {duration:.2f} segundos")
        logger.info("=" * 80)
        
        # Mostrar estadísticas
        stats.log_report()
        
        return 0
    
    except Exception as e:
        logger.error(f"Error crítico en el pipeline: {e}", exc_info=args.debug)
        return 1


# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================

if __name__ == '__main__':
    sys.exit(main())
