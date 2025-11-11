#!/usr/bin/env python3
"""
Script de Inicialización de Base de Datos (v1.0.0)
===================================================

Ejecuta las migraciones necesarias y verifica el estado de la base de datos.

Uso:
    python init_database.py [--check-only]

Opciones:
    --check-only    Solo verifica el estado sin hacer cambios
"""

import os
import sys
import mysql.connector
from dotenv import load_dotenv
from pathlib import Path

# Cargar variables de entorno
load_dotenv()

# Configuración de BD desde .env
DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
DB_NAME = os.getenv('DB_NAME')

def print_header(text):
    """Imprime un encabezado formateado."""
    print(f"\n{'=' * 80}")
    print(f"  {text}")
    print('=' * 80)

def print_success(text):
    """Imprime un mensaje de éxito."""
    print(f"✓ {text}")

def print_error(text):
    """Imprime un mensaje de error."""
    print(f"✗ {text}")

def print_info(text):
    """Imprime un mensaje informativo."""
    print(f"  {text}")

def check_connection():
    """Verifica la conexión a la base de datos."""
    print_header("Verificando Conexión a Base de Datos")
    
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        print_error("Faltan variables de entorno:")
        if not DB_HOST: print_info("DB_HOST no definido")
        if not DB_USER: print_info("DB_USER no definido")
        if not DB_PASSWORD: print_info("DB_PASSWORD no definido")
        if not DB_NAME: print_info("DB_NAME no definido")
        return None
    
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        print_success(f"Conectado a {DB_NAME}@{DB_HOST}")
        return conn
    except mysql.connector.Error as e:
        print_error(f"Error de conexión: {e}")
        return None

def check_table_exists(cursor, table_name):
    """Verifica si una tabla existe."""
    cursor.execute(f"""
        SELECT COUNT(*) 
        FROM information_schema.TABLES 
        WHERE TABLE_SCHEMA = DATABASE() 
        AND TABLE_NAME = '{table_name}'
    """)
    return cursor.fetchone()[0] > 0

def get_table_info(cursor, table_name):
    """Obtiene información detallada de una tabla."""
    cursor.execute(f"""
        SELECT 
            TABLE_NAME,
            ENGINE,
            TABLE_COLLATION,
            CREATE_TIME,
            UPDATE_TIME,
            TABLE_ROWS,
            DATA_LENGTH,
            INDEX_LENGTH
        FROM information_schema.TABLES 
        WHERE TABLE_SCHEMA = DATABASE() 
        AND TABLE_NAME = '{table_name}'
    """)
    return cursor.fetchone()

def get_table_indexes(cursor, table_name):
    """Obtiene lista de índices de una tabla."""
    cursor.execute(f"SHOW INDEX FROM {table_name}")
    indexes = {}
    for row in cursor.fetchall():
        index_name = row[2]
        if index_name not in indexes:
            indexes[index_name] = []
        indexes[index_name].append(row[4])  # Column name
    return indexes

def check_database_status(conn):
    """Verifica el estado actual de la base de datos."""
    print_header("Estado Actual de la Base de Datos")
    
    cursor = conn.cursor()
    
    # Verificar wp_product_sync_tracking
    table_name = 'wp_product_sync_tracking'
    if check_table_exists(cursor, table_name):
        print_success(f"Tabla '{table_name}' existe")
        
        info = get_table_info(cursor, table_name)
        if info:
            print_info(f"  Engine: {info[1]}")
            print_info(f"  Collation: {info[2]}")
            print_info(f"  Creada: {info[3]}")
            print_info(f"  Filas: {info[5]}")
            print_info(f"  Tamaño datos: {info[6]:,} bytes")
            print_info(f"  Tamaño índices: {info[7]:,} bytes")
        
        # Verificar índices
        indexes = get_table_indexes(cursor, table_name)
        print_info(f"  Índices: {len(indexes)}")
        for idx_name, columns in indexes.items():
            cols_str = ', '.join(columns)
            print_info(f"    - {idx_name}: ({cols_str})")
        
        # Estadísticas de sync
        cursor.execute(f"""
            SELECT 
                sync_status,
                channel,
                COUNT(*) as count
            FROM {table_name}
            GROUP BY sync_status, channel
        """)
        
        stats = cursor.fetchall()
        if stats:
            print_info("  Estadísticas de sincronización:")
            for stat in stats:
                print_info(f"    - {stat[0]} ({stat[1]}): {stat[2]} productos")
        else:
            print_info("  Sin registros de sincronización aún")
    else:
        print_error(f"Tabla '{table_name}' NO existe")
        return False
    
    cursor.close()
    return True

