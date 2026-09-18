from django.apps import AppConfig
from django.conf import settings


class SharedModelsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shared_models"
    verbose_name = "Shared Models (Minty / Module 1)"

    def ready(self):
        """Flip managed on for tests only.

        In production every model here is managed = False because Alembic in Minty
        owns the DDL. The SQLite test database has no Alembic, so without this the
        tables never exist and the whole suite errors at setup. Flipping the flag in
        ready() builds them from the models instead.

        Guarded by a setting that only config/settings_test.py sets, so production
        can never take this branch.
        """
        if not getattr(settings, "SHARED_MODELS_MANAGED_FOR_TESTING", False):
            return

        from shared_models import models

        for model in (
            models.User,
            models.Entity,
            models.UserEntity,
            models.CountryInfo,
            models.CurrencyInfo,
            models.EntityFunction,
            models.EntityFunctionMap,
            models.BillingPlan,
            models.BillingPolicy,
            models.AccountInfo,
            models.EntityPettycashSettings,
            models.EntitySaleSetting,
            models.Report,
            models.Invitation,
            models.SaleInfo,
        ):
            model._meta.managed = True
