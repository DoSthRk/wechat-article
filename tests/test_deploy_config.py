from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_lightweight_server_deploy_config_is_tracked():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "gm-blog.service").read_text(encoding="utf-8")

    assert "gunicorn" in requirements.lower()
    assert "WorkingDirectory=/opt/gm-blog/current" in service
    assert "EnvironmentFile=-/opt/gm-blog/shared/.env" in service
    assert "0.0.0.0:8001" in service
    assert "app:create_app()" in service
