import xml.etree.ElementTree as ET

import pytest

from content_factory.storefront.vk_import_retry import failed_offer_ids, build_retry_feed


FEED = '''<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2026-08-31 14:48"><shop><name>Магазин</name>
<currencies><currency id="RUR" rate="1"/></currencies>
<categories><category id="2">Бойлеры</category></categories><offers>
<offer id="ok"><name>Другой товар</name></offer>
<offer id="rusklimat:НС-1588136" available="true"><price>8505</price>
<currencyId>RUR</currencyId><categoryId>2</categoryId>
<picture>https://example.test/original.png</picture>
<name>Royal Thermo Trend, 80 л</name><description>Описание\nБез изменений</description>
</offer></offers></shop></yml_catalog>'''.encode('utf-8')
KEY = 'rusklimat:НС-1588136'
URL = 'https://example.test/vk-import/royal-thermo-80-v1.png'


def test_log_reads_utf8_bom_csv_and_excel_apostrophe():
    log = '\ufeffАртикул,Название,Ошибка\r\n\'rusklimat:НС-1588136,"Trend, 80 л",Ошибка\r\n'
    assert failed_offer_ids(log) == [KEY]


def test_retry_keeps_only_failed_offer_and_original_identity_and_fields():
    result = build_retry_feed(FEED, [KEY], {KEY: URL})
    root = ET.fromstring(result)
    offers = root.findall('./shop/offers/offer')
    assert len(offers) == 1
    assert offers[0].get('id') == KEY
    assert offers[0].get('available') == 'true'
    assert offers[0].findtext('price') == '8505'
    assert offers[0].findtext('description') == 'Описание\nБез изменений'
    assert offers[0].findtext('picture') == URL
    assert [el.tag for el in offers[0]] == [
        'price', 'currencyId', 'categoryId', 'picture', 'name', 'description',
    ]
    assert root.findtext('./shop/categories/category') == 'Бойлеры'
    assert b'encoding=\'utf-8\'' in result


@pytest.mark.parametrize('ids,replacements', [
    ([], {}), (['missing'], {}), ([KEY], {'missing': URL}),
    ([KEY], {KEY: 'file:///private/image.png'}),
    ([KEY], {KEY: 'https://user:secret@example.test/image.png'}),
])
def test_invalid_inputs_fail_closed(ids, replacements):
    with pytest.raises(ValueError):
        build_retry_feed(FEED, ids, replacements)


def test_duplicate_source_ids_are_rejected():
    duplicate = FEED.replace(b'<offer id="ok">', '<offer id="rusklimat:НС-1588136">'.encode())
    with pytest.raises(ValueError, match='Duplicate'):
        build_retry_feed(duplicate, [KEY], {KEY: URL})


def test_required_fields_cannot_be_missing():
    with pytest.raises(ValueError, match='description'):
        build_retry_feed(FEED.replace('Описание\nБез изменений'.encode(), b''), [KEY], {})


def test_empty_log_is_rejected_instead_of_exporting_all():
    with pytest.raises(ValueError):
        failed_offer_ids('Артикул,Название,Ошибка\n')
