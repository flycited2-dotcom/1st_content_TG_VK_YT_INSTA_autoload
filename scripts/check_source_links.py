"""Проверяет, что все источники редакционной базы ещё открываются.

В тесты это выносить нельзя — сеть сделала бы прогон нестабильным.
Запускать вручную перед крупной партией постов:

    python scripts/check_source_links.py
"""
import pathlib
import sys

import httpx
import yaml

CONFIG = pathlib.Path(__file__).parents[1] / "config" / "vk-editorial-sources.yaml"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def main() -> int:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    used = {}
    for topic in raw["topics"]:
        for fact in topic["facts"]:
            used.setdefault(fact["source"], []).append(topic["id"])

    broken = []
    with httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS) as client:
        for key, item in raw["sources"].items():
            url = item["url"]
            try:
                code = client.head(url).status_code
                # Часть сайтов не отвечает на HEAD — уточняем полным запросом.
                if code in (403, 404, 405):
                    code = client.get(url).status_code
            except httpx.HTTPError as error:
                code = type(error).__name__
            ok = code == 200
            print(f"{code}  {key}")
            if not ok:
                broken.append((key, code, sorted(set(used.get(key, [])))))

    if broken:
        print("\nНедоступны:")
        for key, code, topics in broken:
            print(f"  {key} ({code}) — держит темы: {', '.join(topics) or 'нет'}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
