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
DO $mizzle$ DECLARE defs pg_catalog.text[]; refs pg_catalog.text[]; def pg_catalog.text; r record; note pg_catalog.text; BEGIN
IF pg_catalog.current_setting('server_version_num')::int OPERATOR(pg_catalog.>=) 170000 THEN
  EXECUTE 'ALTER TABLE "products" ALTER COLUMN "search" SET EXPRESSION AS (setweight(to_tsvector(''english'', name), ''A'') || setweight(to_tsvector(''english'', coalesce(details, '''')), ''B''))';
ELSE
IF EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND a.attname OPERATOR(pg_catalog.=) 'search' AND a.attacl IS NOT NULL)
OR EXISTS (SELECT 1 FROM pg_catalog.pg_depend d JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) d.refobjid AND a.attnum OPERATOR(pg_catalog.=) d.refobjsubid WHERE d.refclassid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass AND d.refobjid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND a.attname OPERATOR(pg_catalog.=) 'search' AND d.deptype OPERATOR(pg_catalog.=) ANY ('{a,n}'::pg_catalog."char"[]) AND d.classid OPERATOR(pg_catalog.<>) 'pg_catalog.pg_attrdef'::pg_catalog.regclass AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class s WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass AND s.oid OPERATOR(pg_catalog.=) d.objid AND s.relkind OPERATOR(pg_catalog.=) 'S') AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint n WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_constraint'::pg_catalog.regclass AND n.oid OPERATOR(pg_catalog.=) d.objid AND n.contype OPERATOR(pg_catalog.=) 'n') AND d.classid OPERATOR(pg_catalog.<>) 'pg_catalog.pg_constraint'::pg_catalog.regclass AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class i WHERE d.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass AND i.oid OPERATOR(pg_catalog.=) d.objid AND i.relkind OPERATOR(pg_catalog.=) ANY ('{i,I}'::pg_catalog."char"[]))) THEN
  RAISE EXCEPTION 'column % is dropped and added again, which would drop the grants, constraints, views or triggers on it that mizzle does not manage; this needs a custom migration', 'products.search';
END IF;
  SELECT pg_catalog.array_agg(d) INTO defs FROM (
    SELECT pg_catalog.format('ALTER TABLE %s ADD CONSTRAINT %I %s', '"products"', conname, pg_catalog.pg_get_constraintdef(c.oid)) AS d
    FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.conrelid
    WHERE c.conrelid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND a.attname OPERATOR(pg_catalog.=) 'search' AND a.attnum OPERATOR(pg_catalog.=) ANY(c.conkey)
    UNION ALL
    SELECT DISTINCT pg_catalog.pg_get_indexdef(i.indexrelid) FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_depend dep ON dep.classid OPERATOR(pg_catalog.=) 'pg_catalog.pg_class'::pg_catalog.regclass AND dep.objid OPERATOR(pg_catalog.=) i.indexrelid AND dep.refobjid OPERATOR(pg_catalog.=) i.indrelid
    JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) i.indrelid AND a.attnum OPERATOR(pg_catalog.=) dep.refobjsubid
    WHERE i.indrelid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND a.attname OPERATOR(pg_catalog.=) 'search'
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conindid OPERATOR(pg_catalog.=) i.indexrelid)
  ) s;
  FOR r IN SELECT c.conrelid::pg_catalog.regclass AS t, c.conname, pg_catalog.pg_get_constraintdef(c.oid) AS d
    FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_attribute a ON a.attrelid OPERATOR(pg_catalog.=) c.confrelid
    WHERE c.contype OPERATOR(pg_catalog.=) 'f' AND c.confrelid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND a.attname OPERATOR(pg_catalog.=) 'search'
      AND a.attnum OPERATOR(pg_catalog.=) ANY(c.confkey)
      AND NOT (c.conrelid OPERATOR(pg_catalog.=) c.confrelid AND a.attnum OPERATOR(pg_catalog.=) ANY(c.conkey)) LOOP
    refs := pg_catalog.array_append(refs, pg_catalog.format('ALTER TABLE %s ADD CONSTRAINT %I %s', r.t, r.conname, r.d));
    EXECUTE pg_catalog.format('ALTER TABLE %s DROP CONSTRAINT %I', r.t, r.conname);
  END LOOP;
  note := pg_catalog.col_description('"products"'::pg_catalog.regclass, (SELECT attnum FROM pg_catalog.pg_attribute WHERE attrelid OPERATOR(pg_catalog.=) '"products"'::pg_catalog.regclass AND attname OPERATOR(pg_catalog.=) 'search'));
  EXECUTE 'ALTER TABLE "products" DROP COLUMN "search"';
  EXECUTE 'ALTER TABLE "products" ADD COLUMN "search" tsvector GENERATED ALWAYS AS (setweight(to_tsvector(''english'', name), ''A'') || setweight(to_tsvector(''english'', coalesce(details, '''')), ''B'')) STORED';
  IF note IS NOT NULL THEN EXECUTE pg_catalog.format('COMMENT ON COLUMN %s.%I IS %L', '"products"', 'search', note); END IF;
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
