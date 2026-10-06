import unittest
from unittest.mock import patch
from decimal import Decimal

from bs4 import BeautifulSoup

from notify import send_email
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
            "Example Sectional Sofa",
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
            "Example Sectional Sofa",
            "https://example.com/sofa",
            Decimal("401"),
            Decimal("1000"),
        )

        self.assertIsNone(deal)

    def test_rejects_regular_sofas(self):
        deal = _make_deal(
            "Retailer",
            "Example Sofa",
            "https://example.com/sofa",
            Decimal("100"),
            Decimal("500"),
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
                "handle": "example-modular-couch",
                "tags": "living room, modular couch",
                "images": [{"src": "//cdn.example.com/modular-couch.jpg"}],
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
        self.assertEqual(deals[0].title, "Example Sofa (Blue)")
        self.assertEqual(
            deals[0].image_url, "https://cdn.example.com/modular-couch.jpg"
        )

    def test_ikea_struck_price_produces_deal(self):
        card = BeautifulSoup(
            """
            <div class="plp-mastercard">
              <a class="plp-price-module__product-link"
                 aria-label="Example Sectional">Example Sectional</a>
              <span class="plp-price-module__description">Sectional Sofa</span>
              <a href="/ca/en/p/example-sectional-sofa-12345678/"></a>
              <img class="plp-image"
                   src="https://www.ikea.com/sofa.jpg?f=xxs">
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
        self.assertIn("f=xxl", deal.image_url or "")

    def test_email_reports_no_deals_and_scrape_errors(self):
        subject, body, html_body = _format_email(
            [], ["IKEA Canada"], ["The Brick: timeout"]
        )

        self.assertIn("no sectional/modular deals", subject.lower())
        self.assertIn("IKEA Canada", body)
        self.assertIn("The Brick: timeout", body)
        self.assertIn("No qualifying deals", html_body)

    def test_email_includes_product_photo_and_escapes_product_content(self):
        deal = _make_deal(
            "Retailer",
            "Modular Couch <blue>",
            "https://example.com/couch",
            Decimal("400"),
            Decimal("1000"),
            "https://images.example.com/couch.jpg",
        )
        assert deal is not None

        _, body, html_body = _format_email([deal], ["Retailer"], [])

        self.assertIn("Photo: https://images.example.com/couch.jpg", body)
        self.assertIn('src="https://images.example.com/couch.jpg"', html_body)
        self.assertIn("Modular Couch &lt;blue&gt;", html_body)

    @patch.dict(
        "os.environ",
        {
            "EMAIL_USER": "sender@example.com",
            "EMAIL_PASSWORD": "test-password",
            "EMAIL_TO": "recipient@example.com",
        },
    )
    @patch("notify.smtplib.SMTP_SSL")
    def test_send_email_includes_html_alternative(self, smtp_ssl):
        send_email("Test subject", "Plain text body", "<img src='photo.jpg'>")

        message = smtp_ssl.return_value.__enter__.return_value.send_message.call_args.args[
            0
        ]
        self.assertEqual(message.get_body(preferencelist=("html",)).get_content_type(), "text/html")
        self.assertIn("photo.jpg", message.get_body(preferencelist=("html",)).get_content())


if __name__ == "__main__":
    unittest.main()
