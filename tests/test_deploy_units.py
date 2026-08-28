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


def test_shortlink_config_covers_every_generated_prefix():
    """Каждый префикс короткой ссылки должен разворачиваться на splithome.

    Расхождение уже случалось: конфиг умел только устаревший `/go/vkp_<id>`,
    а издатель к тому времени генерировал `/go/svc<id>` и каталожные префиксы.
    Такая ссылка в живом посте ведёт в 404, поэтому список проверяется тестом.
    """
    from content_factory.analytics.vk import campaign_short_url

    config = (ROOT / "deploy" / "nginx" / "splithome-vk-shortlinks.conf").read_text(
        encoding="utf-8")
    service_link = campaign_short_url(1, base_url="https://splithome.ru", intent="service")
    prefix = service_link.rsplit("/go/", 1)[1].rstrip("0123456789")

    assert f"/go/{prefix}" in config, (
        f"конфиг не разворачивает префикс {prefix!r} из campaign_short_url"
    )
    assert "utm_content=vkp_" in config


def test_catalog_shortlink_destinations_are_documented():
    """Каталожные редиректы живут на climat-simf и не описаны ни в одном юните.

    Они отдают 307 средствами приложения, а не nginx, поэтому единственное место,
    где их можно зафиксировать, — этот же конфиг в виде комментария-карты.
    """
    from content_factory.analytics.vk import EDITORIAL_CATALOG_DESTINATIONS, campaign_short_url

    config = (ROOT / "deploy" / "nginx" / "splithome-vk-shortlinks.conf").read_text(
        encoding="utf-8")
    for category in EDITORIAL_CATALOG_DESTINATIONS:
        link = campaign_short_url(1, base_url="https://climat-simf.ru", intent=category)
        prefix = link.rsplit("/go/", 1)[1].rstrip("0123456789")
        assert f"/go/{prefix}" in config, (
            f"каталожный префикс {prefix!r} ({category}) нигде не зафиксирован"
        )
