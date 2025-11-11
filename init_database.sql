-- Minimal tracking table used by the pipeline
CREATE TABLE IF NOT EXISTS wp_product_sync_tracking (
  id BIGINT(20) UNSIGNED NOT NULL AUTO_INCREMENT,
  product_id BIGINT(20) UNSIGNED NOT NULL,
  sku VARCHAR(255) NOT NULL,
  channel VARCHAR(20) NOT NULL DEFAULT 'online',
  last_sent_at DATETIME DEFAULT NULL,
  last_modified_at DATETIME DEFAULT NULL,
  sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
  merchant_product_id VARCHAR(255) DEFAULT NULL,
  error_count INT(11) NOT NULL DEFAULT 0,
  last_error TEXT DEFAULT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY idx_product_sku (product_id, sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
