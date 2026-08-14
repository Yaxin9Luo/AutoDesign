from __future__ import annotations

import unittest
from dataclasses import replace

from fastapi import HTTPException

from autodesign.config import Settings, authoring_max_attempts_for
from scripts.web_server import (
    _conversation_from_design_events,
    _settings_with_authoring_max_attempts,
    _validated_authoring_max_attempts,
)


class AuthoringAttemptBudgetTests(unittest.TestCase):
    @staticmethod
    def _settings() -> Settings:
        return Settings(
            anthropic_api_key="",
            anthropic_base_url=None,
            gemini_api_key="",
            designer_model="designer",
            critic_model="critic",
        )

    def test_artifact_defaults_are_quality_weighted(self) -> None:
        settings = self._settings()
        expected = {
            "poster": 12,
            "deck": 12,
            "slides": 12,
            "landing": 4,
            "video": 4,
        }

        for artifact_type, value in expected.items():
            with self.subTest(artifact_type=artifact_type):
                self.assertEqual(
                    authoring_max_attempts_for(settings, artifact_type),
                    value,
                )

    def test_explicit_override_wins_for_every_artifact(self) -> None:
        settings = replace(self._settings(), authoring_max_attempts_override=7)

        for artifact_type in ("poster", "deck", "landing", "video"):
            with self.subTest(artifact_type=artifact_type):
                self.assertEqual(
                    authoring_max_attempts_for(settings, artifact_type),
                    7,
                )

    def test_legacy_nondefault_attempt_setting_remains_an_override(self) -> None:
        settings = replace(self._settings(), designer_author_max_attempts=3)

        for artifact_type in ("poster", "deck", "landing", "video"):
            with self.subTest(artifact_type=artifact_type):
                self.assertEqual(
                    authoring_max_attempts_for(settings, artifact_type),
                    3,
                )

    def test_request_budget_defaults_and_boundaries(self) -> None:
        settings = self._settings()

        self.assertEqual(_validated_authoring_max_attempts(None, "landing", settings), 4)
        self.assertEqual(_validated_authoring_max_attempts(None, "video", settings), 4)
        self.assertEqual(_validated_authoring_max_attempts(None, "poster", settings), 12)
        self.assertEqual(_validated_authoring_max_attempts(1, "landing", settings), 1)
        self.assertEqual(_validated_authoring_max_attempts(12, "video", settings), 12)
        for value in (0, 13):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as caught:
                    _validated_authoring_max_attempts(value, "video", settings)
                self.assertEqual(caught.exception.status_code, 422)

    def test_request_budget_replace_does_not_mutate_base_settings(self) -> None:
        base = self._settings()

        resolved = _settings_with_authoring_max_attempts(base, "landing", 7)

        self.assertEqual(resolved.authoring_max_attempts_override, 7)
        self.assertIsNone(base.authoring_max_attempts_override)

    def test_history_rebuild_preserves_authoring_budget_for_resume(self) -> None:
        conversation = _conversation_from_design_events(
            "conv",
            [{
                "event": "message.user_submitted",
                "run_id": "run_budget",
                "_ts_ms": 1,
                "data": {
                    "brief": "Create a landing page",
                    "artifact_type": "landing",
                    "authoring_max_attempts": 7,
                },
            }],
            set(),
        )

        self.assertIsNotNone(conversation)
        assert conversation is not None
        self.assertEqual(
            conversation["messages"][0]["task_payload"]["authoring_max_attempts"],
            7,
        )

    def test_history_rebuild_omits_missing_authoring_budget(self) -> None:
        conversation = _conversation_from_design_events(
            "conv",
            [{
                "event": "message.user_submitted",
                "run_id": "run_legacy",
                "_ts_ms": 1,
                "data": {
                    "brief": "Create a landing page",
                    "artifact_type": "landing",
                },
            }],
            set(),
        )

        self.assertIsNotNone(conversation)
        assert conversation is not None
        self.assertNotIn(
            "authoring_max_attempts",
            conversation["messages"][0]["task_payload"],
        )


if __name__ == "__main__":
    unittest.main()
