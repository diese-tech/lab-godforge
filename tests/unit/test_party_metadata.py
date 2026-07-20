import sqlite3

from utils.party import Participant, PlayerPreferences
from utils.party_store import SQLitePartyRepository


def test_lobby_metadata_and_participant_preferences_round_trip(tmp_path):
    repo = SQLitePartyRepository(tmp_path / "party.sqlite3")
    lobby = repo.create(
        guild_id=1,
        organizer_id=2,
        capacity=10,
        lobby_id="meta",
        operation_id="create",
        mode="Ranked",
        region="NA East",
        format="Conquest 5v5",
        voice_required=True,
        skill_band="Intermediate",
        notes="Be kind.",
    )
    changed = repo.save_participant(
        1,
        lobby.lobby_id,
        Participant(
            3,
            primary_role="Jungle",
            secondary_role="Mid",
            fill=True,
            captain=True,
        ),
        operation_id="join",
    )

    assert (changed.mode, changed.region, changed.format) == (
        "ranked",
        "na east",
        "conquest 5v5",
    )
    assert changed.voice_required is True
    assert changed.skill_band == "intermediate"
    assert changed.notes == "Be kind."
    participant = changed.participant(3)
    assert participant.primary_role == "jungle"
    assert participant.secondary_role == "mid"
    assert participant.fill is True
    assert participant.captain is True


def test_organizer_can_edit_lobby_metadata_idempotently(tmp_path):
    repo = SQLitePartyRepository(tmp_path / "party.sqlite3")
    repo.create(
        guild_id=1, organizer_id=2, capacity=10, lobby_id="edit-me",
        operation_id="create",
    )
    changed = repo.update_metadata(
        1, "edit-me", operation_id="edit", actor_id=2, mode="Arena",
        region="EU", format="PUG", capacity=8, voice_required=True,
        notes="fast games",
    )
    retried = repo.update_metadata(
        1, "edit-me", operation_id="edit", actor_id=2, mode="Arena",
        region="EU", format="PUG", capacity=8, voice_required=True,
        notes="fast games",
    )

    assert changed.mode == "arena"
    assert changed.capacity == 8
    assert retried.version == changed.version


def test_authoritative_preferences_are_structured_and_guild_scoped(tmp_path):
    repo = SQLitePartyRepository(tmp_path / "party.sqlite3")
    saved = repo.set_player_preferences(
        1,
        7,
        primary_role="Support",
        secondary_role="Solo",
        fill=True,
        captain=True,
    )

    assert saved == PlayerPreferences("support", "solo", True, True)
    assert SQLitePartyRepository(repo.path).get_player_preferences(1, 7) == saved
    assert SQLitePartyRepository(repo.path).get_player_preferences(2, 7) == ()


def test_legacy_preference_keys_are_classified_during_migration(tmp_path):
    repo = SQLitePartyRepository(tmp_path / "party.sqlite3")
    with repo._connect() as conn:
        conn.execute(
            """INSERT INTO party_player_preferences
               (guild_id,user_id,preferences_json,updated_at)
               VALUES (1,7,'["captain","support","fill","substitute"]',
                       '2026-07-20T00:00:00+00:00')"""
        )

    profile = repo.get_player_preferences(1, 7)

    assert profile == PlayerPreferences("support", None, True, True)


def test_remove_participant_is_idempotent_and_audited(tmp_path):
    repo = SQLitePartyRepository(tmp_path / "party.sqlite3")
    repo.create(
        guild_id=1, organizer_id=2, capacity=5, lobby_id="leave",
        operation_id="create",
    )
    joined = repo.save_participant(
        1, "leave", Participant(3), operation_id="join",
    )
    removed = repo.remove_participant(
        1, "leave", 3, operation_id="leave-1", actor_id=3,
    )
    retried = repo.remove_participant(
        1, "leave", 3, operation_id="leave-1", actor_id=3,
    )
    noop = repo.remove_participant(
        1, "leave", 3, operation_id="leave-2", actor_id=3,
    )

    assert removed.participant(3) is None
    assert removed.version == joined.version + 1
    assert retried.version == removed.version
    assert noop.version == removed.version
    events = repo.audit_events(1, "leave")
    assert [event.event_type for event in events[-2:]] == [
        "participant_removed",
        "participant_remove_noop",
    ]
    assert events[-1].metadata == {"user_id": 3}


def test_existing_database_is_migrated_additively(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE party_lobbies (
              lobby_id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,
              organizer_id INTEGER NOT NULL, capacity INTEGER NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              expires_at TEXT, version INTEGER NOT NULL,
              delivery_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE party_participants (
              lobby_id TEXT NOT NULL, user_id INTEGER NOT NULL,
              preferences_json TEXT NOT NULL DEFAULT '[]',
              ready INTEGER NOT NULL DEFAULT 0, joined_at TEXT NOT NULL,
              PRIMARY KEY(lobby_id,user_id)
            );
            CREATE TABLE party_audit (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, lobby_id TEXT NOT NULL,
              guild_id INTEGER NOT NULL, operation_id TEXT NOT NULL UNIQUE,
              command_fingerprint TEXT NOT NULL, event_type TEXT NOT NULL,
              from_state TEXT, to_state TEXT NOT NULL, actor_id INTEGER,
              occurred_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE party_player_preferences (
              guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
              preferences_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
              PRIMARY KEY(guild_id,user_id)
            );
            """
        )

    SQLitePartyRepository(path)

    with sqlite3.connect(path) as conn:
        lobby_columns = {row[1] for row in conn.execute("PRAGMA table_info(party_lobbies)")}
        preference_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(party_player_preferences)")
        }
    assert {"mode", "region", "format", "voice_required", "skill_band", "notes"} <= lobby_columns
    assert {"primary_role", "secondary_role", "fill", "captain"} <= preference_columns