def run_initialization(conn):
    """Ejecuta el script de inicialización SQL."""
    print_header("Ejecutando Script de Inicialización")
    
    # Buscar el archivo SQL
    sql_file = Path(__file__).parent.parent / 'db' / 'init_database.sql'
    
    if not sql_file.exists():
        print_error(f"Archivo SQL no encontrado: {sql_file}")
        return False
    
    print_info(f"Leyendo: {sql_file}")
    
    try:
        with open(sql_file, 'r', encoding='utf-8') as f:
            sql_content = f.read()
        
        # Separar statements (simple split por ;)
        statements = [s.strip() for s in sql_content.split(';') if s.strip() and not s.strip().startswith('/*')]
        
        cursor = conn.cursor()
        executed = 0
        
        for statement in statements:
            # Ignorar comentarios
            if statement.startswith('--'):
                continue
            
            try:
                cursor.execute(statement)
                executed += 1
            except mysql.connector.Error as e:
                # Ignorar errores de "ya existe"
                if 'already exists' not in str(e).lower():
                    print_error(f"Error ejecutando statement: {e}")
                    print_info(f"Statement: {statement[:100]}...")
        
        conn.commit()
        print_success(f"Ejecutados {executed} statements SQL")
        cursor.close()
        return True
        
    except Exception as e:
        print_error(f"Error leyendo archivo SQL: {e}")
        return False

def verify_required_tables(conn):
    """Verifica que todas las tablas requeridas existan."""
    print_header("Verificando Tablas Requeridas")
    
    required_tables = {
        'wp_posts': 'Tabla principal de WordPress (posts/productos)',
        'wp_postmeta': 'Metadatos de productos (precio, SKU, stock)',
        'wp_product_sync_tracking': 'Tracking de sincronización con Google'
    }
    
    cursor = conn.cursor()
    all_exist = True
    
    for table_name, description in required_tables.items():
        if check_table_exists(cursor, table_name):
            print_success(f"{table_name}: {description}")
        else:
            print_error(f"{table_name}: {description} - NO EXISTE")
            all_exist = False
    
    cursor.close()
    return all_exist

def main():
    """Función principal."""
    print_header("Inicialización de Base de Datos - Pipeline Google Merchant")
    
    check_only = '--check-only' in sys.argv
    
    # Verificar conexión
    conn = check_connection()
    if not conn:
        return 1
    
    # Verificar estado actual
    status_ok = check_database_status(conn)
    
    if check_only:
        print_header("Modo Solo Verificación - No se realizaron cambios")
        conn.close()
        return 0 if status_ok else 1
    
    # Si la tabla no existe, ejecutar inicialización
    if not status_ok:
        if run_initialization(conn):
            print_success("Inicialización completada")
            # Verificar de nuevo
            check_database_status(conn)
        else:
            print_error("Inicialización falló")
            conn.close()
            return 1
    else:
        print_info("Base de datos ya está inicializada")
    
    # Verificación final
    if verify_required_tables(conn):
        print_header("✓ Todas las tablas requeridas están presentes")
        print_success("Base de datos lista para usar")
    else:
        print_header("✗ Faltan tablas requeridas")
        conn.close()
        return 1
    
    conn.close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
