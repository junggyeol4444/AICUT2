import unittest

from backend.auth import ApiKeyGuard


class ApiKeyGuardTest(unittest.TestCase):
    def test_guard_is_backward_compatible_when_key_is_not_configured(self):
        guard = ApiKeyGuard(None)
        self.assertFalse(guard.enabled)
        self.assertTrue(guard.authorized("/api/projects", {}))

    def test_api_requires_exact_bearer_credential_when_enabled(self):
        guard = ApiKeyGuard("secret-value")
        self.assertFalse(guard.authorized("/api/projects", {}))
        self.assertFalse(guard.authorized("/api/projects", {"Authorization": "Bearer wrong"}))
        self.assertTrue(guard.authorized(
            "/api/projects", {"Authorization": "Bearer secret-value"},
        ))

    def test_health_oauth_callback_and_static_assets_remain_reachable(self):
        guard = ApiKeyGuard("secret")
        self.assertTrue(guard.authorized("/api/health", {}))
        self.assertTrue(guard.authorized("/api/youtube/oauth/callback", {}))
        self.assertTrue(guard.authorized("/assets/index.js", {}))

    def test_credentials_are_not_accepted_from_query_parameters(self):
        guard = ApiKeyGuard("secret")
        self.assertFalse(guard.authorized("/api/projects?api_key=secret", {}))


if __name__ == "__main__":
    unittest.main()
