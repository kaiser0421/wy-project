-- Test-only clock override. Applied to a cloned test database by the `test_clock` fixture; it is
-- never part of the production migrations or image. Lets tests move the database clock (used by
-- every deadline decision via app_now()) without sleeping.
CREATE TABLE test_clock (
  id           int PRIMARY KEY CHECK (id = 1),
  now_override timestamptz
);
INSERT INTO test_clock (id, now_override) VALUES (1, NULL);

CREATE OR REPLACE FUNCTION app_now() RETURNS timestamptz
  LANGUAGE sql VOLATILE AS $$
    SELECT coalesce((SELECT now_override FROM test_clock WHERE id = 1), clock_timestamp())
  $$;
