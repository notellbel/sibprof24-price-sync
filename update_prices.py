"""
Автообновление цен на sibprof24.ru по выгрузке поставщика (grmeh.ru).

ЧТО ДЕЛАЕТ:
1. Скачивает свежий YML-файл поставщика (обновляется у них раз в 2 часа).
2. Берёт готовую таблицу соответствия "наш артикул -> код товара поставщика"
   (site_price_map.csv, уже проверена вручную на 113 товарах).
3. Для каждого товара считает новую цену: (цена поставщика / 1.55) * 1.3
   (1.55 = убираем их наценку 55%, 1.3 = добавляем нашу наценку 30%).
4. Обновляет цену в WooCommerce по артикулу (SKU) через REST API.

ПЕРЕД ПЕРВЫМ ЗАПУСКОМ:
1. pip install requests
2. Заполните SITE_URL, CONSUMER_KEY, CONSUMER_SECRET ниже
   (те же ключи, что использовали для очистки дублей, права должны быть
   "Чтение/Запись")
3. Файл site_price_map.csv должен лежать рядом с этим скриптом.
4. Сначала запустите с DRY_RUN = True — покажет, что изменится, ничего
   не тронет. Проверьте цифры, потом поставьте DRY_RUN = False.

КАК ЗАПУСКАТЬ РЕГУЛЯРНО (раз в 2-4 часа):
Через Планировщик заданий Windows:
  - Открыть "Планировщик заданий" -> "Создать простую задачу"
  - Триггер: Ежедневно, повторять задачу каждые 2 часа в течение дня
  - Действие: Запуск программы -> program: python.exe,
    аргументы: полный путь до этого файла (update_prices.py),
    рабочая папка: папка, где лежат оба файла
"""

import csv
import time
import requests
import xml.etree.ElementTree as ET

# ========== НАСТРОЙКИ — ЗАПОЛНИТЕ ПЕРЕД ЗАПУСКОМ ==========
SITE_URL = "https://sibprof24.ru"
CONSUMER_KEY = "ck_ВСТАВЬТЕ_СЮДА"
CONSUMER_SECRET = "cs_ВСТАВЬТЕ_СЮДА"

YML_URL = "https://yml.grmeh.ru/export/feed_yandex_yml23_.xml"

SUPPLIER_MARKUP = 1.55   # наценка поставщика уже внутри их цены (+55%)
OUR_MARKUP = 1.30        # наша наценка сверх себестоимости (+30%)

DRY_RUN = True
BATCH_SIZE = 20
DELAY_BETWEEN_BATCHES = 1.0
LOG_FILE = "price_update_log.csv"
# ============================================================


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
    """Скачивает YML поставщика и возвращает {vendorCode: price}."""
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


MAX_RETRIES = 3
RETRY_DELAY = 5  # секунд между повторными попытками при сетевом сбое


def _request_with_retry(method, url, **kwargs):
    """Делает запрос с несколькими попытками — на случай разового сетевого
    сбоя (например, GitHub Actions на секунду теряет связь с сайтом)."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                print(f"    Сетевой сбой (попытка {attempt}/{MAX_RETRIES}), повтор через {RETRY_DELAY} сек: {e}")
                time.sleep(RETRY_DELAY)
    # все попытки исчерпаны — пробрасываем последнюю ошибку выше,
    # вызывающий код сам решит, пропустить товар или нет
    raise last_error


def get_product_id_by_sku(sku):
    """Находит ID товара в WooCommerce по артикулу. Возвращает None,
    если товар не найден ИЛИ если после нескольких попыток так и не
    удалось связаться с сайтом (сетевой сбой) — в обоих случаях
    вызывающий код просто пропустит этот товар и пойдёт дальше."""
    url = f"{SITE_URL}/wp-json/wc/v3/products"
    try:
        resp = _request_with_retry(
            "GET", url, params={"sku": sku},
            auth=(CONSUMER_KEY, CONSUMER_SECRET)
        )
    except requests.exceptions.RequestException as e:
        print(f"    Не удалось получить ID для SKU {sku} после {MAX_RETRIES} попыток: {e}")
        return None
    data = resp.json()
    if data:
        return data[0]["id"]
    return None


def update_prices_batch(updates):
    """updates: список {'id': ..., 'regular_price': '...'}"""
    url = f"{SITE_URL}/wp-json/wc/v3/products/batch"
    resp = _request_with_retry(
        "POST", url, json={"update": updates},
        auth=(CONSUMER_KEY, CONSUMER_SECRET), timeout=60
    )
    return resp.json()


def main():
    mapping = load_price_map()
    print(f"Товаров в таблице соответствия: {len(mapping)}")

    supplier_prices = fetch_supplier_prices()
    print(f"Цен получено от поставщика: {len(supplier_prices)}")

    to_update = []
    log_rows = []
    missing_in_feed = 0

    for item in mapping:
        sp = supplier_prices.get(item["vendor_code"])
        if sp is None:
            missing_in_feed += 1
            continue
        new_price = calc_new_price(sp)
        to_update.append({"sku": item["sku"], "new_price": new_price,
                           "name": item["name"], "supplier_price": sp})

    print(f"Будет обновлено цен: {len(to_update)}")
    print(f"Не найдено в текущей выгрузке поставщика (пропущено): {missing_in_feed}")

    if DRY_RUN:
        print("\n=== СУХОЙ ПРОГОН (DRY_RUN=True), ничего не изменено ===")
        for item in to_update[:10]:
            print(f"  {item['sku']:20s} | {item['name'][:45]:45s} | "
                  f"поставщик {item['supplier_price']:>10.2f} -> новая цена {item['new_price']:>10.2f}")
        if len(to_update) > 10:
            print(f"  ... и ещё {len(to_update) - 10} товаров")
        print("\nЧтобы применить реально, поставьте DRY_RUN = False.")
        return

    # ---- реальное обновление ----
    updated, errors = 0, 0
    for i in range(0, len(to_update), BATCH_SIZE):
        chunk = to_update[i:i + BATCH_SIZE]
        batch_payload = []
        for item in chunk:
            pid = get_product_id_by_sku(item["sku"])
            if pid is None:
                errors += 1
                log_rows.append([item["sku"], item["name"], "", item["new_price"], "SKU не найден на сайте"])
                continue
            batch_payload.append({"id": pid, "regular_price": str(item["new_price"])})
            log_rows.append([item["sku"], item["name"], pid, item["new_price"], "ok"])

        if batch_payload:
            try:
                update_prices_batch(batch_payload)
                updated += len(batch_payload)
                print(f"Обновлена пачка {i // BATCH_SIZE + 1}: {len(batch_payload)} товаров")
            except requests.exceptions.RequestException as e:
                errors += len(batch_payload)
                print(f"ОШИБКА в пачке {i // BATCH_SIZE + 1}: {e}")

        time.sleep(DELAY_BETWEEN_BATCHES)

    with open(LOG_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sku", "name", "product_id", "new_price", "status"])
        w.writerows(log_rows)

    print("\n=== ИТОГ ===")
    print(f"Обновлено успешно: {updated}")
    print(f"Ошибок / не найдено на сайте: {errors}")
    print(f"Подробный лог: {LOG_FILE}")


if __name__ == "__main__":
    main()
