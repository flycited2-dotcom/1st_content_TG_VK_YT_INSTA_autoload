"""Publish only validated additive YML artifacts; no VK calls or app changes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import re
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--directory', required=True, type=Path)
    cli.add_argument('--key', required=True)
    cli.add_argument('--folder', default='vk-catalog-20260831')
    cli.add_argument('--verify-from-server', action='store_true',
                     help='Full public HTTPS GET from VPS; record verification vantage explicitly')
    args = cli.parse_args()
    if not args.folder.replace('-', '').isalnum():
        raise ValueError('Invalid static folder')
    report = json.loads((args.directory / 'report.json').read_text(encoding='utf-8'))
    base_url = f'https://splithome.ru/static/{args.folder}/'
    payloads, ids = {}, []
    for entry in report['files']:
        name = entry['file']
        if Path(name).name != name or not name.endswith('.yml'):
            raise ValueError('Invalid feed filename')
        payload = (args.directory / name).read_bytes()
        root = ET.fromstring(payload)
        offers = root.findall('./shop/offers/offer')
        assert 0 < len(payload) < 7_800_000
        assert len(payload) == entry['bytes'] and len(offers) == entry['count']
        categories = {n.get('id') for n in root.findall('./shop/categories/category')}
        for offer in offers:
            assert offer.get('id') and offer.get('id') not in report['excluded_ids']
            ids.append(offer.get('id'))
            assert offer.findtext('categoryId') in categories
            for tag in ('name', 'price', 'picture', 'description', 'url'):
                assert (offer.findtext(tag) or '').strip()
            assert len(offer.findtext('name')) <= 100
            assert len(offer.findtext('description')) <= 4000
            assert not re.search(r'</?[A-Za-z][^>]*>', offer.findtext('description'))
            assert len(offer.findall('param')) <= 2
        payloads[name] = payload
        entry['url'] = base_url + name
        entry['sha256'] = hashlib.sha256(payload).hexdigest()
    assert len(ids) == len(set(ids)) == report['exported_count']
    assert len(ids) + sum(report['reasons'].values()) == report['catalog_count']
    assert len(ids) + len(report['excluded_ids']) <= 15000
    public = {k: report[k] for k in ('catalog_count', 'exported_count', 'files', 'reasons', 'image_validation')}
    public['generated_date'] = '2026-08-31'
    public['vk_import_executed'] = False
    public['note'] = 'Разовая добавочная выгрузка. Загружать части по очереди, без замены каталога. Цены/наличие на дату сборки.'
    payloads['manifest.json'] = json.dumps(public, ensure_ascii=False, indent=2).encode('utf-8')
    lines = [
        'Split Home — оставшийся каталог для VK, 31.08.2026',
        f'Готово к импорту: {len(ids)} товаров, файлов: {len(report["files"])}.',
        'Все файлы меньше 8 МБ. Загружать по одному, дожидаясь окончания импорта.',
        'VK → Добавить товар → Из файла → ссылка. Не выбирать замену всего каталога.',
        'Для подборок можно включить группировку по категориям.',
        'Из выгрузки исключены прежние 44 товара и известная XIGMA SKY21.',
        'Без цены или подходящей фотографии товары не включены. Полный учёт — manifest.json.',
        'Это снимок цен/наличия на 31.08.2026, не автоматическая синхронизация.', '',
    ]
    for n, entry in enumerate(report['files'], 1):
        lines.extend([f'{n}. {entry["group"]} — {entry["count"]} товаров, {entry["bytes"]/1_000_000:.2f} МБ',
                      entry['url'], ''])
    payloads['import-links.txt'] = '\n'.join(lines).encode('utf-8')
    (args.directory / 'import-links.txt').write_bytes(payloads['import-links.txt'])
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(payload), 0o644
            tar.addfile(info, io.BytesIO(payload))
    # Do not replace existing artifacts. Atomic directory rename publishes the batch.
    remote = '''import hashlib,io,json,pathlib,sys,tarfile,tempfile
base=pathlib.Path('/opt/oasis/staticfiles').resolve(strict=True)
target=base/FOLDER
assert target.resolve().parent==base
payload=sys.stdin.buffer.read()
with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as tar:
    members=tar.getmembers()
    assert members and all(m.isfile() and pathlib.PurePosixPath(m.name).name==m.name for m in members)
    content={m.name:tar.extractfile(m).read() for m in members}
if target.exists():
    assert all((target/n).read_bytes()==v for n,v in content.items()), 'Existing publication differs; refusing overwrite'
else:
    staging=pathlib.Path(tempfile.mkdtemp(prefix='.'+FOLDER+'-',dir=base))
    staging.chmod(0o755)
    for name,data in content.items():
        p=staging/name
        p.write_bytes(data)
        p.chmod(0o644)
    staging.rename(target)
print(json.dumps({'path':str(target),'files':len(content),'bytes':sum(map(len,content.values()))}))
'''.replace('FOLDER', repr(args.folder))
    import base64
    command = 'python3 -c "import base64;exec(base64.b64decode(\'' + base64.b64encode(remote.encode()).decode() + '\'))"'
    result = subprocess.run(['ssh', '-i', args.key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                             'root@212.116.115.150', command], input=archive.getvalue(), capture_output=True, check=True)
    print(result.stdout.decode(), flush=True)
    if args.verify_from_server:
        expected = {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                    for name, data in payloads.items()}
        verify_script = '''import json,urllib.request,hashlib,concurrent.futures
base=BASE
expected=EXPECTED
def check(item):
    name,meta=item
    with urllib.request.urlopen(base+name,timeout=25) as response:
        data=response.read()
        digest=hashlib.sha256(data).hexdigest()
        assert response.status==200 and len(data)==meta['bytes'] and digest==meta['sha256']
    return {'file':name,'status':200,'bytes':len(data),'sha256':digest,'vantage':'VPS public HTTPS URL'}
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    print(json.dumps(list(pool.map(check,expected.items()))))
'''.replace('BASE', repr(base_url)).replace('EXPECTED', repr(expected))
        response = subprocess.run(['ssh', '-i', args.key, '-o', 'BatchMode=yes',
                                   'root@212.116.115.150', 'python3 -'],
                                  input=verify_script.encode(), capture_output=True, check=True)
        checks = json.loads(response.stdout)
        (args.directory / 'publication-verification.json').write_text(
            json.dumps(checks, indent=2), encoding='utf-8')
        print(json.dumps({'verified': len(checks), 'vantage': 'VPS public HTTPS URL',
                          'url': base_url + 'import-links.txt', 'exported': len(ids)}), flush=True)
        return
    def verify(item):
        name, expected = item
        for attempt in range(3):
            try:
                with urllib.request.urlopen(base_url + name, timeout=20) as response:
                    chunks = []
                    while chunk := response.read(65536): chunks.append(chunk)
                    actual = b''.join(chunks)
                    assert response.status == 200 and actual == expected, name
                print(f'GET verified {name}: {len(actual)} bytes', flush=True)
                return {'file': name, 'status': 200, 'bytes': len(actual), 'sha256': hashlib.sha256(actual).hexdigest()}
            except (TimeoutError, OSError):
                if attempt == 2: raise
                time.sleep(1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(verify, payloads.items()))
    (args.directory / 'publication-verification.json').write_text(
        json.dumps(checks, indent=2), encoding='utf-8')
    print(json.dumps({'verified': len(checks), 'url': base_url + 'import-links.txt', 'exported': len(ids)}), flush=True)


if __name__ == '__main__':
    main()
