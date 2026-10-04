-- The key/value settings table was never read or written; configuration lives
-- in config/del.toml. Drop it so the schema matches what the code uses.
DROP TABLE IF EXISTS settings;
