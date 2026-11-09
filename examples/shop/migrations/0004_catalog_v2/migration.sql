-- rename column products.description to details
-- add column users.phone
-- change type of stores.slug from character varying(40) to character varying(64)
-- change generated expression on products.search
-- create view top_products
-- enable row level security on orders
-- create policy orders_owner on orders
ALTER TABLE "products" RENAME COLUMN "description" TO "details";
-- mizzle:breakpoint
ALTER TABLE "users" ADD COLUMN "phone" text;
-- mizzle:breakpoint
ALTER TABLE "stores" ALTER COLUMN "slug" SET DATA TYPE character varying(64) USING "slug"::character varying(64);
-- mizzle:breakpoint
DO $mizzle$ DECLARE defs text[]; refs text[]; def text; r record; BEGIN
IF current_setting('server_version_num')::int >= 170000 THEN
  EXECUTE 'ALTER TABLE "products" ALTER COLUMN "search" SET EXPRESSION AS (setweight(to_tsvector(''english'', name), ''A'') || setweight(to_tsvector(''english'', coalesce(details, '''')), ''B''))';
ELSE
IF EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = '"products"'::regclass AND a.attname = 'search' AND a.attacl IS NOT NULL)
OR EXISTS (SELECT 1 FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = '"products"'::regclass AND a.attname = 'search' AND d.deptype IN ('a', 'n') AND d.classid <> 'pg_attrdef'::regclass AND NOT EXISTS (SELECT 1 FROM pg_class s WHERE d.classid = 'pg_class'::regclass AND s.oid = d.objid AND s.relkind = 'S') AND NOT EXISTS (SELECT 1 FROM pg_constraint n WHERE d.classid = 'pg_constraint'::regclass AND n.oid = d.objid AND n.contype = 'n') AND d.classid <> 'pg_constraint'::regclass AND NOT EXISTS (SELECT 1 FROM pg_class i WHERE d.classid = 'pg_class'::regclass AND i.oid = d.objid AND i.relkind IN ('i', 'I'))) THEN
  RAISE EXCEPTION 'column % is dropped and added again, which would drop the grants, constraints or triggers on it that mizzle does not manage; this needs a custom migration', 'products.search';
END IF;
  SELECT array_agg(d) INTO defs FROM (
    SELECT format('ALTER TABLE %s ADD CONSTRAINT %I %s', '"products"', conname, pg_get_constraintdef(c.oid)) AS d
    FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid
    WHERE c.conrelid = '"products"'::regclass AND a.attname = 'search' AND a.attnum = ANY(c.conkey)
    UNION ALL
    SELECT DISTINCT pg_get_indexdef(i.indexrelid) FROM pg_index i
    JOIN pg_depend dep ON dep.classid = 'pg_class'::regclass AND dep.objid = i.indexrelid AND dep.refobjid = i.indrelid
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = dep.refobjsubid
    WHERE i.indrelid = '"products"'::regclass AND a.attname = 'search'
      AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
  ) s;
  FOR r IN SELECT c.conrelid::regclass AS t, c.conname, pg_get_constraintdef(c.oid) AS d
    FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.confrelid
    WHERE c.contype = 'f' AND c.confrelid = '"products"'::regclass AND a.attname = 'search'
      AND a.attnum = ANY(c.confkey)
      AND NOT (c.conrelid = c.confrelid AND a.attnum = ANY(c.conkey)) LOOP
    refs := refs || format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.t, r.conname, r.d);
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.t, r.conname);
  END LOOP;
  EXECUTE 'ALTER TABLE "products" DROP COLUMN "search"';
  EXECUTE 'ALTER TABLE "products" ADD COLUMN "search" tsvector GENERATED ALWAYS AS (setweight(to_tsvector(''english'', name), ''A'') || setweight(to_tsvector(''english'', coalesce(details, '''')), ''B'')) STORED';
  FOREACH def IN ARRAY coalesce(defs, '{}') LOOP EXECUTE def; END LOOP;
  FOREACH def IN ARRAY coalesce(refs, '{}') LOOP EXECUTE def; END LOOP;
END IF; END $mizzle$;
-- mizzle:breakpoint
CREATE VIEW "top_products" AS
SELECT "order_items"."product_id" AS "product_id", sum("order_items"."quantity") AS "sold" FROM "order_items" GROUP BY "order_items"."product_id";
-- mizzle:breakpoint
ALTER TABLE "orders" ENABLE ROW LEVEL SECURITY, NO FORCE ROW LEVEL SECURITY;
-- mizzle:breakpoint
CREATE POLICY "orders_owner" ON "orders" AS PERMISSIVE FOR SELECT TO "shop_authenticated" USING ("user_id" = (select auth.uid()));
