import unittest
from decimal import Decimal

from bs4 import BeautifulSoup

from main import (
    _format_email,
    _ikea_card_deal,
    _make_deal,
    _shopify_deals,
)


class DealTests(unittest.TestCase):
    def test_accepts_exactly_sixty_percent_off(self):
        deal = _make_deal(
            "Retailer",
            "Example Sofa",
            "https://example.com/sofa",
            Decimal("400"),
            Decimal("1000"),
        )

        self.assertIsNotNone(deal)
        assert deal is not None
        self.assertEqual(deal.discount_percent, Decimal("60"))

    def test_rejects_discount_below_sixty_percent(self):
        deal = _make_deal(
            "Retailer",
            "Example Sofa",
            "https://example.com/sofa",
            Decimal("401"),
            Decimal("1000"),
        )

        self.assertIsNone(deal)

    def test_rejects_non_sofa_products(self):
        deal = _make_deal(
            "Retailer",
            "Example armchair",
            "https://example.com/chair",
            Decimal("100"),
            Decimal("500"),
        )

        self.assertIsNone(deal)

    def test_shopify_compare_at_price_produces_deal(self):
        products = [
            {
                "title": "Example Sofa",
                "handle": "example-sofa",
                "tags": "living room, sofa",
                "variants": [
                    {
                        "available": True,
                        "price": "400.00",
                        "compare_at_price": "1000.00",
                        "title": "Blue",
                    }
                ],
            }
        ]

        deals = _shopify_deals(products, "Retailer", "https://example.com")

        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].sale_price, Decimal("400.00"))
        self.assertIn("(Blue)", deals[0].title)

    def test_ikea_struck_price_produces_deal(self):
        card = BeautifulSoup(
            """
            <div class="plp-mastercard">
              <a class="plp-price-module__product-link"
                 aria-label="Example Sofa">Example Sofa</a>
              <span class="plp-price-module__description">Three-seat sofa</span>
              <a href="/ca/en/p/example-sofa-12345678/"></a>
              <span class="plp-price-module__current-price">$ 399 . 00</span>
              <del>$ 997 . 50</del>
            </div>
            """,
            "html.parser",
        ).select_one(".plp-mastercard")

        assert card is not None
        deal = _ikea_card_deal(card)

        self.assertIsNotNone(deal)
        assert deal is not None
        self.assertEqual(deal.discount_percent, Decimal("60"))
        self.assertEqual(deal.sale_price, Decimal("399.00"))

    def test_email_reports_no_deals_and_scrape_errors(self):
        subject, body = _format_email([], ["IKEA Canada"], ["The Brick: timeout"])

        self.assertIn("no sofa deals", subject.lower())
        self.assertIn("IKEA Canada", body)
        self.assertIn("The Brick: timeout", body)


if __name__ == "__main__":
    unittest.main()
