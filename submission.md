# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

_To be completed in Milestone 4 after bug fixes are done._

---

## Codebase Map

_Written before starting any bug work, as part of Milestone 1 orientation._

### Overview

Mixtape is a Flask REST API for a social music app. Users share songs, rate each other's shares, build collaborative playlists, track listening streaks, and see what friends are listening to. The app uses SQLite via SQLAlchemy and has no frontend — all endpoints return JSON.

The architecture follows a clear **Routes → Services → Models** split. Routes handle HTTP (parsing request params/body, formatting JSON responses). Services contain all business logic. Models define the database schema and relationships.

### Main Files and Their Roles

| File | Role |
|------|------|
| `app.py` | Flask application factory (`create_app()`). Configures SQLite, initializes SQLAlchemy, registers four route blueprints under URL prefixes (`/songs`, `/playlists`, `/users`, `/feed`), and creates tables on startup. |
| `models.py` | Defines all SQLAlchemy models and three association tables. Every entity uses UUID string primary keys via `generate_uuid()`. |
| `routes/songs.py` | Song search, song detail, rating (`POST /songs/<id>/rate`), and listening events (`POST /songs/<id>/listen`). Delegates to `search_service`, `notification_service`, and `streak_service`. |
| `routes/playlists.py` | Playlist CRUD and adding songs to playlists. Delegates to `playlist_service` for reads/creates and `notification_service.add_to_playlist()` when a song is added. |
| `routes/users.py` | User profile lookup, streak retrieval, and notification listing/marking read. Delegates to `streak_service` and `notification_service`. |
| `routes/feed.py` | "Friends Listening Now" and general activity feed. Delegates to `feed_service`. |
| `services/streak_service.py` | Records `ListeningEvent` rows and updates `User.listening_streak` / `User.last_listened_at` based on consecutive calendar-day rules. |
| `services/feed_service.py` | Queries friends' recent `ListeningEvent` records to build listening-now and activity feeds. Uses a 24-hour recency threshold for "listening now." |
| `services/search_service.py` | Searches songs by title or artist (case-insensitive `ilike`), joining the `song_tags` table to include tag data. |
| `services/notification_service.py` | Creates and retrieves `Notification` records. Also handles `rate_song()` and `add_to_playlist()` — both of which can trigger notifications to the song's original sharer. |
| `services/playlist_service.py` | Creates playlists and retrieves playlist metadata and ordered song lists via the `playlist_entries` join table. |
| `seed_data.py` | Drops and recreates the database with 5 users, 13 songs (varying tag counts), 3 playlists, friendships, listening events, and sample notifications. Run once during setup. |
| `tests/` | Existing pytest files for streaks, search, and playlists. |

### Data Models

**Core models:** `User`, `Song`, `Tag`, `Playlist`, `ListeningEvent`, `Rating`, `Notification`

**Association tables:**
- `friendships` — bidirectional many-to-many between users
- `song_tags` — many-to-many between songs and tags (used by search)
- `playlist_entries` — many-to-many between playlists and songs, with an explicit `position` column, `added_by`, and `added_at` (songs have a defined order, not just insertion order)

**Notable fields:**
- `User.listening_streak` and `User.last_listened_at` — cached streak state updated on each listen
- `Song.shared_by` — the user who originally shared the song; used to determine who gets notifications
- `Rating` has a unique constraint on `(user_id, song_id)` — one rating per user per song

### Data Flow: Friend Adds Your Song to a Playlist

This flow shows how a social action triggers a notification — a pattern used across the app.

1. **HTTP entry point:** `POST /playlists/<playlist_id>/songs` with JSON body `{ "song_id": "...", "added_by": "..." }`
2. **Route:** `routes/playlists.py` → `add_song()` validates required fields, then calls `notification_service.add_to_playlist()`
3. **Service — add song:** `notification_service.add_to_playlist()` looks up the `Song`, `User` (adder), and `Playlist`. If the song isn't already in the playlist, it appends via `playlist.songs.append(song)` and commits.
4. **Service — notify sharer:** If `song.shared_by != added_by_user_id`, it calls `create_notification()` with type `"song_added_to_playlist"` and a human-readable body like `"kenji added your song '...' to the playlist '...'."`
5. **Service — persist notification:** `create_notification()` inserts a `Notification` row and commits.
6. **Retrieval:** The sharer can later fetch notifications via `GET /users/<user_id>/notifications`, which calls `notification_service.get_notifications()` and returns them ordered by `created_at` descending.

