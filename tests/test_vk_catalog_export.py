from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest

from content_factory.storefront.vk_catalog_export import (
    clean_text, model_identity, partition_feeds, render_offer, select_products, export_category,
)


def product(key='breeze:1', model='AS-09', category=2):
    return dict(offer_id=key, brand='Brand', articul=model, title='Brand '+model,
                slug='brand-'+model, category_id=category, category='Кондиционеры',
                price='19990.50', currency='RUB', quantity='3', warehouse='Симферополь',
                images=['https://example.com/photo.jpg'], description='<p>Описание</p>', specs=[])


def test_clean_text_removes_markup_scripts_and_xml_controls():
    assert clean_text('<p>Текст &amp; ещё</p><script>secret()</script>\x01') == 'Текст & ещё'


def test_double_escaped_source_html_is_also_removed():
    assert clean_text('&amp;lt;p&amp;gt;Текст&amp;lt;/p&amp;gt;') == 'Текст'


def test_previous_category_ids_are_not_reused_for_air_conditioners():
    assert export_category(product())[0] == 1002
    assert export_category(product(category=30))[0] == 2
    assert export_category(product(category=22))[0] == 1


def test_towel_holder_does_not_get_into_air_conditioners():
    row = product()
    row['title'] = 'Полотенцедержатель Royal Thermo на 6 секц.'
    assert export_category(row) == (1901, 'Комплектующие и расходные материалы', '12-other')


def test_radiator_in_wrong_source_category_moves_to_heating():
    row = product()
    row['title'] = 'Радиатор алюминиевый Royal Thermo Revolution 350'
    assert export_category(row) == (1118, 'Радиаторы отопления', '09-heating')


def test_identity_keeps_capacity_and_model_separators():
    assert model_identity(product(model='AS-09')) != model_identity(product(model='AS-12'))
    assert model_identity(product(model='AB-C1')) != model_identity(product(model='A-BC1'))


def test_known_ids_also_exclude_same_model_at_other_supplier():
    rows=[product('breeze:1'),product('other:2'),product('breeze:3','AS-12')]
    selected,rejected=select_products(rows,{'breeze:1'})
    assert [p['offer_id'] for p in selected]==['breeze:3']
    assert len(rejected)==2


def test_duplicate_model_prefers_stock_and_retail_price():
    a,b=product('breeze:1'),product('other:2')
    a['quantity']='0'
    selected,_=select_products([a,b],set())
    assert selected[0]['offer_id']=='other:2'


def test_reject_missing_price_photo_and_unexpected_currency():
    rows=[product(str(i),str(i)) for i in range(3)]
    rows[0]['price']=None
    rows[1]['images']=[]
    rows[2]['currency']='USD'
    selected,rejected=select_products(rows,set())
    assert not selected and len(rejected)==3


def test_offer_uses_retail_price_product_url_and_no_html_or_params():
    node=render_offer(product(),'https://example.com/valid.jpg')
    assert node.get('id')=='breeze:1'
    assert Decimal(node.findtext('price'))==Decimal('19991')
    assert node.findtext('url').startswith('https://splithome.ru/product/')
    assert '<p>' not in node.findtext('description')
    assert not node.findall('param')
    assert 'достав' not in node.findtext('description').lower()


def test_parts_strictly_under_byte_limit_and_no_duplicates():
    nodes=[render_offer(product(str(i),f'AS-{i}'),'https://example.com/photo.jpg') for i in range(20)]
    parts=partition_feeds(nodes,max_bytes=4000)
    assert len(parts)>1 and all(len(p)<4000 for p in parts)
    ids=[n.get('id') for p in parts for n in ET.fromstring(p).findall('./shop/offers/offer')]
    assert len(ids)==20 and len(set(ids))==20


def test_one_oversized_offer_raises():
    with pytest.raises(ValueError):
        partition_feeds([render_offer(product(),'https://example.com/photo.jpg')],max_bytes=100)


def test_out_of_stock_product_never_reaches_the_export():
    """Товара без остатка в витрине быть не должно.

    Пометки available="false" недостаточно: VK такие позиции не скрывает, а
    показывает с плашкой «Недоступно». В выгрузке каталога 31.08.2026 из 13 618
    товаров в наличии было лишь 2 698 — витрина на 80% состояла из пустых
    карточек, а в четырёх файлах не было ни одного товара в наличии.
    """
    in_stock = product(key='breeze:1', model='AS-09')
    sold_out = product(key='breeze:2', model='AS-12')
    sold_out['quantity'] = '0'

    selected, rejected = select_products([in_stock, sold_out], set())

    assert [row['offer_id'] for row in selected] == ['breeze:1']
    assert any(r['id'] == 'breeze:2' and r['reason'] == 'not_in_stock' for r in rejected)


def test_offer_id_is_ascii_so_vk_does_not_mangle_the_article():
    """Кириллица в id выводится в карточке VK как «rusklimat:РќРЎ-1598403»."""
    row = product(key='rusklimat:НС-1598403')
    node = render_offer(row, 'https://example.com/photo.jpg')
    offer_id = node.get('id')

    assert offer_id.isascii(), offer_id
    assert 'NS-1598403' in offer_id
    # Идентификатор обязан быть стабильным: повторный импорт обновляет товар.
    assert render_offer(product(key='rusklimat:НС-1598403'), 'x').get('id') == offer_id
