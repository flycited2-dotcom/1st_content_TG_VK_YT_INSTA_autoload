from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_linux_deploy_files_are_declared_with_lf_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert "*.sh text eol=lf" in attributes
    assert "*.service text eol=lf" in attributes
    assert "*.timer text eol=lf" in attributes


def test_db_host_helper_is_invoked_via_shell():
    for name in ("cf-cards.service", "cf-channel-sync.service", "cf-scheduler.service"):
        unit = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "ExecStartPre=/bin/sh /opt/content-factory/deploy/update_db_host.sh" in unit
