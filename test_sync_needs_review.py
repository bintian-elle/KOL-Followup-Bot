import unittest

from sync_needs_review import handoff_search_sources, is_brand_team_sender


class BrandTeamSenderTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "brand_team_emails": "mel@bluevua.com,jeremy.ku@bluevua.com",
            "brand_team_domains": "bluevua.com",
        }

    def test_matches_explicit_brand_team_email(self):
        self.assertTrue(is_brand_team_sender("jeremy.ku@bluevua.com", self.config))

    def test_matches_any_sender_from_configured_domain(self):
        self.assertTrue(is_brand_team_sender("new.person@bluevua.com", self.config))

    def test_rejects_similar_unconfigured_domain(self):
        self.assertFalse(is_brand_team_sender("person@fakebluevua.com", self.config))

    def test_domain_is_included_in_gmail_candidate_search(self):
        self.config["forwarding_mailbox"] = "partnerships@bluevua.com"
        self.assertIn("bluevua.com", handoff_search_sources(self.config))


if __name__ == "__main__":
    unittest.main()
