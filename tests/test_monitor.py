import copy
import datetime as dt
import email.utils
import json
from pathlib import Path
import threading
import unittest
import tempfile
from unittest.mock import Mock, patch

import monitor as m


def config_from_payload(payload, include_product):
    stores = {store["storeNumber"]: store["storeName"]
              for store in payload["body"]["stores"]}
    products = []
    for part_number, availability in payload["body"]["stores"][0]["partsAvailability"].items():
        regular = availability["messageTypes"]["regular"]
        product_name = regular["storePickupProductTitle"]
        if include_product(part_number, product_name):
            products.append({
                "product_name": product_name,
                "part_number": part_number,
                "product_url": ("https://www.apple.com.cn/shop/buy-iphone/iphone-model/"
                                + part_number.lower()),
            })
    raw = {"location": "100000", "city": "北京", "stores": stores,
           "interval_seconds": 30, "timeout_seconds": 60,
           "max_cache_age_seconds": 30, "desktop_notifications": True,
           "sound": True, "products": products}
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return m.load_config(path)


class StockTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / "fixtures" / "beijing-unavailable.json"
        self.payload = json.loads(fixture.read_text(encoding="utf-8"))
        self.config = config_from_payload(
            self.payload, lambda part_number, _: part_number == "MJT84CH/A")

    def part(self, index=0):
        return self.payload["body"]["stores"][index]["partsAvailability"]["MJT84CH/A"]

    def rows(self):
        return m.parse_stock(self.payload, self.config)

    def test_real_response_six_unavailable(self):
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["available"] is False for row in rows.values()))

    def test_eligible_alone_is_not_stock(self):
        self.assertTrue(self.part()["storePickEligible"])
        self.assertFalse(self.rows()["R448"]["available"])

    def test_available_requires_eligible(self):
        self.part()["pickupDisplay"] = "available"
        self.assertTrue(self.rows()["R448"]["available"])
        self.part()["storePickEligible"] = False
        self.assertIsNone(self.rows()["R448"]["available"])

    def test_missing_store_is_unknown(self):
        self.payload["body"]["stores"].pop(0)
        self.assertIsNone(self.rows()["R448"]["available"])

    def test_unknown_display_not_false_positive(self):
        self.part()["pickupDisplay"] = "newStatus"
        self.part()["pickupSearchQuote"] = "今天可取货"
        self.assertIsNone(self.rows()["R448"]["available"])

    def test_wrong_product_name_is_unknown(self):
        self.part()["pickupDisplay"] = "available"
        self.part()["messageTypes"]["regular"]["storePickupProductTitle"] = "iPhone 18 Pro Max 256GB 银色"
        self.assertIsNone(self.rows()["R448"]["available"])

    def test_wrong_city_is_unknown(self):
        self.payload["body"]["stores"][0]["city"] = "天津"
        self.assertIsNone(self.rows()["R448"]["available"])

    def test_duplicate_store_rejected(self):
        self.payload["body"]["stores"].append(copy.deepcopy(self.payload["body"]["stores"][0]))
        with self.assertRaises(m.QueryError):
            self.rows()

    def test_empty_and_business_error_rejected(self):
        for payload in ({}, [], {"body": {"stores": []}}, {"head": {"status": "541"}}):
            with self.subTest(payload=payload), self.assertRaises(m.QueryError):
                m.parse_stock(payload, self.config)

    def test_restock_dedup_and_unknown_preservation(self):
        tracker = m.Changes()
        self.assertEqual(tracker.update(self.rows()), [])
        self.part()["pickupDisplay"] = "available"
        self.assertEqual(len(tracker.update(self.rows())), 1)
        self.assertEqual(tracker.update(self.rows()), [])
        self.part()["pickupDisplay"] = "unknown"
        self.assertEqual(tracker.update(self.rows()), [])
        self.part()["pickupDisplay"] = "available"
        self.assertEqual(tracker.update(self.rows()), [])
        self.part()["pickupDisplay"] = "unavailable"
        tracker.update(self.rows())
        self.part()["pickupDisplay"] = "available"
        self.assertEqual(len(tracker.update(self.rows())), 1)

    def test_first_observation_in_stock_alerts(self):
        self.part()["pickupDisplay"] = "available"
        self.assertEqual(len(m.Changes().update(self.rows())), 1)

    def test_browser_payload_rate_limit_and_block(self):
        for status, retry, reset in ((429, 600, False), (541, 0, True), (403, 0, True)):
            with self.subTest(status=status), self.assertRaises(m.QueryError) as caught:
                m.decode_browser_payload({"status": status, "body": "{}",
                                          "retryAfter": str(retry)}, self.config, "R448")
            self.assertEqual(caught.exception.retry_after, retry)
            self.assertEqual(caught.exception.reset_session, reset)

    def test_browser_payload_html_and_stale_cache_rejected(self):
        for body, age in (("<html>blocked</html>", "0"),
                          (json.dumps(self.payload), "60")):
            with self.subTest(age=age), self.assertRaises(m.QueryError):
                m.decode_browser_payload({"status": 200, "body": body, "age": age},
                                         self.config, "R448")

    def test_inventory_request_contains_browser_handshake_parameters(self):
        request = m.browser_inventory_request(self.config, "R448")
        self.assertEqual(request["url"], "https://www.apple.com.cn/shop/retail/pickup-message")
        self.assertIn(("fae", "true"), request["pairs"])
        self.assertIn(("pl", "true"), request["pairs"])
        self.assertIn(("mts.0", "regular"), request["pairs"])
        self.assertIn(("parts.0", "MJT84CH/A"), request["pairs"])
        self.assertEqual(request["pairs"][-1], ("store", "R448"))
        expression = m.browser_fetch_expression(request)
        self.assertIn("credentials:'same-origin'", expression)
        self.assertIn("X-Requested-With", expression)

    def test_retry_after_http_date_and_backoff_cap(self):
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=600)
        self.assertGreater(m.retry_seconds(email.utils.format_datetime(future)), 598)
        self.assertEqual(m.backoff(1000), 300)
        self.assertEqual(m.retry_seconds("nonsense"), 0)

    def test_bark_validation(self):
        self.assertEqual(m.validate_bark("https://api.day.app/example/"), "https://api.day.app/example")
        for value in ("http://api.day.app/key", "https://api.day.app", "https://api.day.app/key/message",
                      "https://user:secret@api.day.app/key", "https://api.day.app/key?x=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.validate_bark(value)

    def test_multiple_bark_urls_are_parsed_and_deduplicated(self):
        first = "https://api.day.app/first"
        second = "https://push.example.com/second"
        self.assertEqual(m.parse_bark_urls(first + ", " + second + "\n" + first),
                         [first, second])
        with self.assertRaises(ValueError):
            m.parse_bark_urls(",".join("https://api.day.app/key%s" % i for i in range(9)))

    @patch("monitor.http.client.HTTPSConnection")
    def test_bark_post_payload_and_ack(self, constructor):
        response = constructor.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"code":200}'
        m.send_bark("https://api.day.app/example", "标题", "正文",
                    "https://www.apple.com.cn", "上海自提")
        request = constructor.return_value.request.call_args
        self.assertEqual(request.args[:2], ("POST", "/example"))
        data = json.loads(request.kwargs["body"])
        self.assertEqual(data["title"], "标题")
        self.assertEqual(data["level"], "timeSensitive")
        self.assertEqual(data["group"], "上海自提")
        self.assertEqual(data["icon"], "https://www.apple.com/apple-touch-icon.png")
        response.read.return_value = b'{"code":400}'
        with self.assertRaises(RuntimeError):
            m.send_bark("https://api.day.app/example", "标题", "正文", "https://www.apple.com.cn")

    @patch("monitor.log")
    def test_status_error_skips_bark_queue(self, _):
        notification = m.Notifications.__new__(m.Notifications)
        notification.config = {"product_url": "https://www.apple.com.cn"}
        notification.failed = threading.Event()
        desktop = Mock()
        bark = Mock()
        notification.channels = [("电脑", desktop, None), ("Bark 1", bark, None)]
        notification.send("库存监控暂时异常", "测试异常", include_bark=False)
        desktop.put_nowait.assert_called_once()
        bark.put_nowait.assert_not_called()

    @patch("monitor.log")
    def test_recovery_still_reaches_bark_queue(self, _):
        notification = m.Notifications.__new__(m.Notifications)
        notification.config = {"product_url": "https://www.apple.com.cn"}
        notification.failed = threading.Event()
        desktop = Mock()
        bark = Mock()
        notification.channels = [("电脑", desktop, None), ("Bark 1", bark, None)]
        notification.send("库存监控已恢复", "测试恢复")
        desktop.put_nowait.assert_called_once()
        bark.put_nowait.assert_called_once()


class MultiProductTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((Path(__file__).parent / "fixtures" / "beijing-multi.json").read_text())
        for store in self.payload["body"]["stores"]:
            parts = store["partsAvailability"]
            black = copy.deepcopy(parts["MJT84CH/A"])
            black["pickupDisplay"] = "unavailable"
            black["pickupSearchQuote"] = "不可取货"
            regular = black["messageTypes"]["regular"]
            regular["storePickupProductTitle"] = "iPhone 18 Pro 256GB 黑色"
            regular["basePartNumber"] = "MJT74"
            parts["MJT74CH/A"] = black
        self.config = config_from_payload(
            self.payload, lambda _, product_name: "Pro Max 2TB" not in product_name)

    def rows(self):
        return m.parse_all_stock(self.payload, self.config)

    def test_real_response_covers_all_84_combinations(self):
        self.assertEqual(len(self.config["products"]), 14)
        rows = self.rows()
        self.assertEqual(len(rows), 84)
        self.assertEqual([key for key, row in rows.items() if row["available"] is True],
                         ["R320|MJT84CH/A"])
        self.assertEqual(sum(row["available"] is False for row in rows.values()), 83)
        self.assertEqual(len({row["part_number"] for row in rows.values()}), 14)
        self.assertEqual(len({row["store_id"] for row in rows.values()}), 6)

    def test_missing_one_product_does_not_hide_other_stock(self):
        parts = self.payload["body"]["stores"][0]["partsAvailability"]
        del parts["MJY74CH/A"]
        parts["MJY64CH/A"]["pickupDisplay"] = "available"
        rows = self.rows()
        self.assertIsNone(rows["R448|MJY74CH/A"]["available"])
        self.assertTrue(rows["R448|MJY64CH/A"]["available"])
        self.assertEqual(sum(row["available"] is None for row in rows.values()), 1)

    def test_dedup_separates_stores_and_products(self):
        tracker = m.Changes()
        tracker.update(self.rows())
        stores = self.payload["body"]["stores"]
        stores[0]["partsAvailability"]["MJY74CH/A"]["pickupDisplay"] = "available"
        first = tracker.update(self.rows())
        self.assertEqual(len(first), 1)
        stores[0]["partsAvailability"]["MJY64CH/A"]["pickupDisplay"] = "available"
        stores[1]["partsAvailability"]["MJY74CH/A"]["pickupDisplay"] = "available"
        second = tracker.update(self.rows())
        self.assertEqual(len(second), 2)
        self.assertEqual({(r["store_id"], r["part_number"]) for r in second},
                         {("R448", "MJY64CH/A"), ("R320", "MJY74CH/A")})
        self.assertEqual(tracker.update(self.rows()), [])

    def test_notifications_group_same_sku_with_matching_link(self):
        rows = self.rows()
        selected = [rows["R448|MJY74CH/A"], rows["R320|MJY74CH/A"], rows["R448|MJY64CH/A"]]
        alerts = list(m.stock_alerts(selected, "测试时间", self.config))
        self.assertEqual(len(alerts), 2)
        self.assertIn("王府井", alerts[0][1])
        self.assertIn("三里屯", alerts[0][1])
        self.assertIn("256GB 银色", alerts[0][1])
        self.assertEqual(alerts[0][0], "北京 Apple Store 自提有货")
        self.assertTrue(alerts[0][2].endswith("/mjy74ch/a"))
        self.assertIn("256GB 黑色", alerts[1][1])
        self.assertTrue(alerts[1][2].endswith("/mjy64ch/a"))

    def test_request_batches_every_sku_for_each_store(self):
        request = m.browser_inventory_request(self.config, "R448")
        parts = [value for key, value in request["pairs"] if key.startswith("parts.")]
        self.assertEqual(len(parts), 14)
        self.assertEqual(set(parts), {p["part_number"] for p in self.config["products"]})

    @patch("monitor.ChromiumSession")
    def test_client_reuses_one_chrome_session_across_six_stores(self, session_type):
        fixture_rows = self.rows()
        session = session_type.return_value
        session.fetch_store.side_effect = [
            ({key: value for key, value in fixture_rows.items() if key.startswith(store + "|")}, 0)
            for store in self.config["stores"]
        ]
        client = m.AppleClient(self.config)
        rows, _, age = client.query()
        self.assertEqual(len(rows), 84)
        self.assertEqual(age, 0)
        self.assertEqual(session.fetch_store.call_count, 6)
        session_type.assert_called_once_with(self.config)

    def test_duplicate_sku_and_empty_products_rejected(self):
        for products in ([], [self.config["products"][0]] * 2):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                path.write_text(json.dumps({**self.config, "products": products}))
                with self.assertRaises(ValueError):
                    m.load_config(path)

    def test_config_drives_city_stores_storefront_and_notification_names(self):
        product = dict(self.config["products"][0])
        product["product_url"] = "https://www.apple.com/shop/buy-iphone/example-product"
        custom = {key: value for key, value in self.config.items()
                  if key not in ("storefront_url", "alert_title", "notification_group")}
        custom.update({"city": "上海", "location": "200000",
                       "stores": {"R999": "示例门店"}, "products": [product]})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(custom))
            loaded = m.load_config(path)
        self.assertEqual(loaded["storefront_url"], "https://www.apple.com")
        self.assertEqual(loaded["alert_title"], "上海 Apple Store 自提有货")
        self.assertEqual(loaded["notification_group"], "上海 Apple Store 自提")
        request = m.browser_inventory_request(loaded, "R999")
        self.assertEqual(request["url"], "https://www.apple.com/shop/retail/pickup-message")

    def test_example_config_is_structurally_valid(self):
        example = m.load_config(m.ROOT / "config.example.json")
        self.assertEqual(example["city"], "上海")
        self.assertEqual(len(example["stores"]), 1)
        self.assertEqual(len(example["products"]), 1)

    def test_legacy_single_config_still_works(self):
        legacy = {k: v for k, v in self.config.items() if k != "products"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(legacy))
            config = m.load_config(path)
        self.assertEqual(len(config["products"]), 1)
        self.assertEqual(len(m.parse_all_stock(self.payload, config)), 6)


if __name__ == "__main__":
    unittest.main()
