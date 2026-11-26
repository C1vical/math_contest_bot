"""Command-line parsing and error reporting (no database, no network)."""

from __future__ import annotations

import pytest

from mathbot.cli import build_parser, common


def test_year_and_number_ranges_are_parsed_and_merged():
    args = build_parser().parse_args(["ingest", "PCF", "--year", "2019", "2021-2023", "--number", "1,3,5-6"])
    assert common.int_set(args.year) == {2019, 2021, 2022, 2023}
    assert common.int_set(args.number) == {1, 3, 5, 6}
    assert common.int_set(build_parser().parse_args(["ingest", "PCF"]).year) is None


@pytest.mark.parametrize("bad", ["2019-", "abc", "2023-2019", ","])
def test_malformed_ranges_are_usage_errors_not_tracebacks(bad, capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["ingest", "PCF", "--year", bad])
    assert exit_info.value.code == 2
    assert "--year" in capsys.readouterr().err


def test_a_database_from_newer_code_is_reported_cleanly(tmp_path, monkeypatch, capsys):
    from mathbot.cli import main
    from mathbot.db import connect, migrate

    database = tmp_path / "p.db"
    conn = connect(database)
    migrate(conn)
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (999, 'x')")
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["stats"]) == 2
    assert "newer than this code understands" in capsys.readouterr().err


def test_render_force_selects_problems_that_already_have_an_image(tmp_path, monkeypatch, capsys):
    from mathbot.cli import main
    from mathbot.contests import ContestRegistry
    from mathbot.db import Repository, open_database
    from tests.conftest import add_problem

    database = tmp_path / "p.db"
    conn = open_database(database)
    repo = Repository(conn)
    repo.sync_registry(ContestRegistry())
    problem = add_problem(repo, 1, key="CSMC", year=2024)
    from mathbot.config import load_settings
    from mathbot.render.template import renderer_version

    version = renderer_version(load_settings({"DATA_DIR": str(tmp_path)}).render)
    repo.record_render(problem_id=problem.id, content_hash=problem.content_hash, renderer_version=version, status="ok")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    renders = tmp_path / "renders"
    monkeypatch.setenv("RENDERS_DIR", str(renders))
    from mathbot.render.pipeline import render_key

    (renders / render_key(problem, version)).parent.mkdir(parents=True)
    (renders / render_key(problem, version)).write_bytes(b"png")

    assert main(["render", "CSMC", "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("0 problem(s) need rendering")
    assert main(["render", "CSMC", "--dry-run", "--force"]) == 0
    assert capsys.readouterr().out.startswith("1 problem(s) need rendering")
