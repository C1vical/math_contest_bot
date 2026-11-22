from pathlib import Path

import pytest

from mathbot.config import PROJECT_ROOT, ConfigError, load_settings, require_bot_token


def test_defaults_need_no_environment():
    settings = load_settings({})
    assert settings.database_path == PROJECT_ROOT / "data" / "problems.db"
    assert settings.storage.backend == "local" and settings.storage.public_base_url is None
    assert settings.bot.token is None and settings.bot.allowed_channel_ids == frozenset()
    assert settings.http.respect_robots is True
    assert "Mozilla" not in settings.http.user_agent  # honest UA, no browser impersonation


def test_values_are_parsed(tmp_path):
    settings = load_settings(
        {
            "DISCORD_TOKEN": " abc ",
            "ALLOWED_CHANNEL_IDS": "222, 333",
            "ADMIN_ROLE_IDS": "9",
            "DATA_DIR": str(tmp_path),
            "PORT": "9000",
            "ENABLE_CONTESTS": "cjmo, gauss7",
            "IMAGE_BASE_URL": "https://img.example.org/",
            "SYNC_COMMANDS": "false",
        }
    )
    assert settings.bot.token == "abc"
    assert settings.bot.allowed_channel_ids == {222, 333}
    assert settings.bot.admin_role_ids == {9}
    assert settings.database_path == Path(tmp_path) / "problems.db"
    assert settings.bot.health_port == 9000 and settings.bot.sync_commands is False
    assert settings.enable_contests == {"CJMO", "GAUSS7"}
    assert settings.storage.public_base_url == "https://img.example.org"


@pytest.mark.parametrize(
    "env, fragment",
    [
        ({"ALLOWED_CHANNEL_IDS": "general"}, "ALLOWED_CHANNEL_IDS"),
        ({"PORT": "eighty"}, "PORT"),
        ({"PORT": "0"}, "PORT"),
        ({"SYNC_COMMANDS": "maybe"}, "SYNC_COMMANDS"),
        ({"STORAGE_BACKEND": "ftp"}, "STORAGE_BACKEND"),
        ({"STORAGE_BACKEND": "s3", "S3_BUCKET": "b"}, "S3_ACCESS_KEY_ID"),
        ({"HTTP_TIMEOUT": "-1"}, "HTTP_TIMEOUT"),
    ],
)
def test_bad_values_name_the_offending_variable(env, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_settings(env)


def test_render_assets_and_snapshot_settings(tmp_path):
    settings = load_settings(
        {"DATA_DIR": str(tmp_path), "DATABASE_SNAPSHOT_URL": "https://img.example.org/db/latest.json"}
    )
    assert settings.render_assets_dir == tmp_path / "render_assets"
    assert settings.database_snapshot_url == "https://img.example.org/db/latest.json"
    assert load_settings({}).database_snapshot_url is None


def test_missing_token_is_a_clear_error_only_when_the_bot_starts():
    settings = load_settings({})
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        require_bot_token(settings)
    assert require_bot_token(load_settings({"DISCORD_TOKEN": "t"})) == "t"


def test_no_secrets_or_bucket_urls_are_hard_coded():
    offenders = []
    for path in (PROJECT_ROOT / "mathbot").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "r2.dev" in text or "aops_session_id" in text:
            offenders.append(path.name)
    assert offenders == []


def test_optional_ids_and_object_keys():
    settings = load_settings({"GUILD_ID": "123", "S3_KEY_PREFIX": "/renders/"})
    assert settings.bot.guild_id == 123 and settings.bot.announce_channel_id is None
    assert settings.storage.object_key("imo/a.png") == "renders/imo/a.png"
    assert load_settings({}).storage.object_key("imo/a.png") == "imo/a.png"
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_settings({"GUILD_ID": "my server"})
