from decimal import Decimal
from types import SimpleNamespace

import httpx

from content_factory.ingest.storefront_api import collect_storefront_offers, parse_product_jsonld


def _cfg(**kw):
    base = dict(api_url="https://shop.test/api/internal/tender-products",
                token_env="TOKEN", queries=["стабилизатор", "Ресанта"], limit_per_query=20,
                category_id=10598, available_only=True, enrich_product_pages=True,
                timeout_seconds=5)
    base.update(kw)
    return SimpleNamespace(**base)


def test_parse_product_jsonld_extracts_images_and_retail_price():
    html = '''<script type="application/ld+json">{"@type":"Product","image":["https://x/1.jpg"],
              "offers":{"@type":"Offer","price":"12990"}}</script>'''
    images, price = parse_product_jsonld(html)
    assert images == ["https://x/1.jpg"]
    assert price == Decimal("12990")


def test_collect_deduplicates_filters_and_enriches():
    product = {"sku": "A-1", "name": "Стабилизатор 10 кВА", "vendor": "Ресанта",
               "purchasePriceGross": 10000, "isAvailable": True, "stockStatus": "low",
               "productUrl": "https://shop.test/product/a-1",
               "attributes": [{"label": "Мощность", "value": "10", "unit": "кВА"}]}

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "total": 1, "products": [product]})
        return httpx.Response(200, text='''<script type="application/ld+json">
          {"@type":"Product","image":"https://shop.test/img/a.jpg","offers":{"price":15990}}
          </script>''')

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        offers = collect_storefront_offers(_cfg(), "x" * 32, client=client)
    assert len(offers) == 1
    offer = offers[0]
    assert offer.source == "storefront" and offer.category_id == 10598
    assert offer.cost == Decimal("10000") and offer.retail_ref == Decimal("15990")
    assert offer.photos == ["https://shop.test/img/a.jpg"]
    assert offer.attrs["Мощность"] == "10 кВА"


def test_collect_rejects_missing_secret():
    try:
        collect_storefront_offers(_cfg(), "short")
    except ValueError as exc:
        assert "TOKEN" in str(exc)
    else:
        raise AssertionError("expected ValueError")
