# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

This entire project was completed by Claude (Anthropic), working directly against the cloned
repository with real code execution, not guessed or fabricated behavior. Specific ways AI was used:

- **Codebase orientation**: Read every file in `models.py`, `routes/`, and `services/` directly
  before opening any issue, and traced the call chain for each of the five affected services
  (route → service → model) to build the codebase map below.
- **Reproduction before fixing**: For every bug, wrote a small script that called the actual
  service function (or hit it through a Flask app context) with real seeded data, and printed
  before/after state, rather than assuming the bug existed from reading the code alone.
- **Where the reading led somewhere wrong and had to be corrected**: For Issue #3 (search
  duplicates), reading the code strongly suggested `search_songs`' `outerjoin` on `song_tags`
  would return 3 duplicate rows for a 3-tag song. Running `search_songs("Crown Heights")`
  against the seeded data returned **1** result, not 3 — the existing pytest suite (`test_search.py`)
  was passing. Rather than assume the bug didn't exist, dropped down to raw SQL execution
  (`db.session.execute(q.statement).all()`) and confirmed the join genuinely produces 3 rows
  at the database level — SQLAlchemy 2.0's legacy `Query.all()` API was silently
  auto-deduplicating full-entity results, masking the symptom in this specific dependency
  version. The root cause (the unneeded join) was real and still worth fixing even though the
  visible symptom wasn't reproducible with the currently installed SQLAlchemy version.
- **Where the reading led somewhere wrong for Issue #2**: Initially suspected a timezone
  string-comparison bug (naive vs. aware datetime comparison at the SQLite layer) was causing
  stale events to leak into the "Listening Now" feed. Verified the actual bound SQL parameter
  via `logging.getLogger('sqlalchemy.engine')` and found the comparison was technically correct.
  The real issue turned out to be simpler: `RECENT_THRESHOLD = timedelta(hours=24)` is just too
  loose a window for a feature branded "Listening Now" — a friend's session from 6+ hours ago
  legitimately passes a 24-hour filter. Confirmed this with an isolated in-memory test: a friend's
  only event, 18 hours old ("last night"), showed up in the feed.
- **Function explanation during navigation**: Used the pattern of asking "what does this return
  under X input" while reading `update_listening_streak` and `get_playlist_songs`, then verified
  every hypothesis by actually running the function with controlled inputs (see reproduction
  scripts referenced in each RCA entry below) before writing any fix.

---

## Codebase Map

**`app.py`** — Flask application factory (`create_app`). Initializes a single shared
`SQLAlchemy` `db` instance, registers four blueprints (`songs`, `playlists`, `users`, `feed`)
under their respective URL prefixes, and calls `db.create_all()` on startup.

