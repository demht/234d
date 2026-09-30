import unittest
from types import SimpleNamespace
from logic import validate_collection, validate_max_number, pick_username, gift_attributes, format_resell_amount, Hit, describe_hit


class TestLogic(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(validate_collection("IceCream"), "IceCream")
        with self.assertRaises(ValueError):
            validate_collection("Ice Cream")

    def test_range(self):
        self.assertEqual(validate_max_number("342255"), 342255)
        with self.assertRaises(ValueError):
            validate_max_number("-1")

    def test_public_username(self):
        self.assertEqual(pick_username(SimpleNamespace(username="ExampleUser")), "ExampleUser")
        self.assertIsNone(pick_username(SimpleNamespace(username=None, usernames=[])))
        entry = SimpleNamespace(username="OtherExample", active=True)
        self.assertEqual(pick_username(SimpleNamespace(username=None, usernames=[entry])), "OtherExample")

    def test_attributes(self):
        model = type("StarGiftAttributeModel", (), {})()
        model.name = "Mad Hatter"
        self.assertEqual(gift_attributes(SimpleNamespace(attributes=[model]))["model"], "Mad Hatter")

    def test_price_stars_and_ton(self):
        StarsAmount = type("StarsAmount", (), {})
        stars = StarsAmount(); stars.amount = 3600; stars.nanos = 0
        StarsTonAmount = type("StarsTonAmount", (), {})
        ton = StarsTonAmount(); ton.amount = 1250000000
        self.assertEqual(format_resell_amount([stars, ton]), "3 600 ⭐ / 1.25 TON")

    def test_html_escape(self):
        hit = Hit(1, "TestUser", "<script>", "IceCream-12", "<b>Ice Cream</b>", 12)
        text = describe_hit(hit)
        self.assertNotIn("<b>Ice Cream</b>", text)
        self.assertIn("&lt;b&gt;Ice Cream&lt;/b&gt;", text)

    def test_market_card(self):
        hit = Hit(1, "TestUser", "User", "HangingStar-1", "Hanging Star", 1, source="market", price="3 600 ⭐")
        text = describe_hit(hit, "LAKES TEAM")
        self.assertIn("LAKES TEAM", text)
        self.assertIn("Telegram Market", text)
        self.assertIn("3 600 ⭐", text)
        self.assertIn("Продавец", text)
        self.assertIn("<blockquote>", text)
        self.assertIn("ПАРСЕР — LAKES TEAM", text)
        self.assertIn("Маркет", text)


if __name__ == "__main__":
    unittest.main()
