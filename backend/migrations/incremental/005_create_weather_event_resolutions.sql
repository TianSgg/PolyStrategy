-- Cache the final outcome of each Polymarket weather event.
--
-- A resolved result is immutable and can be reused by analysis jobs. Non-final
-- lookup results use next_check_at so callers can retry them later.

CREATE TABLE weather_event_resolutions (
  event_slug VARCHAR(255) NOT NULL COMMENT 'Polymarket event slug',

  status ENUM('resolved', 'open', 'not_found', 'error') NOT NULL
    COMMENT 'Latest resolution lookup state',
  winning_temperature_label VARCHAR(100) NULL
    COMMENT 'Winning weather outcome label when status is resolved',

  source ENUM('local_signal', 'gamma') NULL
    COMMENT 'System observation or Polymarket Gamma API',
  source_signal_id VARCHAR(512) NULL
    COMMENT 'weather_orderbook_signals.signal_id for local_signal',
  gamma_event_id VARCHAR(64) NULL
    COMMENT 'Gamma event ID when available',
  resolved_at DATETIME(3) NULL
    COMMENT 'Time this system first confirmed the final resolution in UTC',

  checked_at DATETIME(3) NOT NULL
    COMMENT 'Time of the latest lookup or local observation in UTC',
  next_check_at DATETIME(3) NULL
    COMMENT 'Earliest retry time for non-final states in UTC',
  raw_payload JSON NULL
    COMMENT 'Source payload retained for audit and parser changes',
  last_error VARCHAR(512) NULL
    COMMENT 'Most recent lookup error for status error',

  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  PRIMARY KEY (event_slug),
  KEY idx_resolution_retry (status, next_check_at),
  KEY idx_resolution_checked (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Polymarket weather event resolution cache';
