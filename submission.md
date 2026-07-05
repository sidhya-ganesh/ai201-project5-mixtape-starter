# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

I used AI tools (Claude) a handful of times during this project, mostly to speed up reading
code I hadn't written and to sanity-check things I wasn't 100% sure about — not to find bugs
for me. Specific instances:

- **Tracing the notification data flow**: Before touching any issue, I pasted `notification_service.py`
  and `routes/songs.py` into Claude and asked it to trace what happens end-to-end when a song
  gets rated. It walked me through the route → service → DB call chain, which helped me build
  the codebase map below faster than reading cold. I double-checked its trace against the actual
  file myself before writing anything down — it was accurate.
- **`weekday()` vs `isoweekday()`**: Once I'd narrowed Issue #1 down to a date comparison in
  `streak_service.py`, I asked Claude what `datetime.weekday()` actually returns for each day,
  since I wasn't sure off the top of my head whether Sunday was 0 or 6. It confirmed Sunday = 6,
  Monday = 0. I verified this myself with a one-line `python3 -c` check before trusting it, since
  getting this backwards would've sent me chasing the wrong day.
- **SQLAlchemy join behavior**: For Issue #3, my read of `search_songs` said the `outerjoin` on
  `song_tags` should produce duplicate rows for any song with more than one tag, but my own test
  run wasn't showing duplicates. I asked Claude why an ORM query with a fan-out join might return
  deduplicated results even when the raw SQL wouldn't. It pointed me toward SQLAlchemy's legacy
  `Query.all()` doing automatic entity deduplication, versus the newer `select()`/`execute()` API
  which doesn't. I verified this myself by literally running both call styles against the same
  query and comparing counts (3 raw rows vs. 1 ORM row) — that's what actually confirmed the
  explanation rather than just taking the AI's word for it.
- **Where I had to push back / correct course**: When I first described Issue #2 to Claude, it
  suggested the bug was probably a timezone comparison issue (naive vs. aware datetimes at the
  SQLite layer). That felt plausible, but when I actually turned on SQLAlchemy's engine logging
  to see the real bound SQL parameter, the comparison was fine — no timezone bug. The AI's first
  guess was wrong; I only figured out the real issue (the 24-hour threshold itself being too
  loose for a feature called "Listening Now") after ruling that out myself and going back to
  just reading `RECENT_THRESHOLD` against what the feature is supposed to mean.

