-- Custom migration: triggers, their functions and grants (mizzle does not manage them).
-- Separate statements with: -- mizzle:breakpoint

-- Every change to orders and stores lands in the audit log.
CREATE OR REPLACE FUNCTION shop_audit.log_change() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  INSERT INTO shop_audit.audit_log (table_name, action, row_id, data)
  VALUES (
    TG_TABLE_NAME,
    lower(TG_OP)::shop_audit.audit_action,
    CASE WHEN TG_OP = 'DELETE' THEN OLD.id::text ELSE NEW.id::text END,
    CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END
  );
  RETURN NULL;
END
$$;
-- mizzle:breakpoint
CREATE TRIGGER orders_audit AFTER INSERT OR UPDATE OR DELETE ON orders
FOR EACH ROW EXECUTE FUNCTION shop_audit.log_change();
-- mizzle:breakpoint
CREATE TRIGGER stores_audit AFTER INSERT OR DELETE ON stores
FOR EACH ROW EXECUTE FUNCTION shop_audit.log_change();
-- mizzle:breakpoint
-- A price change notifies listeners and the store owner.
CREATE OR REPLACE FUNCTION price_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('shop_price', json_build_object(
    'product_id', NEW.id, 'old', OLD.price, 'new', NEW.price)::text);
  INSERT INTO notifications (user_id, kind, payload)
  SELECT s.owner_id, 'price_changed',
         jsonb_build_object('product_id', NEW.id, 'old', OLD.price, 'new', NEW.price)
  FROM stores s WHERE s.id = NEW.store_id;
  RETURN NULL;
END
$$;
-- mizzle:breakpoint
CREATE TRIGGER products_price_changed AFTER UPDATE OF price ON products
FOR EACH ROW WHEN (OLD.price IS DISTINCT FROM NEW.price) EXECUTE FUNCTION price_changed();
-- mizzle:breakpoint
GRANT SELECT ON orders TO shop_authenticated;
-- mizzle:breakpoint
GRANT SELECT, INSERT, UPDATE, DELETE ON reviews TO shop_authenticated;
-- mizzle:breakpoint
GRANT SELECT, UPDATE ON notifications TO shop_authenticated;
-- mizzle:breakpoint
GRANT SELECT ON notifications TO shop_support;
-- mizzle:breakpoint
GRANT SELECT ON shop_audit.audit_log TO shop_support;
-- mizzle:breakpoint
GRANT SELECT ON product_ratings TO shop_authenticated;
-- mizzle:breakpoint
-- Policies call auth.uid() as the signed-in role, which needs to see the schema.
GRANT USAGE ON SCHEMA auth TO shop_authenticated;
-- mizzle:breakpoint
-- Inserts through RLS also need the serial's sequence.
GRANT USAGE ON SEQUENCE reviews_id_seq TO shop_authenticated;
