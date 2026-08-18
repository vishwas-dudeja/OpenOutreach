# tests/test_vina_customizations.py
"""Regression tests enforcing Vina Technologies deployment customizations."""
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import ModelHTTPError

from openoutreach.core.cycle import run_daemon
from openoutreach.core.models import Campaign
from openoutreach.core.operator import campaigns
from tests.factories import UserFactory


@pytest.mark.django_db
class TestVinaCustomizations:
    def test_run_daemon_never_invokes_freemium_importer_on_startup(self):
        """run_daemon startup must never call _import_freemium_campaign.

        Dependencies are mocked so the daemon loop halts deterministically after startup
        without executing network calls or sleeping indefinitely.
        """
        UserFactory(is_active=True, is_staff=True)
        Campaign.objects.create(name="Production Campaign", is_freemium=False)

        with patch("openoutreach.core.cycle._import_freemium_campaign") as mock_import, \
                patch("openoutreach.core.cycle.read_mail_if_due"), \
                patch("openoutreach.core.cycle.refresh_capacities_if_due"), \
                patch("openoutreach.core.cycle.run_one_action",
                      side_effect=ModelHTTPError(status_code=400, model_name="test")):
            run_daemon()

        mock_import.assert_not_called()

    def test_campaigns_rotation_excludes_freemium_campaigns(self):
        """operator.campaigns() must filter out campaigns where is_freemium=True."""
        user = UserFactory(is_active=True, is_staff=True)
        prod = Campaign.objects.create(name="Production", is_freemium=False)
        free = Campaign.objects.create(name="Freemium Promo", is_freemium=True)
        prod.users.add(user)
        free.users.add(user)

        active_campaigns = campaigns()
        assert prod in active_campaigns
        assert free not in active_campaigns

    def test_nvidia_openai_compatible_disables_thinking(self):
        """NVIDIA OpenAI-compatible model must receive extra_body with enable_thinking=False."""
        from openoutreach.core.llm import build_llm_model

        model = build_llm_model(
            "openai_compatible:nvidia/nemotron-3.5-lightning-30b-a3b",
            "dummy_key",
            "https://integrate.api.nvidia.com/v1",
        )
        assert model.settings is not None
        assert model.settings.get("extra_body") == {
            "chat_template_kwargs": {
                "enable_thinking": False,
            },
        }

    def test_non_nvidia_openai_compatible_preserves_default_settings(self):
        """Generic non-NVIDIA OpenAI-compatible endpoints must not receive NVIDIA extra_body settings."""
        from openoutreach.core.llm import build_llm_model

        model = build_llm_model(
            "openai_compatible:custom-model",
            "dummy_key",
            "https://custom-llm.example.com/v1",
        )
        assert model.settings is None or "extra_body" not in model.settings

    def test_standard_providers_unchanged(self):
        """Standard providers build successfully without receiving NVIDIA extra_body settings."""
        from openoutreach.core.llm import build_llm_model

        providers = [
            ("openai:gpt-4o", ""),
            ("anthropic:claude-3-5-sonnet", ""),
            ("google:gemini-1.5-pro", ""),
            ("groq:llama-3.3-70b", ""),
            ("mistral:mistral-large", ""),
            ("cohere:command-r-plus", ""),
        ]
        for ai_model, api_base in providers:
            model = build_llm_model(ai_model, "dummy_key", api_base)
            settings = getattr(model, "settings", None)
            assert settings is None or "extra_body" not in settings
