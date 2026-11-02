-- create table bookmarks
-- create index bookmarks_post_idx
-- add foreign key bookmarks_user_id_fkey
-- add foreign key bookmarks_post_id_fkey
CREATE TABLE "bookmarks" (
    "user_id" integer NOT NULL,
    "post_id" integer NOT NULL,
    "note" text,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "bookmarks_pkey" PRIMARY KEY ("user_id", "post_id")
);
-- mizzle:breakpoint
CREATE INDEX "bookmarks_post_idx" ON "bookmarks" ("post_id");
-- mizzle:breakpoint
ALTER TABLE "bookmarks" ADD CONSTRAINT "bookmarks_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "bookmarks" ADD CONSTRAINT "bookmarks_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "posts" ("id") ON DELETE CASCADE;
