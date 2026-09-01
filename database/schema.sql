-- One row per parsed leaderboard result message. `game` namespaces the rows so
-- the gauntle/foodguessr/catfishing cogs share the table without colliding.
-- `payload` is a game-specific JSON blob (scores, categories, etc). Keyed by
-- message so re-scanning or editing a message upserts rather than duplicates.
CREATE TABLE IF NOT EXISTS `leaderboard_results` (
  `game` TEXT NOT NULL,
  `channel_id` TEXT NOT NULL,
  `message_id` TEXT NOT NULL,
  `author_id` TEXT NOT NULL,
  `author_name` TEXT NOT NULL,
  `played_on` TEXT NOT NULL,  -- ISO date (YYYY-MM-DD), used for month filtering
  `payload` TEXT NOT NULL,    -- game-specific JSON
  PRIMARY KEY (`game`, `message_id`)
);

CREATE INDEX IF NOT EXISTS `idx_leaderboard_lookup`
  ON `leaderboard_results` (`game`, `channel_id`, `played_on`);

-- Which channel holds each server's introduction messages (set by /introchannel).
CREATE TABLE IF NOT EXISTS `intro_channels` (
  `server_id` TEXT NOT NULL PRIMARY KEY,
  `channel_id` TEXT NOT NULL
);

-- Owner-supplied answers to "who is 'x'?" — maps a normalized "invited by" text
-- that couldn't be resolved automatically to a target: either 'id:<user id>' or
-- 'name:<unique key>' for people who aren't on Discord/the server.
CREATE TABLE IF NOT EXISTS `invite_aliases` (
  `server_id` TEXT NOT NULL,
  `alias` TEXT NOT NULL,
  `target` TEXT NOT NULL,
  PRIMARY KEY (`server_id`, `alias`)
);

-- Manual per-member corrections of invited-by attribution. `inviter` is
-- 'id:<user id>', 'name:<unique key>', or NULL for "explicitly nobody".
-- Overrides win over anything parsed from the member's intro message.
CREATE TABLE IF NOT EXISTS `invite_overrides` (
  `server_id` TEXT NOT NULL,
  `member_id` TEXT NOT NULL,
  `inviter` TEXT,
  PRIMARY KEY (`server_id`, `member_id`)
);

-- Extra per-puzzle info fetched from a game's website — e.g. Krillion's daily
-- prompts, which the site only serves on the day itself, so the bot archives
-- them as they appear. `payload` is a game-specific JSON blob.
CREATE TABLE IF NOT EXISTS `puzzle_info` (
  `game` TEXT NOT NULL,
  `puzzle` INTEGER NOT NULL,
  `payload` TEXT NOT NULL,
  PRIMARY KEY (`game`, `puzzle`)
);

-- Channels that get an automatic leaderboard post for a game on the 1st of
-- each month (opted in via /autopost).
CREATE TABLE IF NOT EXISTS `leaderboard_autopost` (
  `game` TEXT NOT NULL,
  `channel_id` TEXT NOT NULL,
  PRIMARY KEY (`game`, `channel_id`)
);

-- Tracks how much of each channel's history has already been scanned per game,
-- so a command only fetches messages it hasn't seen instead of re-scanning the
-- whole channel every time.
--   * `newest_id`    - highest message id scanned so far (forward catch-up).
--   * `oldest_after` - earliest `after` datetime (ISO) we've backfilled to.
CREATE TABLE IF NOT EXISTS `leaderboard_scan` (
  `game` TEXT NOT NULL,
  `channel_id` TEXT NOT NULL,
  `newest_id` TEXT,
  `oldest_after` TEXT,
  PRIMARY KEY (`game`, `channel_id`)
);
