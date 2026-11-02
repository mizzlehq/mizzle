-- create enum visibility
-- create table users
-- create table follows
-- create table posts
-- create table comments
-- create table likes
-- create index follows_followee_idx
-- create index posts_author_created_idx
-- create index comments_post_idx
-- create index likes_post_idx
-- add foreign key follows_follower_id_fkey
-- add foreign key follows_followee_id_fkey
-- add foreign key posts_author_id_fkey
-- add foreign key comments_post_id_fkey
-- add foreign key comments_author_id_fkey
-- add foreign key likes_user_id_fkey
-- add foreign key likes_post_id_fkey
CREATE TYPE "visibility" AS ENUM ('public', 'followers', 'private');
-- mizzle:breakpoint
CREATE TABLE "users" (
    "id" serial NOT NULL,
    "username" character varying(32) NOT NULL,
    "email" character varying(255) NOT NULL,
    "display_name" text NOT NULL,
    "bio" text,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "users_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "users_username_key" UNIQUE ("username"),
    CONSTRAINT "users_email_key" UNIQUE ("email"),
    CONSTRAINT "users_username_format" CHECK (username ~ '^[a-z0-9_]{3,32}$')
);
-- mizzle:breakpoint
CREATE TABLE "follows" (
    "follower_id" integer NOT NULL,
    "followee_id" integer NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "follows_pkey" PRIMARY KEY ("follower_id", "followee_id"),
    CONSTRAINT "follows_not_self" CHECK (follower_id <> followee_id)
);
-- mizzle:breakpoint
CREATE TABLE "posts" (
    "id" serial NOT NULL,
    "author_id" integer NOT NULL,
    "body" text NOT NULL,
    "visibility" visibility NOT NULL DEFAULT 'public',
    "meta" jsonb,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "posts_pkey" PRIMARY KEY ("id"),
    CONSTRAINT "posts_body_not_empty" CHECK (length(body) > 0)
);
-- mizzle:breakpoint
CREATE TABLE "comments" (
    "id" serial NOT NULL,
    "post_id" integer NOT NULL,
    "author_id" integer NOT NULL,
    "body" text NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "comments_pkey" PRIMARY KEY ("id")
);
-- mizzle:breakpoint
CREATE TABLE "likes" (
    "user_id" integer NOT NULL,
    "post_id" integer NOT NULL,
    "created_at" timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT "likes_pkey" PRIMARY KEY ("user_id", "post_id")
);
-- mizzle:breakpoint
CREATE INDEX "follows_followee_idx" ON "follows" ("followee_id");
-- mizzle:breakpoint
CREATE INDEX "posts_author_created_idx" ON "posts" ("author_id", "created_at");
-- mizzle:breakpoint
CREATE INDEX "comments_post_idx" ON "comments" ("post_id");
-- mizzle:breakpoint
CREATE INDEX "likes_post_idx" ON "likes" ("post_id");
-- mizzle:breakpoint
ALTER TABLE "follows" ADD CONSTRAINT "follows_follower_id_fkey" FOREIGN KEY ("follower_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "follows" ADD CONSTRAINT "follows_followee_id_fkey" FOREIGN KEY ("followee_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "posts" ADD CONSTRAINT "posts_author_id_fkey" FOREIGN KEY ("author_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "comments" ADD CONSTRAINT "comments_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "posts" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "comments" ADD CONSTRAINT "comments_author_id_fkey" FOREIGN KEY ("author_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "likes" ADD CONSTRAINT "likes_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE CASCADE;
-- mizzle:breakpoint
ALTER TABLE "likes" ADD CONSTRAINT "likes_post_id_fkey" FOREIGN KEY ("post_id") REFERENCES "posts" ("id") ON DELETE CASCADE;
