"""Find Canadian sectional and modular couch deals and email the results."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from notify import send_email

DISCOUNT_THRESHOLD = Decimal("50")
REQUEST_TIMEOUT_SECONDS = 30
SHOPIFY_PAGE_SIZE = 250
MAX_SHOPIFY_PAGES = 20

SHOPIFY_SOURCES = {
    "The Brick": (
        "https://www.thebrick.com",
        (
            "/collections/furniture-living-room-sofas/products.json",
            "/collections/furniture-living-room-sectionals/products.json",
        ),
    ),
    "Leon's": (
        "https://www.leons.ca",
        (
            "/collections/furniture-living-room-sofas/products.json",
            "/collections/furniture-living-room-sectionals/products.json",
        ),
    ),
}
IKEA_BASE_URL = "https://www.ikea.com/ca/en/"
IKEA_SOURCES = (
    (urljoin(IKEA_BASE_URL, "cat/sofas-fu003/"), ""),
    (urljoin(IKEA_BASE_URL, "cat/sectional-sofas-31786/"), "sectional sofa"),
)
SECTIONAL_PATTERN = re.compile(
    r"\b(?:sectionals?|sectionnels?|sectionnelles?)\b", re.IGNORECASE
)
MODULAR_PATTERN = re.compile(r"\b(?:modular|modulaire|modulaires)\b", re.IGNORECASE)
SOFA_OR_COUCH_PATTERN = re.compile(
    r"\b(?:sofas?|couch(?:es)?|canap[eé]s?)\b", re.IGNORECASE
)
PRICE_PATTERN = re.compile(r"(\d[\d,]*)(?:\s*\.\s*(\d{2}))?")
MEUBLES_RD_BASE_URL = "https://www.meublesrd.com"
MEUBLES_RD_PAGE_URL = (
    f"{MEUBLES_RD_BASE_URL}/fr/mobilier-salon/sofas-fauteuils/sectionnels"
)
MEUBLES_RD_CATEGORY = "Salon /// Sofas & fauteuils"
MEUBLES_RD_PAGE_SIZE = 100
MAX_MEUBLES_RD_PAGES = 20


@dataclass(frozen=True)
class Deal:
    retailer: str
    title: str
    url: str
    sale_price: Decimal
    regular_price: Decimal
    discount_percent: Decimal
    image_url: str | None = None


def _is_target_product(text: str) -> bool:
    return bool(
        SECTIONAL_PATTERN.search(text)
        or (
            MODULAR_PATTERN.search(text)
            and SOFA_OR_COUCH_PATTERN.search(text)
        )
    )


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
    image_url: str | None = None,
    category_hint: str = "",
) -> Deal | None:
    if (
        not title
        or not _is_target_product(f"{title} {category_hint}")
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
    return Deal(retailer, title, url, sale_price, regular_price, discount, image_url)


def _shopify_deals(
    products: list[dict[str, Any]], retailer: str, base_url: str
) -> list[Deal]:
    deals: dict[str, Deal] = {}
    for product in products:
        title = str(product.get("title") or "").strip()
        product_type = str(product.get("product_type") or "")
        raw_tags = product.get("tags") or ""
        tags = " ".join(raw_tags) if isinstance(raw_tags, list) else str(raw_tags)
        if not _is_target_product(f"{title} {product_type} {tags}"):
            continue

        handle = str(product.get("handle") or "")
        if not handle:
            continue
        product_url = urljoin(base_url, f"/products/{handle}")
        images = product.get("images") or []
        first_image = images[0] if images and isinstance(images[0], dict) else {}
        image_data = product.get("image")
        if not first_image and isinstance(image_data, dict):
            first_image = image_data
        product_image = first_image.get("src") if isinstance(first_image, dict) else None
        if not product_image and isinstance(image_data, str):
            product_image = image_data
        product_image_url = (
            urljoin(base_url, str(product_image)) if product_image else None
        )

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
            variant_image = variant.get("featured_image")
            if isinstance(variant_image, dict):
                variant_image = variant_image.get("src")
            image_url = (
                urljoin(base_url, str(variant_image))
                if variant_image
                else product_image_url
            )

            deal = _make_deal(
                retailer,
                display_title,
                product_url,
                sale_price,
                regular_price,
                image_url,
                f"{product_type} {tags}",
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


def _ikea_card_deal(card: Tag, category_hint: str = "") -> Deal | None:
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
    product_url = urljoin(IKEA_BASE_URL, str(product_link.get("href") or ""))
    image_node = card.select_one("img.plp-image")
    image_url = None
    if image_node is not None:
        image_source = image_node.get("src") or image_node.get("data-src")
        if not image_source and image_node.get("srcset"):
            image_source = str(image_node["srcset"]).split(",")[-1].strip().split()[0]
        if image_source:
            image_url = urljoin(IKEA_BASE_URL, str(image_source))
            parsed_image_url = urlsplit(image_url)
            image_query = re.sub(
                r"(^|&)f=[^&]*", r"\1f=xxl", parsed_image_url.query
            )
            image_url = urlunsplit(
                (
                    parsed_image_url.scheme,
                    parsed_image_url.netloc,
                    parsed_image_url.path,
                    image_query or parsed_image_url.query,
                    parsed_image_url.fragment,
                )
            )

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
    return _make_deal(
        "IKEA Canada",
        title,
        product_url,
        sale_price,
        regular_price,
        image_url,
        category_hint,
    )


def _ikea_source_deals(
    session: requests.Session, url: str, category_hint: str = ""
) -> list[Deal]:
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return [
        deal
        for card in soup.select(".plp-mastercard")
        if (deal := _ikea_card_deal(card, category_hint)) is not None
    ]


def _meubles_rd_deals(hits: list[dict[str, Any]]) -> list[Deal]:
    deals: dict[str, Deal] = {}
    for hit in hits:
        if str(hit.get("can_add_to_cart") or "").lower() != "yes":
            continue
        price = ((hit.get("price") or {}).get("CAD") or {})
        sale_price = _decimal_price(price.get("default"))
        discount_amount = _decimal_price(price.get("discount"))
        if sale_price is None or discount_amount is None:
            continue
        sale_price = sale_price.quantize(Decimal("0.01"))
        regular_price = (sale_price + discount_amount).quantize(Decimal("0.01"))
        category_hint = " ".join(
            str(name)
            for level in (hit.get("categories") or {}).values()
            for name in level
        )
        url = str(hit.get("url") or "")
        deal = _make_deal(
            "Meubles RD",
            str(hit.get("name") or "").strip(),
            url,
            sale_price,
            regular_price,
            hit.get("image_url") or hit.get("thumbnail_url"),
            category_hint,
        )
        if deal is None:
            continue
        current = deals.get(url)
        if current is None or deal.discount_percent > current.discount_percent:
            deals[url] = deal
    return list(deals.values())


def _meubles_rd_source_deals(session: requests.Session) -> list[Deal]:
    # The category page renders products client-side from Algolia, using a
    # short-lived search key embedded in the page.
    response = session.get(MEUBLES_RD_PAGE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    def config_value(name: str) -> str:
        match = re.search(
            rf"\\u0022{name}\\u0022\\u003A\\u0022(.*?)\\u0022", response.text
        )
        if match is None:
            raise ValueError(f"Meubles RD search {name} not found")
        return re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            match.group(1),
        )

    app_id = config_value("applicationId")
    api_key = config_value("apiKey")
    index_name = f"{config_value('baseIndexName')}_products"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Referer": f"{MEUBLES_RD_BASE_URL}/",
    }
    facet_filters = json.dumps([[f"categories.level1:{MEUBLES_RD_CATEGORY}"]])

    hits: list[dict[str, Any]] = []
    for page in range(MAX_MEUBLES_RD_PAGES):
        search = session.post(
            f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries",
            headers=headers,
            json={
                "requests": [
                    {
                        "indexName": index_name,
                        "params": urlencode(
                            {
                                "query": "",
                                "hitsPerPage": MEUBLES_RD_PAGE_SIZE,
                                "page": page,
                                "facetFilters": facet_filters,
                            }
                        ),
                    }
                ]
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        search.raise_for_status()
        results = search.json().get("results")
        if not isinstance(results, list) or not results:
            raise ValueError("Meubles RD returned an unexpected response format")
        page_hits = results[0].get("hits")
        if not isinstance(page_hits, list):
            raise ValueError("Meubles RD returned an unexpected product-list format")
        hits.extend(item for item in page_hits if isinstance(item, dict))
        if page + 1 >= int(results[0].get("nbPages") or 0):
            break
    else:
        raise ValueError(f"Meubles RD exceeded the {MAX_MEUBLES_RD_PAGES}-page limit")

    return _meubles_rd_deals(hits)


def scrape_deals() -> tuple[list[Deal], list[str], list[str]]:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "SofaMonitor/1.0 (+https://github.com/sidtechno/sofa-monitor)"}
    )
    retry_policy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry_policy))
    deals: list[Deal] = []
    checked: list[str] = []
    errors: list[str] = []

    for retailer, (base, endpoints) in SHOPIFY_SOURCES.items():
        retailer_deals: dict[str, Deal] = {}
        successful_collections = 0
        for endpoint in endpoints:
            try:
                collection_deals = _shopify_source_deals(
                    session, retailer, base, endpoint
                )
                successful_collections += 1
                for deal in collection_deals:
                    current = retailer_deals.get(deal.url)
                    if current is None or deal.discount_percent > current.discount_percent:
                        retailer_deals[deal.url] = deal
            except (requests.RequestException, ValueError) as error:
                errors.append(f"{retailer} ({endpoint}): {error}")
                print(
                    f"Scraping failed for {retailer} ({endpoint}): {error}",
                    file=sys.stderr,
                )
        deals.extend(retailer_deals.values())
        if successful_collections:
            checked.append(retailer)

    successful_ikea_sources = 0
    for url, category_hint in IKEA_SOURCES:
        try:
            deals.extend(_ikea_source_deals(session, url, category_hint))
            successful_ikea_sources += 1
        except (requests.RequestException, ValueError) as error:
            errors.append(f"IKEA Canada ({url}): {error}")
            print(f"Scraping failed for IKEA Canada ({url}): {error}", file=sys.stderr)
    if successful_ikea_sources:
        checked.append("IKEA Canada")

    try:
        deals.extend(_meubles_rd_source_deals(session))
        checked.append("Meubles RD")
    except (requests.RequestException, ValueError) as error:
        errors.append(f"Meubles RD: {error}")
        print(f"Scraping failed for Meubles RD: {error}", file=sys.stderr)

    unique_deals: dict[tuple[str, str], Deal] = {}
    for deal in deals:
        key = (deal.retailer, deal.url)
        current = unique_deals.get(key)
        if current is None or deal.discount_percent > current.discount_percent:
            unique_deals[key] = deal

    deals = list(unique_deals.values())
    deals.sort(key=lambda deal: (-deal.discount_percent, deal.sale_price))
    return deals, checked, errors


def _format_email(
    deals: list[Deal], checked: list[str], errors: list[str]
) -> tuple[str, str, str]:
    if deals:
        subject = (
            f"Sofa Monitor: {len(deals)} sectional/modular deal(s) at 60%+ off"
        )
        sections = [
            f"{deal.title}\n"
            f"Retailer: {deal.retailer}\n"
            f"Sale price: ${deal.sale_price:,.2f} CAD "
            f"(regularly ${deal.regular_price:,.2f} CAD)\n"
            f"Discount: {deal.discount_percent:.1f}%\n"
            f"Product: {deal.url}"
            + (f"\nPhoto: {deal.image_url}" if deal.image_url else "")
            for deal in deals
        ]
        body = "\n\n".join(sections)
    else:
        subject = "Sofa Monitor: no sectional/modular deals at 60%+ off"
        body = (
            "No sectional sofa or modular couch listings with a verified "
            "discount of 60% or more were found."
        )

    body += "\n\nWebsites checked: " + (", ".join(checked) or "none")
    body += (
        "\n\nThese are Canadian online listings. Confirm delivery and stock "
        "availability for your Quebec postal code with the retailer."
    )
    if errors:
        body += "\n\nWebsite errors:\n" + "\n".join(f"- {error}" for error in errors)

    cards = []
    for deal in deals:
        image = ""
        if deal.image_url:
            image = (
                f'<a href="{escape(deal.url, quote=True)}">'
                f'<img src="{escape(deal.image_url, quote=True)}" '
                f'alt="{escape(deal.title, quote=True)}" '
                'style="display:block;max-width:100%;width:360px;height:auto;'
                'border:0;margin-bottom:12px"></a>'
            )
        cards.append(
            '<div style="border:1px solid #ddd;border-radius:8px;'
            'padding:16px;margin:0 0 16px">'
            f"{image}"
            f'<h2 style="font-size:18px;margin:0 0 8px">'
            f'<a href="{escape(deal.url, quote=True)}">'
            f"{escape(deal.title)}</a></h2>"
            f"<p><strong>{escape(deal.retailer)}</strong><br>"
            f"Sale price: ${deal.sale_price:,.2f} CAD "
            f"(regularly ${deal.regular_price:,.2f} CAD)<br>"
            f"<strong>{deal.discount_percent:.1f}% off</strong></p></div>"
        )

    html_body = (
        "<html><body style=\"font-family:Arial,sans-serif;color:#222\">"
        "<h1>Sectional sofa and modular couch deals</h1>"
        f'{"".join(cards) if cards else "<p>No qualifying deals found.</p>"}'
        f"<p>Websites checked: {escape(', '.join(checked) or 'none')}</p>"
        "<p>These are Canadian online listings. Confirm delivery and stock "
        "availability for your Quebec postal code with the retailer.</p>"
        + (
            "<p><strong>Website errors:</strong><br>"
            + "<br>".join(escape(error) for error in errors)
            + "</p>"
            if errors
            else ""
        )
        + "</body></html>"
    )
    return subject, body, html_body


def main() -> None:
    deals, checked, errors = scrape_deals()
    subject, body, html_body = _format_email(deals, checked, errors)
    send_email(subject, body, html_body)

    if not checked:
        raise RuntimeError("All retailer scrapes failed; see the email for details.")


if __name__ == "__main__":
    main()
