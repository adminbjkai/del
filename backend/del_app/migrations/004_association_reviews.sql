-- Keep operator review flags separate from correlation-derived safety flags.
ALTER TABLE associations ADD COLUMN user_excluded INTEGER NOT NULL DEFAULT 0;
ALTER TABLE associations ADD COLUMN user_shared INTEGER NOT NULL DEFAULT 0;
-- Existing exclusions cannot be distinguished from operator exclusions. Keep
-- them conservatively rather than silently making a resource removable.
UPDATE associations SET user_excluded = 1 WHERE excluded = 1;
