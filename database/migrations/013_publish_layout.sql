-- Per-site article layout: how pictures are placed so the post matches the site's own house style.
-- {hero_in_body: bool, image_position: 'under_heading'|'as_placed', image_captions: bool}
ALTER TABLE publish_targets ADD COLUMN IF NOT EXISTS layout JSONB NOT NULL DEFAULT '{}'::jsonb;
INSERT INTO schema_migrations (version) VALUES ('013_publish_layout') ON CONFLICT DO NOTHING;
