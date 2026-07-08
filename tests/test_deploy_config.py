from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_lightweight_server_deploy_config_is_tracked():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "gm-blog.service").read_text(encoding="utf-8")

    assert "openai==" in requirements.lower()
    assert "pdfplumber==" in requirements.lower()
    assert "gunicorn==" in requirements.lower()
    assert "WorkingDirectory=/opt/gm-blog/current" in service
    assert "EnvironmentFile=-/opt/gm-blog/shared/.env" in service
    assert "0.0.0.0:5000" in service
    assert "app:create_app()" in service


def test_line_runtime_configs_are_deployable():
    required = [
        "inputs/products/purprox_aaveasy_spin_columns.yaml",
        "inputs/products/solidex_pan_t_cell_iso_kit.yaml",
        "inputs/style_templates/aav_academic_soft_intro.yaml",
        "inputs/style_templates/oncology_wechat_pop_sci.yaml",
    ]
    for rel in required:
        assert (ROOT / rel).exists(), rel
        assert rel in _git_tracked_files(), rel


def _git_tracked_files():
    import subprocess

    return set(
        subprocess.check_output(
            ["git", "ls-files"], cwd=ROOT, text=True, encoding="utf-8",
        ).splitlines()
    )
