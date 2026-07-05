"""
tests/test_feed.py — Mixtape

Regression test for Issue #2: "Friends Listening Now shows people from yesterday."

Written after fixing services/feed_service.py. This test would have failed
against the original code, which used RECENT_THRESHOLD = timedelta(hours=24)
and therefore treated any event from the last 24 rolling hours as "happening
now" -- including a session from late the previous night.
"""

import pytest
from datetime import timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now
from datetime import datetime


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def friends(app):
    """Two friends and a song they can log listens against."""
    with app.app_context():
        me = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([me, friend])
        db.session.flush()

        db.session.execute(friendships.insert().values(user_id=me.id, friend_id=friend.id))
        db.session.execute(friendships.insert().values(user_id=friend.id, friend_id=me.id))

        song = Song(title="Late Night Track", artist="Someone", shared_by=me.id)
        db.session.add(song)
        db.session.commit()

        yield {"me": me, "friend": friend, "song": song}


def test_stale_yesterday_listen_does_not_appear_as_listening_now(app, friends):
    """
    A friend's only listening event from ~18 hours ago (last night) should
    NOT appear in "Listening Now" today. Under the original 24-hour rolling
    window, this event incorrectly passed the recency filter.
    """
    with app.app_context():
        me = db.session.get(User, friends["me"].id)
        friend = db.session.get(User, friends["friend"].id)
        song = db.session.get(Song, friends["song"].id)

        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(
            user_id=friend.id, song_id=song.id, listened_at=now - timedelta(hours=18)
        ))
        db.session.commit()

        feed = get_friends_listening_now(me.id)
        assert feed == []  # Bug: previously returned the 18-hour-old event


def test_genuinely_recent_listen_appears_as_listening_now(app, friends):
    """A friend who listened a few minutes ago should still show up."""
    with app.app_context():
        me = db.session.get(User, friends["me"].id)
        friend = db.session.get(User, friends["friend"].id)
        song = db.session.get(Song, friends["song"].id)

        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(
            user_id=friend.id, song_id=song.id, listened_at=now - timedelta(minutes=10)
        ))
        db.session.commit()

        feed = get_friends_listening_now(me.id)
        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "friend"
