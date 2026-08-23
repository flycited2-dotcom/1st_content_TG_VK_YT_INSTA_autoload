import json

from content_factory.ingest.product_references import (
    ProductReferenceInbox, ReferenceCandidate, candidate_is_verified,
    exact_model_for_offer,
)
from content_factory.models import Offer


def _offer(photos=None):
    return Offer(
        supplier_sku="storefront:9758739", source="storefront", brand="RUCELF",
        model="Стабилизатор напряжения RUCELF SRW-12000-D 12000 ВА",
        attrs={}, stock=1, photos=photos or [],
    )


def test_catalog_photo_has_priority_and_creates_no_web_request(tmp_path):
    inbox = ProductReferenceInbox(tmp_path)
    assert inbox.resolve(_offer(["https://catalog.test/original.jpg"])) == \
        "https://catalog.test/original.jpg"
    assert not inbox.requests.exists()


def test_missing_photo_queues_exact_model_request(tmp_path):
    offer = _offer()
    inbox = ProductReferenceInbox(tmp_path)
    assert inbox.resolve(offer) is None
    request = json.loads(inbox.queue(offer).read_text(encoding="utf-8"))
    assert request["model"] == "SRW-12000-D"
    assert request["requirements"]["exact_model_only"] is True
    assert request["requirements"]["forbid_similar_models"] is True


def test_only_exact_authorized_candidate_is_accepted(tmp_path):
    offer = _offer()
    inbox = ProductReferenceInbox(tmp_path, trusted_domains=["manufacturer.test"])
    inbox.results.mkdir(parents=True)
    result = inbox.results / f"{inbox.key_for(offer)}.json"
    result.write_text(json.dumps({"candidates": [{
        "image_url": "https://cdn.manufacturer.test/srw.jpg",
        "page_url": "https://manufacturer.test/products/srw-12000-d",
        "page_title": "RUCELF SRW-12000-D",
        "source_type": "manufacturer",
        "usage_allowed": True,
        "corroborating_sources": 2,
    }]}), encoding="utf-8")
    assert inbox.resolve(offer) == "https://cdn.manufacturer.test/srw.jpg"

    similar = ReferenceCandidate(
        "https://manufacturer.test/srw.jpg", "https://manufacturer.test/p",
        "RUCELF SRW-10000-D", "manufacturer", True, 2,
    )
    assert candidate_is_verified(exact_model_for_offer(offer), similar,
                                 ["manufacturer.test"]) is False

