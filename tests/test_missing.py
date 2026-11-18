

def test_feet_found_by_name_are_not_listed_as_missing_contacts_and_meshes_still_are(tmp_path, monkeypatch):
    """ANYmal: the standing check ran from its named feet, so foot contacts are not a missing
    input; a mesh line in 0.5.1's stored text still lists mesh files as missing."""
    import shutil
    from pathlib import Path

    from typer.testing import CliRunner

    from lockwire.cli import app

    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.chdir(tmp_path)
    shutil.copy(fixtures / "anymal_d.urdf", "a.urdf")
    out = CliRunner().invoke(app, ["check", "a.urdf", "--all"]).output
    assert "model/standing/" in out and "foot contacts (the feet)" not in out
    shutil.copytree(fixtures / "so101", "so101")
    assert "mesh files (" in CliRunner().invoke(app, ["check", "so101/so101.urdf"]).output
