"""
tests/test_feed.py — Mixtape

Regression tests for Friends Listening Now feed logic.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app, db
from models import User, Song, ListeningEvent, friendships
from services.feed_service import get_friends_listening_now


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def friends_with_yesterday_listen(app):
    with app.app_context():
        nova = User(username="nova", email="nova@test.com")
        darius = User(username="darius", email="darius@test.com")
        db.session.add_all([nova, darius])
        db.session.flush()
        db.session.execute(friendships.insert().values(user_id=nova.id, friend_id=darius.id))
        db.session.execute(friendships.insert().values(user_id=darius.id, friend_id=nova.id))
        song = Song(title="Late Night Track", artist="DJ", shared_by=darius.id)
        db.session.add(song)
        db.session.flush()
        db.session.add(
            ListeningEvent(
                user_id=darius.id,
                song_id=song.id,
                listened_at=datetime(2024, 6, 16, 23, 0, 0, tzinfo=timezone.utc),
            )
        )
        db.session.commit()
        yield nova, darius


def test_listening_now_excludes_yesterday_evening(app, friends_with_yesterday_listen):
    """
    Regression: friends who listened yesterday evening should not appear
    in the listening-now feed the next morning.
    """
    with app.app_context():
        nova, _ = friends_with_yesterday_listen
        nova = db.session.get(User, nova.id)
        morning = datetime(2024, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
        with patch("services.feed_service.datetime") as mock_dt:
            mock_dt.now.return_value = morning
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            feed = get_friends_listening_now(nova.id)
        assert feed == []


def test_listening_now_includes_today(app, friends_with_yesterday_listen):
    """Friends who listened today should appear in the feed."""
    with app.app_context():
        nova, darius = friends_with_yesterday_listen
        nova = db.session.get(User, nova.id)
        song = db.session.query(Song).first()
        morning = datetime(2024, 6, 17, 9, 0, 0, tzinfo=timezone.utc)
        db.session.add(
            ListeningEvent(
                user_id=darius.id,
                song_id=song.id,
                listened_at=datetime(2024, 6, 17, 8, 0, 0, tzinfo=timezone.utc),
            )
        )
        db.session.commit()
        with patch("services.feed_service.datetime") as mock_dt:
            mock_dt.now.return_value = morning
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            feed = get_friends_listening_now(nova.id)
        assert len(feed) == 1
        assert feed[0]["friend"]["username"] == "darius"