In general: AI was useful for explaining unfamiliar syntax/behavior and for a first-pass trace of
files I hadn't read yet, but every actual diagnosis came from running the code myself with
controlled inputs. The one time I trusted an AI-suggested root cause without checking it first
(Issue #2's timezone theory) turned out to be a dead end, which is exactly why I didn't skip the
verification step on the others.

---

## Codebase Map

**`app.py`** — Flask application factory (`create_app`). Sets up a shared `SQLAlchemy` `db`
instance, registers four blueprints (`songs`, `playlists`, `users`, `feed`), and calls
`db.create_all()` on startup.

**`models.py`** — All the SQLAlchemy models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`,
`Playlist`, `Notification`. Three association tables handle the many-to-many relationships:
`friendships` (self-referential on `User`, inserted both directions so friendship reads as
symmetric), `song_tags` (Song ↔ Tag), and `playlist_entries` (Playlist ↔ Song — this one isn't a
plain join table, it has its own `position`, `added_by`, and `added_at` columns, which is how
playlists keep song order). `User.friends` is a `lazy="dynamic"` relationship built off
`friendships`.

**`routes/`** — Just parses requests and calls into `services/`, then formats the JSON response.
No actual logic lives in routes.
- `songs.py`: search, get-by-id, rate, listen
- `playlists.py`: create, metadata, get songs, add song
- `users.py`: get user, get streak, notifications
- `feed.py`: listening-now feed and general activity feed

**`services/`** — Where the actual logic is:
- `streak_service.py` — `update_listening_streak` compares `today` to the user's
  `last_listened_at.date()`: same day = no-op, exactly 1 day gap = increment, anything bigger =
  reset to 1.
- `feed_service.py` — `get_friends_listening_now` pulls recent `ListeningEvent`s for a user's
  friends inside a `RECENT_THRESHOLD` window, then keeps only the most recent one per friend.
  `get_activity_feed` is a separate function that deliberately ignores recency (per its own
  docstring) and just returns the latest N events overall.
- `search_service.py` — `search_songs` matches on title/artist substring.
- `notification_service.py` — `create_notification` is the shared insert function used by
  `add_to_playlist` (notifies the sharer when someone else adds their song to a playlist) and
  `rate_song` (saves a `Rating`).
- `playlist_service.py` — `get_playlist_songs` joins through `playlist_entries` ordered by
  `position` to return songs in the right order.

**Data flow — rating a song**: `POST /songs/<id>/rate` → `notification_service.rate_song` →
looks up the `Song`, upserts a `Rating` row, commits. That's where the flow stopped before my
fix below — no notification ever got created for the sharer.

**Pattern I noticed**: `add_to_playlist` and `rate_song` are structurally the same shape — both
let one user affect something belonging to another user, and both should notify the owner. Only
`add_to_playlist` actually does.

---

## Root Cause Analysis

### Issue #1: My listening streak keeps resetting

**How I reproduced it**: Set a test user's `last_listened_at` to yesterday and called
`update_listening_streak` with today's date, which happened to fall on a Sunday. Expected the
streak to go from 3 to 4; it reset to 1 instead.

**How I found the root cause**: Read through `streak_service.py`. The docstring says a
one-day gap should increment the streak. The actual condition was
`elif days_since_last == 1 and today.weekday() != 6:` — there's an extra clause I hadn't
expected. I wasn't sure which day `weekday()` returns 6 for, so I checked it directly, which
confirmed 6 = Sunday.

**The root cause**: The extra `today.weekday() != 6` check means the increment branch never
fires on a Sunday, no matter how valid the streak otherwise is, so it falls through to the reset
branch. There's no reason given in the code for treating Sunday differently — it looks like a
stray condition that doesn't belong in a simple consecutive-day check.

**My fix and side-effect check**: Deleted the weekday condition so a 1-day gap always increments.
Re-ran the full `test_streaks.py` suite (new user, consecutive day, same-day no-op, skipped-day
reset, Sunday case) — all 5 passed. Also manually checked that the skipped-day reset path still
works the same as before, since I only touched the increment condition.

---

### Issue #2: Friends Listening Now shows people from yesterday

**How I reproduced it**: Built a small test scenario — one friend with a single listening event
from 18 hours ago, nothing more recent. Called `get_friends_listening_now` and that friend still
showed up as "listening now."

**How I found the root cause**: My first guess (with some AI input) was a timezone mismatch
between how the timestamp is stored and how the cutoff is computed. I checked the actual SQL
being run via SQLAlchemy's engine logging and the comparison was correct — not a timezone bug.
So I went back to just reading the threshold value itself: `RECENT_THRESHOLD = timedelta(hours=24)`.
For a feature called "Listening Now," a full rolling day is way too generous a window — anything
from the last 24 hours passes, including a session from late the night before, which is exactly
what "yesterday" describes.

**The root cause**: The threshold is set to 24 hours, which doesn't match what "now" should mean
for this feature. A friend's listening session from many hours ago legitimately clears that bar
and gets shown as current activity.

**My fix and side-effect check**: Shrank `RECENT_THRESHOLD` to 30 minutes, matching what the
seed data treats as genuinely "recent." Checked `get_activity_feed` in the same file to make sure
it doesn't use this constant at all (it doesn't, by design) — ran it before and after and got the
same result both times. Wrote a regression test (below) so this doesn't regress silently again.

---

### Issue #3: The same song keeps showing up twice in search

**How I reproduced it**: Searched for a song I knew had 3 tags, expecting 3 duplicate results.
Got exactly 1. The existing search tests were all passing too, which was confusing since the code
looked wrong to me.

**How I found the root cause**: I compared what the raw SQL statement `search_songs` builds
actually returns versus what the ORM `.all()` call returns, using the same query object both
ways. Raw SQL: 3 rows. ORM: 1 row. With Claude's help understanding *why* those numbers differed
(SQLAlchemy's legacy Query API auto-deduplicates full-entity results, which the newer
`session.execute(select(...))` style doesn't), I confirmed the join genuinely fans out at the
database level — the current code just happens to be shielded from showing it because of which
SQLAlchemy call style is used.

**The root cause**: `search_songs` joins `song_tags` but never actually filters or selects
anything from it — the search only matches on title/artist. Since a song with N tags has N rows
in `song_tags`, the join produces N duplicate rows at the SQL level for any multi-tag song. It's
not visible through the current code path, but it's a real defect that would resurface the moment
this query gets rewritten in more modern SQLAlchemy style.

**My fix and side-effect check**: Removed the unnecessary join and its now-unused imports.
Confirmed via the raw-SQL check that it now returns exactly 1 row for a 3-tag song. Also checked
that tags still show up correctly in the response — they come from a completely separate
relationship in `to_dict()`, unrelated to this join. Full `test_search.py` suite still passes.

---

### Issue #4: Notified for playlist adds but not for ratings

**How I reproduced it**: Had one user rate another user's song, then checked the sharer's
notification count before and after. Unchanged — nothing was created.

**How I found the root cause**: Per the hint, I compared `rate_song` against `add_to_playlist`
line by line. `add_to_playlist` ends with a clear "notify the owner if it wasn't them" block.
`rate_song` just saves the rating and returns — there's no notification call anywhere in it.

**The root cause**: `rate_song` never calls `create_notification` at all. It's not a broken
condition, it's a missing block — the notification path for ratings was never written, even
though the pattern already exists elsewhere in the same file.

**My fix and side-effect check**: Added the same "notify if `song.shared_by != user_id`" block
that `add_to_playlist` uses. Verified: a rating from someone else creates one notification;
rating your own song creates none; and `add_to_playlist`'s existing notification behavior is
unchanged.

---

### Issue #5: The last song in a playlist never shows up

**How I reproduced it**: Ran the existing test that seeds a 5-song playlist and expects all 5
back from `get_playlist_songs`. Got 4 — the last one (by position) was missing.

**How I found the root cause**: The query itself — join through `playlist_entries`, filter by
playlist, order by position — looked correct and returns the right order. The very last line,
though, was `return [song.to_dict() for song in songs[:-1]]`. That slice drops the last item off
an already-correct list right before returning it.

**The root cause**: An off-by-one slice on the return line drops whichever song has the highest
position — always the most recently added one — regardless of playlist size.

**My fix and side-effect check**: Removed the `[:-1]` slice. Re-ran the 5-song test (now returns
all 5 in order) and the empty-playlist test (still correctly returns `[]`).

---

## Note on an unrelated finding (out of scope)

While testing Issue #4, I ran into a separate crash in `add_to_playlist` — it fails with an
integrity error if the song being added isn't already in the playlist, because the `position` and
`added_by` columns never get set through the relationship's `.append()`. Not one of the five
tracked issues, so I left it alone, but flagging it here in case it's worth reporting separately.

---

## Regression Test

Added `tests/test_feed.py`, since no test file existed for `feed_service.py` before this:
- `test_stale_yesterday_listen_does_not_appear_as_listening_now` — an 18-hour-old event should
  not show up; this would have failed against the original 24-hour threshold.
- `test_genuinely_recent_listen_appears_as_listening_now` — control test making sure a 10-minute-old
  event still shows.

## Commits

```
dcafa92 fix: remove off-by-one slice dropping the last playlist song
20f6895 fix: send a notification when a friend rates your song
d8a248a fix: remove unnecessary outerjoin on song_tags in search_songs
c130cd5 fix: tighten Listening Now window from 24 hours to 30 minutes
b2dd967 fix: remove incorrect Sunday exclusion from streak increment check
```