### Data Flow: User Listens to a Song (Streak Update)

1. **HTTP entry point:** `POST /songs/<song_id>/listen` with JSON body `{ "user_id": "..." }`
2. **Route:** `routes/songs.py` → `listen()` calls `streak_service.record_listening_event()`
3. **Service — record event:** Creates a `ListeningEvent` row with the current UTC timestamp.
4. **Service — update streak:** Calls `update_listening_streak(user, now)` which compares today's date to `user.last_listened_at`:
   - First listen ever → streak = 1
   - Already listened today → no change
   - Listened yesterday → streak increments
   - More than one day gap → streak resets to 1
5. **Updates:** Sets `user.last_listened_at = now` and commits.
6. **Retrieval:** Streak is readable via `GET /users/<user_id>/streak` → `streak_service.get_streak()` returns `user.listening_streak`.

### Patterns I Noticed

1. **Thin routes, fat services.** Every route function is 5–15 lines: parse input, call one service function, return JSON. No business logic lives in routes.
2. **Services raise `ValueError` for bad input.** Routes catch these and return 400/404 with the error message. This is consistent across all four route files.
3. **Models have `to_dict()` methods.** Services and routes never manually serialize — they call `model.to_dict()` for JSON responses.
4. **Cross-service imports where needed.** For example, `notification_service.add_to_playlist()` imports from `playlist_service`, and playlist song addition logic lives in the notification service (not the playlist service). Notifications are tightly coupled to social actions.
5. **UUIDs everywhere.** All IDs are string UUIDs, not integers. The seed data creates users like kenji, nova, darius with auto-generated IDs.
6. **No authentication layer.** User IDs are passed directly in request bodies/URLs — this is a simplified student project, not production auth.

---

## Root Cause Analysis Entries

### Issue #1 — My listening streak keeps resetting

**How you reproduced it:** Ran `pytest tests/test_streaks.py::test_streak_increments_on_sunday`, which simulates kenji's scenario: a user listens on Saturday (`2024-06-15`, `weekday() == 5`) then again on Sunday morning (`2024-06-16`, `weekday() == 6`) with an existing streak of 1. Before the fix, the Sunday listen reset the streak to 1 instead of incrementing to 2. This matches the user report of a 12-day streak dropping to 1 after listening on Sunday.

**How you found the root cause:** Started from the route `POST /songs/<song_id>/listen` in `routes/songs.py`, which calls `streak_service.record_listening_event()`. That calls `update_listening_streak()` in `services/streak_service.py`. Reading the streak rules in the docstring and comparing them to the `elif` branch on line 73 revealed an extra guard: `today.weekday() != 6`.

**The root cause:** Python's `datetime.weekday()` returns `6` for Sunday. The increment branch required `days_since_last == 1` (listened yesterday) **and** `today.weekday() != 6`. On Sunday, consecutive-day listens failed the second condition and fell through to the `else` branch, which resets the streak to 1 — even though only one calendar day had passed.

**Your fix and side-effect check:** Removed the `today.weekday() != 6` guard so any consecutive calendar day increments the streak, including Sunday after Saturday. Verified with `pytest tests/test_streaks.py` (all 5 tests pass). Also confirmed `test_streak_resets_after_skipped_day` still passes — skipping Tuesday between Monday and Wednesday still resets correctly.

---

### Issue #2 — Friends Listening Now shows people from yesterday

**How you reproduced it:** Wrote a script that mirrors nova's report: darius has a `ListeningEvent` at 11pm on June 16, and nova checks the feed at 9am on June 17. Called `get_friends_listening_now(nova.id)` with `datetime.now` patched to the morning time. Before the fix, darius appeared in the feed with `listened_at` from the previous night.

**How you found the root cause:** Traced `GET /feed/<user_id>/listening-now` in `routes/feed.py` to `feed_service.get_friends_listening_now()`. The docstring says friends who listened "recently," but the filter used `RECENT_THRESHOLD = timedelta(hours=24)` — a rolling 24-hour window, not "today."

