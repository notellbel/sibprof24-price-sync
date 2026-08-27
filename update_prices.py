"""
Подготовка файла с ценами для sibprof24.ru (гибридная схема).

ПОЧЕМУ ТАК:
Прямые запросы от GitHub Actions к WooCommerce API сайта стали
блокироваться (похоже на защиту хостинга от чужих ботов). Поэтому
вместо того чтобы GitHub стучался к сайту, этот скрипт готовит
маленький файл с результатом и публикует его в самом репозитории.
Дальше сайт сам, по расписанию, скачивает этот файл через
WP All Import Pro (уже установлен на сайте) и обновляет цены —
это исходящий запрос с сервера сайта, а не входящий, блокировку
не затрагивает.

ЧТО ДЕЛАЕТ:
1. Скачивает свежий YML поставщика.
2. Берёт таблицу соответствия site_price_map.csv (113 проверенных
   товаров).
3. Считает новую цену: (цена поставщика / 1.55) * 1.3
4. Записывает результат в price_feed.csv — два столбца: sku;price
   Этот файл коммитится в репозиторий самим workflow (см.
   update-prices.yml) и становится доступен по прямой ссылке
   через raw.githubusercontent.com — именно её вы укажете в
   WP All Import.
"""

import csv
import xml.etree.ElementTree as ET

YML_URL = "https://yml.grmeh.ru/export/feed_yandex_yml23_.xml"

SUPPLIER_MARKUP = 1.55   # наценка поставщика уже внутри их цены (+55%)
OUR_MARKUP = 1.30        # наша наценка сверх себестоимости (+30%)

OUTPUT_FILE = "price_feed.csv"


def load_price_map(path="site_price_map.csv"):
    mapping = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            mapping.append({
                "sku": row["site_sku"].strip(),
                "vendor_code": row["yml_vendorCode"].strip(),
                "name": row["site_name"].strip(),
            })
    return mapping


def fetch_supplier_prices():
    import requests
    resp = requests.get(YML_URL, timeout=60)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    prices = {}
    for offer in root.findall(".//offer"):
        vc = offer.find("vendorCode")
        price = offer.find("price")
        if vc is not None and price is not None and vc.text:
            try:
                prices[vc.text.strip()] = float(price.text)
            except (TypeError, ValueError):
                continue
    return prices


def calc_new_price(supplier_price):
    cost = supplier_price / SUPPLIER_MARKUP
    return round(cost * OUR_MARKUP, 2)


def main():
    mapping = load_price_map()
    print(f"Товаров в таблице соответствия: {len(mapping)}")

    supplier_prices = fetch_supplier_prices()
    print(f"Цен получено от поставщика: {len(supplier_prices)}")

    rows = []
    missing = 0
    for item in mapping:
        sp = supplier_prices.get(item["vendor_code"])
        if sp is None:
            missing += 1
            continue
        rows.append((item["sku"], calc_new_price(sp)))

    print(f"Товаров с ценой в итоговом файле: {len(rows)}")
    print(f"Не найдено в текущей выгрузке поставщика (пропущено): {missing}")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sku", "price"])
        for sku, price in rows:
            w.writerow([sku, price])

    print(f"Готово: {OUTPUT_FILE} записан, {len(rows)} строк.")


if __name__ == "__main__":
    main()