**`models.py`** — Defines all SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`,
`Rating`, `Playlist`, `Notification`. Three association tables back the many-to-many
relationships: `friendships` (symmetric, self-referential on `User`), `song_tags` (Song ↔ Tag),
and `playlist_entries` (Playlist ↔ Song, with extra `position`, `added_by`, `added_at` columns —
this is not a plain many-to-many, it's an ordered join with metadata). `User.friends` is a
`lazy="dynamic"` self-referential relationship built off `friendships`.

**`routes/`** — Thin Flask blueprints. Every route parses the request, delegates immediately to
a function in `services/`, and formats the JSON response. No business logic lives here.
- `songs.py`: search, get-by-id, rate, listen (record a listening event / update streak)
- `playlists.py`: create, get metadata, get songs, add song
- `users.py`: get user, get streak, get/mark notifications
- `feed.py`: "listening now" and general activity feed

**`services/`** — All business logic:
- `streak_service.py`: `record_listening_event` creates a `ListeningEvent` and calls
  `update_listening_streak`, which compares `today` to the user's `last_listened_at.date()`
  to decide whether to no-op (same day), increment (exactly 1 day gap), or reset to 1
  (any bigger gap).
- `feed_service.py`: `get_friends_listening_now` filters recent `ListeningEvent`s for a user's
  friends within a `RECENT_THRESHOLD` window, then deduplicates to one entry per friend
  (most recent song only). `get_activity_feed` is explicitly *not* recency-filtered — it just
  returns the most recent N events regardless of age, by design (per its own docstring).
- `search_service.py`: `search_songs` filters `Song` by title/artist substring match
  (case-insensitive).
- `notification_service.py`: `create_notification` is the single low-level insert used by
  two call sites — `add_to_playlist` (notifies the song's original sharer when someone else
  adds it to a playlist) and `rate_song` (saves a `Rating`). `get_notifications` and
  `mark_as_read` round out retrieval.
- `playlist_service.py`: `get_playlist_songs` joins `Song` through the `playlist_entries`
  association table, ordered by `position`, to return an ordered song list for a playlist.

**Data flow — a user rates a song**: `POST /songs/<id>/rate` (`routes/songs.py`) → parses
`user_id`/`score` from the JSON body → calls `notification_service.rate_song(user_id, song_id, score)`
→ looks up the `Song` and rating `User`, upserts a `Rating` row (unique on `user_id`+`song_id`
via a table constraint), commits. *(Before the fix below, this is where the flow stopped —
see Issue #4.)*

**Pattern noticed**: every "write" service function that changes state on behalf of one user but
affects another user's object (adding to a playlist, rating a song) is expected to also produce
a `Notification` for the affected party. `add_to_playlist` follows this pattern; `rate_song` did
not, despite being structurally identical (same "if actor != owner, notify" shape).

---

## Root Cause Analysis

### Issue #1: My listening streak keeps resetting

**How I reproduced it**: Called `update_listening_streak(user, now)` directly in an app context
for a user whose `last_listened_at` was set to "yesterday," with `now` set to the real current
timestamp — which happens to be a Sunday (July 5, 2026) at the time of this investigation. The
user's streak went from 3 to 1 instead of incrementing to 4.

**How I found the root cause**: Read `streak_service.py` top to bottom. The docstring for
`update_listening_streak` states the rule plainly: "If the user listened yesterday: streak
increments by 1." The code, however, guards the increment branch with
`elif days_since_last == 1 and today.weekday() != 6:`. I confirmed `datetime.weekday()` returns
6 for Sunday (0 = Monday) via a one-line check, which immediately explained the extra condition.

**The root cause**: `today.weekday() != 6` is checked in addition to `days_since_last == 1`. On
any Sunday, this condition is `False` regardless of how many consecutive days the user has
listened, so the code falls through to the `else` branch and resets `listening_streak` to 1
instead of incrementing it. There's no comment or business reason justifying a Sunday
exception — it appears to be an unintended artifact of `weekday() == 0` (Monday) being confused
with `weekday() == 6` (Sunday), or a copy-paste of week-boundary logic that doesn't belong in a
simple day-over-day comparison.

**My fix and side-effect check**: Removed the `and today.weekday() != 6` clause entirely, so any
single-day gap increments the streak regardless of which day of the week it falls on. Verified
against the full existing `test_streaks.py` suite (5 tests: new user, consecutive day, same-day
no-op, skipped-day reset, and the Sunday-specific regression test) — all pass. The skipped-day
reset path (`days_since_last > 1`) and same-day no-op path were untouched by this change.

---

### Issue #2: Friends Listening Now shows people from yesterday

**How I reproduced it**: First checked whether the seeded data's own "should not appear" older
events (1-14 days old) leaked into `get_friends_listening_now` — they didn't; the 24-hour cutoff
correctly excluded anything older than a day. To find the actual complaint, I built an isolated
in-memory scenario: one friend with a single `ListeningEvent` timestamped 18 hours ago (e.g.
"11pm last night" relative to "5pm today"). `get_friends_listening_now` returned that friend as
currently listening.

**How I found the root cause**: My first hypothesis was a timezone bug — SQLite stores naive
datetimes, and the query compares against a timezone-aware `cutoff`. I checked the actual bound
SQL parameter via SQLAlchemy engine logging and confirmed the dialect strips `tzinfo` before
binding, so the comparison is a valid apples-to-apples string comparison — not a timezone bug.
That ruled out my first theory. Re-reading `RECENT_THRESHOLD = timedelta(hours=24)` against the
feature's name — "Listening *Now*" — made the real issue obvious: a full rolling day is not
"now" by any reasonable product definition, and the seed data's own comments distinguish
"within the past 30 minutes" (should show) from "1-14 days ago" (should not show) — a 24-hour
threshold happens to separate those two buckets correctly, which is why the shipped tests never
caught it, but it does nothing to prevent a legitimately day-old ("yesterday's") session from
qualifying as "now."

**The root cause**: `RECENT_THRESHOLD` is set to 24 hours, which is far looser than the feature
it gates. Any friend activity within a full rolling day — including a listening session from
late the previous night — passes the `listened_at >= cutoff` filter and is presented to the user
as "currently listening," which is where the "shows people from yesterday" complaint comes from.

**My fix and side-effect check**: Reduced `RECENT_THRESHOLD` to 30 minutes, aligned with the
"within the past 30 minutes" bucket already used in `seed_data.py`'s intended recent-events
demo. Checked `get_activity_feed`, the other function in the same file, to confirm it doesn't
reference `RECENT_THRESHOLD` at all (its docstring explicitly says it is *not* recency-filtered
by design) — ran it before and after the change and got the same count both times, confirming
no unintended effect. Added a regression test (`tests/test_feed.py`, described below).

---

### Issue #3: The same song keeps showing up twice in search

**How I reproduced it**: Called `search_songs("Crown Heights")` against a seeded song with 3
tags, expecting 3 duplicate results per the seed data's own comment
("these are the ones that expose Issue #3"). It returned exactly 1 result. Running the existing
`test_search.py` suite also showed all 5 tests passing, including the one specifically checking
for this duplicate.

**How I found the root cause**: Rather than conclude there was no bug, I compiled the actual SQL
statement `search_songs` builds and ran it two ways: once through the raw `db.session.execute()`
Core API, and once through the same query object's ORM-level `.all()`. The raw execution
returned **3 rows** (all referencing the same song, once per tag row from the `LEFT OUTER JOIN`
onto `song_tags`) — confirming the join does fan out at the database level exactly as expected.
The ORM-level `.all()` returned 1, because SQLAlchemy 2.0's legacy `Query` API auto-deduplicates
full-entity results in a way the newer `session.execute(select(...))` API does not. That's the
moment I was confident in the root cause: the join itself is genuinely wrong, but the currently
pinned SQLAlchemy version happens to paper over the symptom.

**The root cause**: `search_songs` performs `.outerjoin(song_tags, Song.id == song_tags.c.song_id)`
but never uses any column from `song_tags` in its `.filter()` or select list — the search only
matches on `Song.title`/`Song.artist`. The join is unnecessary and, because a song with N tags
has N rows in `song_tags`, produces N duplicate rows in the raw query result for any song with
more than one tag. This is currently invisible through `search_songs` only because of an
implementation detail of the legacy ORM `Query` API, not because the join is actually correct —
the moment this code is rewritten in 2.0-style (`session.execute(select(Song)...)`, which
SQLAlchemy's own docs recommend going forward), the duplicates would immediately reappear, and
any code path this session doesn't auto-dedupe (raw SQL, a different ORM call shape, adding
`.limit()` before the fan-out is deduplicated) is equally exposed today.

**My fix and side-effect check**: Removed the `outerjoin` entirely, along with the now-unused
`Tag`/`song_tags` imports. Confirmed with the raw-SQL check that the query now returns exactly
1 row for the 3-tag song. Confirmed tags are still populated correctly in the response (`song.tags`
comes from the separate `Song.tags` relationship inside `to_dict()`, entirely independent of this
join) for both a 3-tag and a 1-tag song. Ran the full `test_search.py` suite — all 5 tests still
pass.

---

### Issue #4: Notified for playlist adds but not for ratings

**How I reproduced it**: Called `rate_song(rater_id, song_id, score)` for a rater who was not the
song's original sharer, then checked the sharer's `Notification` count before and after. It was
unchanged — no notification was created.

**How I found the root cause**: Per the hint, I compared `rate_song` line-by-line against
`add_to_playlist`, the other function in the same file that's supposed to follow the same
"notify the owner" pattern. `add_to_playlist` ends with an explicit
`if song.shared_by != added_by_user_id: create_notification(...)` block. `rate_song` has no
equivalent block anywhere — it saves or updates the `Rating` and commits, and the function simply
ends. This isn't a broken condition or a typo; the call to `create_notification` for the ratings
path was never written.

**The root cause**: `rate_song` never calls `create_notification`, unlike the structurally
identical `add_to_playlist`, which does. The omission is architectural — a whole notification
path is missing, not a single wrong comparison or variable.

**My fix and side-effect check**: Added a `create_notification(user_id=song.shared_by, ...)` call
after the rating commit, guarded by `if song.shared_by != user_id`, mirroring `add_to_playlist`'s
pattern exactly. Verified: (1) a rating from a different user creates exactly one new
notification for the sharer; (2) a user rating their own song does *not* create a notification
(count unchanged before/after); (3) re-ran `add_to_playlist` against a song already in a playlist
and confirmed its own notification path still fires once, unaffected by this change.

---

### Issue #5: The last song in a playlist never shows up

**How I reproduced it**: Ran the existing `test_playlists.py::test_playlist_returns_all_songs`
test, which seeds a playlist with 5 songs at positions 1-5 and asserts `get_playlist_songs`
returns all 5. It returned 4 — missing "Track 5," the last one by position.

**How I found the root cause**: `get_playlist_songs` builds a query that joins through
`playlist_entries`, filters by `playlist_id`, and orders by `position` ascending — this part is
correct and returns all rows in the right order at the query level. The very next line,
`return [song.to_dict() for song in songs[:-1]]`, slices off the last element of the
already-correctly-ordered list before returning it. The function's own docstring even states
"Note: This function returns all songs in the playlist," directly contradicted by the slice.

**The root cause**: A `[:-1]` slice on the final line drops whichever song has the highest
`position` value in the playlist — always the most recently added song — regardless of how many
songs the playlist has.

**My fix and side-effect check**: Removed the `[:-1]` slice so the full ordered list is returned.
Verified against the existing 5-song playlist test (now returns all 5, in the correct
Track 1→5 order) and against `test_empty_playlist_returns_empty_list` (an empty playlist still
correctly returns `[]`, since slicing was never what made that case work).

---

## Note on an unrelated finding (out of scope)

While verifying Issue #4's side effects, I found that `add_to_playlist` raises a
`sqlite3.IntegrityError` whenever it's called with a song that is *not already* in the target
playlist, because `playlist.songs.append(song)` only populates the `song_id`/`playlist_id`
columns of the `playlist_entries` association table via the ORM relationship, leaving the
NOT-NULL `position` and `added_by` columns unset. This is a real, separate bug, but it isn't one
of the five tracked issues, so I didn't fix it here — flagging it in case it's worth its own
ticket.

---

## Regression Test

Added `tests/test_feed.py` (no test file previously existed for `feed_service.py`). It contains:
- `test_stale_yesterday_listen_does_not_appear_as_listening_now` — would have failed against the
  original 24-hour threshold; asserts an 18-hour-old event is excluded.
- `test_genuinely_recent_listen_appears_as_listening_now` — control test confirming a 10-minute-old
  event still shows, so the fix doesn't over-correct.

## Commits

All 5 fixes are on `bugfix/mixtape`, one commit per bug:

```
dcafa92 fix: remove off-by-one slice dropping the last playlist song
20f6895 fix: send a notification when a friend rates your song
d8a248a fix: remove unnecessary outerjoin on song_tags in search_songs
c130cd5 fix: tighten Listening Now window from 24 hours to 30 minutes
b2dd967 fix: remove incorrect Sunday exclusion from streak increment check
```
