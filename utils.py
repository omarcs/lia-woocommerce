#!/usr/bin/env python3
# ============================================================================
# Módulo: utils.py (v1.0.0)
# Propósito: Utilidades Compartidas para Pipeline ETL
# ============================================================================

import time
import logging
import threading
from typing import Callable, Any, Optional, List, Dict
from dataclasses import dataclass, field
import mysql.connector
from mysql.connector import Error


# ============================================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================================

def setup_logging(debug_mode: bool = False, log_level: str = None) -> logging.Logger:
    """
    Configura el logger con o sin modo depuración.
    
    Args:
        debug_mode: Si True, nivel DEBUG; si False, nivel INFO
        log_level: Nivel personalizado (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    
    Returns:
        logging.Logger: Logger configurado
    """
    level = log_level if log_level else ('DEBUG' if debug_mode else 'INFO')
    
    logger = logging.getLogger(__name__)
    logger.setLevel(getattr(logging, level))
    
    # Handler de consola
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s - %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    
    if not logger.handlers:
        logger.addHandler(handler)
    
    return logger


# ============================================================================
# REINTENTOS CON BACKOFF EXPONENCIAL
# ============================================================================

def retry_with_backoff(
    func: Callable,
    max_retries: int = 7,
    base_wait: int = 2,
    rate_limit_wait: int = 60,
    debug_mode: bool = False
) -> Optional[Any]:
    """
    Ejecuta una función con reintentos y backoff exponencial.
    
    Maneja diferenciadamente:
    - Errores 401: Señal de que el token ha expirado
    - Errores 429: Rate limit (espera fija de 60 segundos)
    - Otros errores: Backoff exponencial (2^attempt)
    
    Args:
        func: Función callable que retorna (success: bool, result: Any, error_code: int)
        max_retries: Número máximo de intentos (default: 7)
        base_wait: Base para backoff exponencial (default: 2)
        rate_limit_wait: Espera para error 429 (default: 60)
        debug_mode: Si True, imprime logs detallados
    
    Returns:
        Resultado de la función si éxito, None si fallan todos los intentos
    
    Ejemplo:
        def my_api_call():
            try:
                response = requests.get('...')
                return (response.ok, response.json(), response.status_code)
            except Exception as e:
                return (False, None, 0)
        
        result = retry_with_backoff(my_api_call, max_retries=7, debug_mode=True)
    """
    logger = logging.getLogger(__name__)
    
    for attempt in range(max_retries):
        try:
            success, result, status_code = func()
            
            if success:
                if debug_mode:
                    logger.debug(f"✓ Intento {attempt + 1}: Éxito")
                return (success, result, status_code)
            
            # Si no fue exitoso, decidir si reintentar
            if status_code in [500, 502, 503, 504]:
                logger.warning(f"Error del servidor ({status_code}). Intento {attempt + 1} de {max_retries}")
                wait_time = base_wait ** attempt
            elif status_code == 429:
                logger.warning(f"Rate limit excedido (429). Intento {attempt + 1} de {max_retries}")
                wait_time = rate_limit_wait
            elif status_code == 401:
                logger.warning(f"Token inválido o expirado (401). Intento {attempt + 1} de {max_retries}")
                wait_time = base_wait ** attempt
            elif status_code in [404, 403]:
                logger.error(f"Error del cliente ({status_code}). Omitiendo sin reintentar.")
                return None
            else:
                logger.warning(f"Error HTTP {status_code}. Intento {attempt + 1} de {max_retries}")
                wait_time = base_wait ** attempt
            
            if attempt < max_retries - 1:
                if debug_mode:
                    logger.debug(f"Reintentando en {wait_time} segundos...")
                time.sleep(wait_time)
            else:
                logger.error(f"Máximo de reintentos ({max_retries}) alcanzado. Omitiendo.")
                return None
        
        except Exception as e:
            logger.error(f"Excepción en intento {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                wait_time = base_wait ** attempt
                if debug_mode:
                    logger.debug(f"Reintentando en {wait_time} segundos...")
                time.sleep(wait_time)
            else:
                logger.error(f"Máximo de reintentos ({max_retries}) alcanzado. Omitiendo.")
                return None
    
    return None


# ============================================================================
# VALIDACIÓN MULTI-ETAPA
# ============================================================================

@dataclass
class ValidationStatus:
    """
    Estado de validación de un producto con múltiples criterios.
    
    Atributos:
        price_valid: Si el precio es válido (> 0)
        images_valid: Si hay imágenes disponibles
        inventory_valid: Si el inventario está disponible
        title_valid: Si el título no está vacío
        description_valid: Si la descripción no está vacía
    """
    price_valid: bool = False
    images_valid: bool = False
    inventory_valid: bool = False
    title_valid: bool = False
    description_valid: bool = False
    custom_validations: Dict[str, bool] = field(default_factory=dict)
    
    def is_valid(self, required_fields: Optional[List[str]] = None) -> bool:
        """
        Verifica si el producto cumple con todas las validaciones requeridas.
        
        Args:
            required_fields: Lista de campos requeridos. Si None, solo verifica los básicos.
        
        Returns:
            True si pasa todas las validaciones, False en caso contrario
        """
        if required_fields is None:
            # Validación por defecto: precio, imágenes, inventario
            required_fields = ['price_valid', 'images_valid', 'inventory_valid']
        
        for field in required_fields:
            if field in ['price_valid', 'images_valid', 'inventory_valid', 'title_valid', 'description_valid']:
                if not getattr(self, field, False):
                    return False
            elif field in self.custom_validations:
                if not self.custom_validations[field]:
                    return False
        
        return True
    
    def summary(self) -> str:
        """Retorna un resumen en string de las validaciones."""
        parts = []
        if self.price_valid:
            parts.append("precio_ok")
        if self.images_valid:
            parts.append("imágenes_ok")
        if self.inventory_valid:
            parts.append("inventario_ok")
        if self.title_valid:
            parts.append("título_ok")
        if self.description_valid:
            parts.append("descripción_ok")
        
        for key, value in self.custom_validations.items():
            if value:
                parts.append(f"{key}_ok")
        
        return f"[{', '.join(parts)}]" if parts else "[SIN VALIDACIONES]"


# ============================================================================
# BATCH PROCESSING
# ============================================================================

class BatchProcessor:
    """
    Procesa datos en lotes de tamaño configurable.
    
    Ejemplo:
        processor = BatchProcessor(batch_size=500)
        for item in large_dataset:
            batch = processor.add(item)
            if batch:
                send_to_api(batch)
        
        # Enviar lote final
        final_batch = processor.flush()
        if final_batch:
            send_to_api(final_batch)
    """
    
    def __init__(self, batch_size: int = 500):
        """
        Inicializa el procesador de lotes.
        
        Args:
            batch_size: Tamaño máximo del lote (default: 500)
        """
        self.batch_size = batch_size
        self.batch: List[Any] = []
        self.total_processed = 0
        self.logger = logging.getLogger(__name__)
    
    def add(self, item: Any) -> Optional[List[Any]]:
        """
        Agrega un item al lote. Retorna el lote si se alcanzó el tamaño máximo.
        
        Args:
            item: Item a agregar al lote
        
        Returns:
            Lista de items si se alcanzó batch_size, None en caso contrario
        """
        self.batch.append(item)
        
        if len(self.batch) >= self.batch_size:
            return self.flush()
        
        return None
    
    def flush(self) -> Optional[List[Any]]:
        """
        Retorna el lote actual (incluso si no está lleno) y reinicia.
        
        Returns:
            Lista de items o None si el lote está vacío
        """
        if not self.batch:
            return None
        
        result = self.batch.copy()
        self.total_processed += len(self.batch)
        self.batch.clear()
        
        self.logger.debug(f"Lote procesado: {len(result)} items. Total: {self.total_processed}")
        return result
    
    def size(self) -> int:
        """Retorna el tamaño actual del lote."""
        return len(self.batch)
    
    def clear(self):
        """Limpia el lote sin procesarlo."""
        self.batch.clear()


# ============================================================================
# THREADING & SINCRONIZACIÓN
# ============================================================================

class ThreadSafeQueue:
    """
    Cola thread-safe con sincronización usando threading.Condition.
    
    Modelada después de process_and_send_product_batches del script validado.
    
    Ejemplo:
        queue = ThreadSafeQueue()
        
        def producer():
            for item in items:
                queue.put(item)
        
        def consumer():
            while True:
                item = queue.get(timeout=5)
                if item is None:
                    break
                process(item)
        
        threading.Thread(target=producer).start()
        threading.Thread(target=consumer).start()
    """
    
    def __init__(self):
        """Inicializa la cola thread-safe."""
        self.lock = threading.Lock()
        self.condition = threading.Condition(lock=self.lock)
        self.queue: Optional[Any] = None
        self.finished = threading.Event()
        self.logger = logging.getLogger(__name__)
    
    def put(self, item: Any, timeout: int = 30):
        """
        Agrega un item a la cola. Espera si hay un item pendiente.
        
        Args:
            item: Item a agregar
            timeout: Tiempo máximo de espera (segundos)
        
        Returns:
            True si éxito, False si timeout
        """
        with self.condition:
            # Esperar a que el item anterior sea procesado
            start_time = time.time()
            while self.queue is not None:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self.logger.warning(f"Timeout esperando a que se procese item anterior ({timeout}s)")
                    return False
                remaining = timeout - elapsed
                self.condition.wait(timeout=remaining)
            
            self.queue = item
            self.condition.notify()
            self.logger.debug(f"Item agregado a cola")
            return True
    
    def get(self, timeout: int = 30) -> Optional[Any]:
        """
        Obtiene un item de la cola. Espera si no hay items.
        
        Args:
            timeout: Tiempo máximo de espera (segundos)
        
        Returns:
            Item o None si timeout o finished
        """
        with self.condition:
            start_time = time.time()
            while self.queue is None:
                if self.finished.is_set():
                    self.logger.debug("Finished flag establecido, terminando consumer")
                    return None
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self.logger.debug(f"Timeout esperando item ({timeout}s)")
                    return None
                remaining = timeout - elapsed
                self.condition.wait(timeout=remaining)
            
            item = self.queue
            self.queue = None
            self.condition.notify()
            self.logger.debug(f"Item obtenido de cola")
            return item
    
    def mark_finished(self):
        """Marca la cola como finalizada (no hay más items)."""
        with self.condition:
            self.finished.set()
            self.condition.notify_all()
            self.logger.debug("Cola marcada como finalizada")


# ============================================================================
# CONEXIÓN A MYSQL
# ============================================================================

def get_mysql_connection(
    host: str = 'localhost',
    user: str = None,
    password: str = None,
    database: str = None,
    max_retries: int = 5,
    debug_mode: bool = False
) -> Optional[mysql.connector.MySQLConnection]:
    """
    Obtiene conexión a MySQL con reintentos.
    
    Args:
        host: Host de MySQL
        user: Usuario de MySQL
        password: Contraseña de MySQL
        database: Base de datos
        max_retries: Número máximo de intentos
        debug_mode: Si True, imprime logs detallados
    
    Returns:
        Conexión MySQL o None si falla
    """
    logger = logging.getLogger(__name__)
    
    def connect():
        try:
            connection = mysql.connector.connect(
                host=host,
                user=user,
                password=password,
                database=database,
                charset='utf8mb4',
                collation='utf8mb4_unicode_ci',
                autocommit=False
            )
            if connection.is_connected():
                db_info = connection.get_server_info()
                logger.info(f"✓ Conectado a MySQL Server versión {db_info}")
                return (True, connection, 200)
            return (False, None, 0)
        except Error as e:
            logger.error(f"Error de conexión a MySQL: {e}")
            return (False, None, 500)
    
    return retry_with_backoff(connect, max_retries=max_retries, debug_mode=debug_mode)


# ============================================================================
# ESTADÍSTICAS DE PIPELINE
# ============================================================================

@dataclass
class PipelineStats:
    """
    Estadísticas del pipeline ETL.
    """
    total_processed: int = 0
    total_valid: int = 0
    total_invalid: int = 0
    
    # Por validación
    with_valid_prices: int = 0
    with_valid_images: int = 0
    with_valid_inventory: int = 0
    
    # Por canal (para Google)
    sent_online: int = 0
    sent_local: int = 0
    
    # Errores
    total_errors: int = 0
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))
    
    def add_valid(self, validation_status: ValidationStatus):
        """Registra un producto válido."""
        self.total_valid += 1
        self.total_processed += 1
        
        if validation_status.price_valid:
            self.with_valid_prices += 1
        if validation_status.images_valid:
            self.with_valid_images += 1
        if validation_status.inventory_valid:
            self.with_valid_inventory += 1
    
    def add_invalid(self):
        """Registra un producto inválido."""
        self.total_invalid += 1
        self.total_processed += 1
    
    def add_error(self):
        """Registra un error."""
        self.total_errors += 1
    
    def report(self) -> str:
        """Retorna un reporte de estadísticas."""
        lines = [
            "",
            "=" * 60,
            "ESTADÍSTICAS DEL PIPELINE".center(60),
            "=" * 60,
            f"  Total procesados:        {self.total_processed:>6}",
            f"  Válidos:                 {self.total_valid:>6}",
            f"  Inválidos:               {self.total_invalid:>6}",
            f"  Errores:                 {self.total_errors:>6}",
            "-" * 60,
            f"  Con precios válidos:     {self.with_valid_prices:>6}",
            f"  Con imágenes válidas:    {self.with_valid_images:>6}",
            f"  Con inventario:          {self.with_valid_inventory:>6}",
            "-" * 60,
            f"  Enviados (Online):       {self.sent_online:>6}",
            f"  Enviados (Local):        {self.sent_local:>6}",
            "=" * 60,
            ""
        ]
        return "\n".join(lines)
    
    def log_report(self):
        """Registra el reporte en los logs."""
        self.logger.info(self.report())


# ============================================================================
# PUNTO DE ENTRADA (PRUEBA)
# ============================================================================

if __name__ == '__main__':
    # Pruebas básicas
    logger = setup_logging(debug_mode=True)
    
    # Test ValidationStatus
    vs = ValidationStatus(
        price_valid=True,
        images_valid=True,
        inventory_valid=True
    )
    print(f"Validación: {vs.summary()}")
    print(f"¿Es válido? {vs.is_valid()}")
    
    # Test BatchProcessor
    processor = BatchProcessor(batch_size=3)
    for i in range(10):
        batch = processor.add(f"item_{i}")
        if batch:
            print(f"Lote completado: {batch}")
    final = processor.flush()
    if final:
        print(f"Lote final: {final}")
    
    # Test PipelineStats
    stats = PipelineStats()
    stats.add_valid(vs)
    stats.add_valid(vs)
    stats.add_invalid()
    stats.log_report()
