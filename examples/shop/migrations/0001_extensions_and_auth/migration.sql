-- Custom migration: what the schema needs and mizzle does not manage (extensions,
-- functions). Runs before the tables: an index uses pg_trgm, policies call auth.uid().
-- Separate statements with: -- mizzle:breakpoint

CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- mizzle:breakpoint
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- mizzle:breakpoint
CREATE SCHEMA IF NOT EXISTS auth;
-- mizzle:breakpoint
-- Supabase's auth.uid(): the "sub" claim of the request's JWT (set per transaction).
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
SELECT nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
$$;
