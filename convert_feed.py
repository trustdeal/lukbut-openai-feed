from __future__ import annotations

import csv
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "products.csv"

FIELDS = [
    "item_id",
    "title",
    "description",
    "url",
    "brand",
    "seller_name",
    "image_url",
    "availability",
    "price",
    "is_ads_eligible",
    "group_id",
    "listing_has_variations",
    "variant_dict",
    "sale_price",
    "gtin",
    "condition",
    "product_category",
    "material",
    "color",
    "size",
    "gender",
]

REQUIRED_FIELDS = [
    "item_id",
    "title",
    "description",
    "url",
    "brand",
    "seller_name",
    "image_url",
    "availability",
    "price",
]

ALLOWED_AVAILABILITY = {
    "in_stock",
    "out_of_stock",
    "pre_order",
    "backorder",
    "unknown",
}

PRICE_RE = re.compile(r"^(\d+(?:\.\d{2})?) ([A-Z]{3})$")


def get_text(item: ET.Element, field: str) -> str:
    node = item.find(field)

    if node is None or node.text is None:
        return ""

    return node.text.strip()


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def download_feed(url: str) -> bytes:
    print("Downloading sanitized IdoSell XML feed...")

    request = Request(
        url,
        headers={
            "User-Agent": "LUKBUT-OpenAI-Feed/1.0",
            "Accept": "application/xml,text/xml,*/*",
        },
    )

    with urlopen(request, timeout=180) as response:
        data = response.read()

    if not data:
        fail("Downloaded XML feed is empty.")

    print(f"Downloaded {len(data):,} bytes.")
    return data


def parse_variant_dict(item: ET.Element) -> str:
    node = item.find("variant_dict")

    if node is None:
        return ""

    result = {}

    for child in list(node):
        value = (child.text or "").strip()

        if value:
            result[child.tag] = value

    if not result:
        return ""

    return json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def validate_price(value: str) -> tuple[Decimal, str]:
    match = PRICE_RE.match(value)

    if not match:
        raise ValueError(
            f"Invalid price format: {value!r}. "
            "Expected format such as '399.99 PLN'."
        )

    amount = Decimal(match.group(1))
    currency = match.group(2)

    return amount, currency


def convert(xml_data: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        fail(f"XML parsing failed: {exc}")

    if root.tag != "openai_feed":
        fail(
            f"Unexpected XML root: {root.tag!r}. "
            "Expected 'openai_feed'."
        )

    rows = []
    seen_item_ids = set()
    seen_variants = set()

    for number, item in enumerate(root.findall("item"), start=1):

        row = {
            field: get_text(item, field)
            for field in FIELDS
            if field != "variant_dict"
        }

        row["variant_dict"] = parse_variant_dict(item)

        # Required fields
        for field in REQUIRED_FIELDS:
            if not row[field]:
                fail(
                    f"Row {number}: missing required field "
                    f"{field!r}."
                )

        # Unique item_id
        item_id = row["item_id"]

        if item_id in seen_item_ids:
            fail(
                f"Row {number}: duplicate item_id {item_id!r}."
            )

        seen_item_ids.add(item_id)

        # Availability
        if row["availability"] not in ALLOWED_AVAILABILITY:
            fail(
                f"Row {number}: invalid availability "
                f"{row['availability']!r}."
            )

        # URLs
        if not row["url"].startswith("https://"):
            fail(
                f"Row {number}: product URL must use HTTPS."
            )

        if not row["image_url"].startswith("https://"):
            fail(
                f"Row {number}: image URL must use HTTPS."
            )

        # Text lengths
        if len(row["title"]) > 150:
            fail(
                f"Row {number}: title exceeds 150 characters."
            )

        if len(row["description"]) > 5000:
            fail(
                f"Row {number}: description exceeds "
                "5000 characters."
            )

        # Boolean-like fields
        if row["listing_has_variations"] not in {
            "true",
            "false",
        }:
            fail(
                f"Row {number}: invalid "
                "listing_has_variations value."
            )

        if row["is_ads_eligible"] not in {
            "true",
            "false",
        }:
            fail(
                f"Row {number}: invalid "
                "is_ads_eligible value."
            )

        # Price
        try:
            price_amount, price_currency = validate_price(
                row["price"]
            )
        except ValueError as exc:
            fail(f"Row {number}: {exc}")

        # Sale price
        if row["sale_price"]:
            try:
                sale_amount, sale_currency = validate_price(
                    row["sale_price"]
                )
            except ValueError as exc:
                fail(f"Row {number}: {exc}")

            if sale_currency != price_currency:
                fail(
                    f"Row {number}: price and sale_price "
                    "currencies differ."
                )

            if sale_amount >= price_amount:
                fail(
                    f"Row {number}: sale_price must be "
                    "lower than price."
                )

        # Variant grouping
        if (
            row["group_id"]
            and row["group_id"] == row["item_id"]
        ):
            fail(
                f"Row {number}: group_id cannot equal item_id."
            )

        if (
            row["listing_has_variations"] == "true"
            and row["variant_dict"]
        ):
            variant_key = (
                row["group_id"],
                row["variant_dict"],
            )

            if variant_key in seen_variants:
                fail(
                    f"Row {number}: duplicate variant "
                    f"{row['variant_dict']} in group "
                    f"{row['group_id']}."
                )

            seen_variants.add(variant_key)

        rows.append(row)

    if len(rows) < 100:
        fail(
            f"Feed contains only {len(rows)} items. "
            "Publishing aborted as a safety check."
        )

    return rows


def write_csv(rows: list[dict[str, str]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )

        writer.writeheader()
        writer.writerows(rows)

    print(
        f"Created {OUTPUT_FILE} "
        f"with {len(rows):,} product variants."
    )


def main() -> None:
    feed_url = os.environ.get("IDOSELL_FEED_URL", "").strip()

    if not feed_url:
        fail(
            "Missing IDOSELL_FEED_URL environment variable."
        )

    xml_data = download_feed(feed_url)
    rows = convert(xml_data)
    write_csv(rows)

    in_stock = sum(
        row["availability"] == "in_stock"
        for row in rows
    )

    out_of_stock = sum(
        row["availability"] == "out_of_stock"
        for row in rows
    )

    print("")
    print("Feed validation completed successfully.")
    print(f"Total variants: {len(rows):,}")
    print(f"In stock:       {in_stock:,}")
    print(f"Out of stock:   {out_of_stock:,}")
    print(f"Output:         {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
