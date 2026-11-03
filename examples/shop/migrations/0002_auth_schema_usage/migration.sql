-- Custom migration. Separate statements with: -- mizzle:breakpoint

-- Policies call auth.uid() as the signed-in role, which needs to see the schema.
GRANT USAGE ON SCHEMA auth TO shop_authenticated;
-- mizzle:breakpoint
-- grant() covers tables and views; inserts through RLS also need the serial's sequence.
GRANT USAGE ON SEQUENCE reviews_id_seq TO shop_authenticated;
