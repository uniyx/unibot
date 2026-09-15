import unittest
from unittest.mock import patch

from cogs.faceit import (
    RATINGS_UNAVAILABLE_NOTE,
    FaceitAPI,
    FaceitRatingsUnavailable,
    _append_unique_note,
    _extract_recent_ratings,
)


class RecentRatingsParsingTests(unittest.TestCase):
    def test_current_camel_case_payload(self):
        data = {
            "payload": {
                "gameStats": {
                    "value": {
                        "matchRounds": [
                            {
                                "faceitRating": 1.10,
                                "faceitRatingT": 1.20,
                                "faceitRatingCt": 1.00,
                            },
                            {
                                "faceitRating": "1.30",
                                "faceitRatingT": "1.40",
                                "faceitRatingCt": "1.20",
                            },
                        ]
                    }
                }
            }
        }

        ratings = _extract_recent_ratings(data, limit=30)

        self.assertAlmostEqual(ratings["faceit_rating"], 1.20)
        self.assertAlmostEqual(ratings["faceit_rating_t"], 1.30)
        self.assertAlmostEqual(ratings["faceit_rating_ct"], 1.10)
        self.assertEqual(ratings["matches_count"], 2)

    def test_legacy_snake_case_payload(self):
        data = {
            "payload": {
                "cs2": {
                    "match_rounds": [
                        {
                            "faceit_rating": 0.90,
                            "faceit_rating_t": 1.00,
                            "faceit_rating_ct": 0.80,
                        },
                        {
                            "faceit_rating": 1.10,
                            "faceit_rating_t": 1.20,
                            "faceit_rating_ct": 1.00,
                        },
                    ]
                }
            }
        }

        ratings = _extract_recent_ratings(data, limit=1)

        self.assertAlmostEqual(ratings["faceit_rating"], 0.90)
        self.assertAlmostEqual(ratings["faceit_rating_t"], 1.00)
        self.assertAlmostEqual(ratings["faceit_rating_ct"], 0.80)
        self.assertEqual(ratings["matches_count"], 1)


class RatingsUnavailableTests(unittest.IsolatedAsyncioTestCase):
    def test_duplicate_note_and_encoded_space_are_normalized(self):
        notes = []

        _append_unique_note(notes, RATINGS_UNAVAILABLE_NOTE)
        _append_unique_note(notes, RATINGS_UNAVAILABLE_NOTE + " &#x20;")

        self.assertEqual(notes, [RATINGS_UNAVAILABLE_NOTE])

    async def test_cloudflare_block_reuses_one_stable_message(self):
        class BlockedResponse:
            status_code = 403
            headers = {"cf-mitigated": "challenge"}
            text = "Cloudflare challenge"

        api = FaceitAPI(None, "test-key", concurrency=1)
        with patch("cogs.faceit.curl_requests.get", return_value=BlockedResponse()) as get:
            with self.assertRaises(FaceitRatingsUnavailable) as first:
                await api._get_public_json("https://example.test", retries=1)
            with self.assertRaises(FaceitRatingsUnavailable) as second:
                await api._get_public_json("https://example.test", retries=1)

        self.assertEqual(str(first.exception), RATINGS_UNAVAILABLE_NOTE)
        self.assertEqual(str(second.exception), RATINGS_UNAVAILABLE_NOTE)
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
