from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from scripts import web_server


class DefaultApiModelsTest(unittest.TestCase):
    def test_credentials_free_health_keeps_designer_on_gpt_5_5(self) -> None:
        def no_settings(_request):
            raise HTTPException(412, detail={"code": "no_api_key"})

        with (
            patch.object(web_server, "SETTINGS", None),
            patch.object(
                web_server,
                "_settings_for_request",
                side_effect=no_settings,
            ),
        ):
            response = TestClient(web_server.app).get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "needs_setup")
        self.assertEqual(payload["models"]["designer"], "gpt-5.5")
        self.assertEqual(payload["models"]["planner"], "gpt-5.5")
        self.assertEqual(
            {
                payload["models"][role]
                for role in (
                    "enhancer",
                    "claim_graph",
                    "deck_outline",
                    "paper_memory",
                    "critic",
                    "composer",
                    "ingest",
                )
            },
            {"gpt-5.4-nano"},
        )


if __name__ == "__main__":
    unittest.main()