**The root cause:** A friend who listened at 11pm yesterday is still within the last 24 hours at 9am the next morning, so their event passed the cutoff filter even though it was not from the current calendar day.

**Your fix and side-effect check:** Changed the filter from a rolling 24-hour window (`now - timedelta(hours=24)`) to the start of the current UTC calendar day (`today_start`). Verified the reproduction script returns 0 friends yesterday evening and 1 friend after a listen today. Confirmed `get_activity_feed()` was not changed — it intentionally returns historical events without a today-only filter.

---

### Issue #3 — The same song keeps showing up twice in search

**How you reproduced it:** Called `GET /songs/search?q=Anthem` against seeded data (Crown Heights Anthem has 3 tags). Also ran `db.session.query(Song.id).outerjoin(song_tags, ...).filter(title ilike '%Anthem%').all()` in a Python shell — this returned **3 identical song ID tuples** for one song, while songs with 0–1 tags returned the correct count. The bug is conditional: only songs with multiple tags are affected.

**How you found the root cause:** Traced `GET /songs/search` in `routes/songs.py` to `search_service.search_songs()`. Noticed an `outerjoin` on the `song_tags` association table even though the filter only checks `Song.title` and `Song.artist` — tags are not part of the search criteria.

**The root cause:** The unnecessary `outerjoin(song_tags)` multiplies SQL result rows — one row per tag on a matching song. A song with 3 tags produces 3 joined rows for a single title match, which surfaces as duplicate entries in the search results.

**Your fix and side-effect check:** Removed the `outerjoin` on `song_tags`. Tags are still included in each result via `Song.to_dict()`, which loads them through the model's `tags` relationship. Verified with `pytest tests/test_search.py` (all 5 tests pass, including `test_search_no_duplicates_multi_tag_song`).

---

### Issue #4 — Rating notifications never sent

**How you reproduced it:** In a Python shell, created aaliya (song sharer) and kenji (rater), shared a song owned by aaliya, then called `rate_song(kenji.id, song.id, 5)` and checked `get_notifications(aaliya.id)`. Before the fix, the notification count was 0 even though the rating was saved.

**How you found the root cause:** Compared `rate_song()` to `add_to_playlist()` line-by-line in `notification_service.py`. `add_to_playlist()` calls `create_notification()` for `song.shared_by` when someone else adds the song. `rate_song()` saved the `Rating` and committed but never called `create_notification()` — the notification step was simply missing.

**The root cause:** `rate_song()` was architecturally incomplete. It handled rating persistence but omitted the notification creation pattern that `add_to_playlist()` already implements for the same `song.shared_by` recipient.

**Your fix and side-effect check:** Added a `create_notification()` call after saving the rating when `song.shared_by != user_id`, using type `"song_rated"`. Verified the reproduction script now returns 1 notification. Confirmed self-ratings (`shared_by == user_id`) still do not generate a notification, matching the playlist-add behavior.

---

### Issue #5 — The last song in a playlist never shows up

**How you reproduced it:** Ran `pytest tests/test_playlists.py::test_playlist_returns_all_songs` with a playlist seeded with 5 songs. Before the fix, `get_playlist_songs()` returned 4 songs — always missing the most recently added (last) entry. `test_playlist_returns_songs_in_order` also failed because `Track 5` was absent.

**How you found the root cause:** Traced `GET /playlists/<playlist_id>/songs` in `routes/playlists.py` to `playlist_service.get_playlist_songs()`. The query correctly fetches all songs ordered by `position`, but the return statement sliced the list with `songs[:-1]`, dropping the final element.

**The root cause:** An off-by-one error in the return statement: `return [song.to_dict() for song in songs[:-1]]` excluded the last song in the ordered result. Adding a new song shifted which entry was "last," matching darius's report that the missing song rotated with each addition.

**Your fix and side-effect check:** Changed the return to `return [song.to_dict() for song in songs]` so all entries are included. Verified with `pytest tests/test_playlists.py` (all 3 tests pass). Confirmed empty playlists still return `[]` via `test_empty_playlist_returns_empty_list`.

---
