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
