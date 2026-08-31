"""Read only the public SplitHome catalogue through the existing Django app."""
import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "splithome.settings")
import django
django.setup()
from django.db import connection, transaction
from apps.catalog.models import Product

with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
    products = Product.objects.filter(
        is_active=True, category__sync_enabled=True, kind=Product.KIND_SPLIT_SYSTEM
    ).select_related("brand", "category", "stock").prefetch_related("images", "tech_values__spec").order_by("id")
    rows = []
    for p in products.iterator(chunk_size=100):
        stock = getattr(p, "stock", None)
        rows.append({
            "id": p.id, "offer_id": f"{p.source}:{p.nc_code}",
            "brand": p.brand.title if p.brand else "", "articul": p.articul,
            "title": p.title, "slug": p.slug, "description": p.description,
            "category_id": p.category_id, "category": p.category.title,
            "price": str(p.ric) if p.ric is not None else None,
            "currency": p.ric_currency,
            "quantity": str(stock.quantity) if stock else "0",
            "warehouse": stock.warehouse if stock else "",
            "images": [i.url for i in p.images.all()],
            "specs": [{"name": v.spec.title, "value": v.value, "unit": v.spec.unit}
                      for v in p.tech_values.all()],
        })
    print(json.dumps(rows, ensure_ascii=True))
