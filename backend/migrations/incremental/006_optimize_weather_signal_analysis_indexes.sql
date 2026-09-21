-- Indexes for bounded Sweep accuracy analysis and local resolution lookup.

ALTER TABLE weather_orderbook_signals
  ADD KEY idx_signal_type_occurred (signal_type, occurred_at DESC, id DESC),
  ADD KEY idx_local_resolution_lookup (
    signal_type, outcome, event_slug, occurred_at DESC, id DESC
  );
