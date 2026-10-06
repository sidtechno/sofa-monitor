"""Find Canadian sofa listings discounted by at least 60% and email the results."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from notify import send_email

DISCOUNT_THRESHOLD = Decimal("60")
REQUEST_TIMEOUT_SECONDS = 30
SHOPIFY_PAGE_SIZE = 250
MAX_SHOPIFY_PAGES = 20

SHOPIFY_SOURCES = {
    "The Brick": (
        "https://www.thebrick.com",
        "/collections/furniture-living-room-sofas/products.json",
    ),
    "Leon's": (
        "https://www.leons.ca",
        "/collections/furniture-living-room-sofas/products.json",
    ),
}
IKEA_URL = "https://www.ikea.com/ca/en/cat/sofas-fu003/"
SOFA_PATTERN = re.compile(
    r"\b(?:sofas?|couches?|sectionals?|loveseats?|divans?|canap[eé]s?)\b",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(r"(\d[\d,]*)(?:\s*\.\s*(\d{2}))?")


@dataclass(frozen=True)
class Deal:
    retailer: str
    title: str
    url: str
    sale_price: Decimal
    regular_price: Decimal
    discount_percent: Decimal


def _decimal_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None


def _price_from_text(text: str) -> Decimal | None:
    match = PRICE_PATTERN.search(text.replace("\xa0", " "))
    if not match:
        return None
    dollars = match.group(1).replace(",", "")
    cents = match.group(2) or "00"
    return Decimal(f"{dollars}.{cents}")


def _make_deal(
    retailer: str,
    title: str,
    url: str,
    sale_price: Decimal | None,
    regular_price: Decimal | None,
) -> Deal | None:
    if (
        not title
        or not SOFA_PATTERN.search(title)
        or not url
        or sale_price is None
        or regular_price is None
        or regular_price <= 0
        or sale_price <= 0
        or sale_price >= regular_price
    ):
        return None

    discount_amount = regular_price - sale_price
    if discount_amount * 100 < regular_price * DISCOUNT_THRESHOLD:
        return None

    discount = discount_amount * 100 / regular_price
    return Deal(retailer, title, url, sale_price, regular_price, discount)


def _shopify_deals(
    products: list[dict[str, Any]], retailer: str, base_url: str
) -> list[Deal]:
    deals: dict[str, Deal] = {}
    for product in products:
        title = str(product.get("title") or "").strip()
        product_type = str(product.get("product_type") or "")
        raw_tags = product.get("tags") or ""
        tags = " ".join(raw_tags) if isinstance(raw_tags, list) else str(raw_tags)
        if not SOFA_PATTERN.search(f"{title} {product_type} {tags}"):
            continue

        handle = str(product.get("handle") or "")
        if not handle:
            continue
        product_url = urljoin(base_url, f"/products/{handle}")

        for variant in product.get("variants", []):
            if not variant.get("available", True):
                continue
            sale_price = _decimal_price(variant.get("price"))
            regular_price = _decimal_price(variant.get("compare_at_price"))
            variant_title = str(variant.get("title") or "")
            display_title = title
            if (
                variant_title
                and variant_title.lower() != "default title"
                and not variant_title.isdigit()
            ):
                display_title = f"{title} ({variant_title})"

            deal = _make_deal(
                retailer, display_title, product_url, sale_price, regular_price
            )
            if deal is None:
                continue

            current = deals.get(product_url)
            if current is None or deal.discount_percent > current.discount_percent:
                deals[product_url] = deal
    return list(deals.values())


def _shopify_source_deals(
    session: requests.Session, retailer: str, base_url: str, endpoint: str
) -> list[Deal]:
    products: list[dict[str, Any]] = []
    for page in range(1, MAX_SHOPIFY_PAGES + 1):
        response = session.get(
            urljoin(base_url, endpoint),
            params={"limit": SHOPIFY_PAGE_SIZE, "page": page},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{retailer} returned an unexpected response format")
        page_products = payload.get("products")
        if not isinstance(page_products, list):
            raise ValueError(f"{retailer} returned an unexpected product-list format")
        products.extend(item for item in page_products if isinstance(item, dict))
        if len(page_products) < SHOPIFY_PAGE_SIZE:
            break
    else:
        raise ValueError(f"{retailer} exceeded the {MAX_SHOPIFY_PAGES}-page limit")

    return _shopify_deals(products, retailer, base_url)


def _ikea_card_deal(card: Tag) -> Deal | None:
    title_node = card.select_one(".plp-price-module__product-link")
    if title_node is None:
        return None
    title = str(title_node.get("aria-label") or title_node.get_text(" ", strip=True))

    description_node = card.select_one(".plp-price-module__description")
    if description_node is not None:
        title = f"{title} {description_node.get_text(' ', strip=True)}".strip()

    product_link = card.select_one('a[href*="/p/"]')
    if product_link is None:
        return None
    product_url = urljoin(IKEA_URL, str(product_link.get("href") or ""))

    current_node = card.select_one(".plp-price-module__current-price")
    if current_node is None:
        return None
    sale_price = _price_from_text(current_node.get_text(" ", strip=True))
    if sale_price is None:
        return None

    old_price_nodes = card.select(
        "del, s, [class*='previous-price'], [class*='regular-price'], "
        "[class*='original-price'], [class*='was-price'], "
        "[class*='strikethrough']"
    )
    old_prices = [
        price
        for node in old_price_nodes
        if (price := _price_from_text(node.get_text(" ", strip=True))) is not None
        and price > sale_price
    ]
    regular_price = max(old_prices) if old_prices else None
    return _make_deal("IKEA Canada", title, product_url, sale_price, regular_price)


def _ikea_source_deals(session: requests.Session) -> list[Deal]:
    response = session.get(IKEA_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return [
        deal
        for card in soup.select(".plp-mastercard")
        if (deal := _ikea_card_deal(card)) is not None
    ]


def scrape_deals() -> tuple[list[Deal], list[str], list[str]]:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "SofaMonitor/1.0 (+https://github.com/sidtechno/sofa-monitor)"}
    )
    deals: list[Deal] = []
    checked: list[str] = []
    errors: list[str] = []

    for retailer, (base, endpoint) in SHOPIFY_SOURCES.items():
        try:
            deals.extend(
                _shopify_source_deals(session, retailer, base, endpoint)
            )
            checked.append(retailer)
        except (requests.RequestException, ValueError) as error:
            errors.append(f"{retailer}: {error}")
            print(f"Scraping failed for {retailer}: {error}", file=sys.stderr)

    try:
        deals.extend(_ikea_source_deals(session))
        checked.append("IKEA Canada")
    except (requests.RequestException, ValueError) as error:
        errors.append(f"IKEA Canada: {error}")
        print(f"Scraping failed for IKEA Canada: {error}", file=sys.stderr)

    deals.sort(key=lambda deal: (-deal.discount_percent, deal.sale_price))
    return deals, checked, errors


def _format_email(
    deals: list[Deal], checked: list[str], errors: list[str]
) -> tuple[str, str]:
    if deals:
        subject = f"Sofa Monitor: {len(deals)} sofa deal(s) at 60%+ off"
        sections = [
            f"{deal.title}\n"
            f"Retailer: {deal.retailer}\n"
            f"Sale price: ${deal.sale_price:,.2f} CAD "
            f"(regularly ${deal.regular_price:,.2f} CAD)\n"
            f"Discount: {deal.discount_percent:.1f}%\n"
            f"Product: {deal.url}"
            for deal in deals
        ]
        body = "\n\n".join(sections)
    else:
        subject = "Sofa Monitor: no sofa deals at 60%+ off"
        body = "No sofa listings with a verified discount of 60% or more were found."

    body += "\n\nWebsites checked: " + (", ".join(checked) or "none")
    body += (
        "\n\nThese are Canadian online listings. Confirm delivery and stock "
        "availability for your Quebec postal code with the retailer."
    )
    if errors:
        body += "\n\nWebsite errors:\n" + "\n".join(f"- {error}" for error in errors)
    return subject, body


def main() -> None:
    deals, checked, errors = scrape_deals()
    subject, body = _format_email(deals, checked, errors)
    send_email(subject, body)

    if not checked:
        raise RuntimeError("All retailer scrapes failed; see the email for details.")


if __name__ == "__main__":
    main()
