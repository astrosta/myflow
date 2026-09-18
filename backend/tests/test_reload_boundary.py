from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX
from agentkit.config.app_config import AppConfig

def test_startup_only_registry_matches_schema():
    for path in STARTUP_ONLY_FIELDS:
        field = AppConfig.model_fields.get(path)
        if field is None:
            continue
        assert field.description.startswith(STARTUP_ONLY_PREFIX)

    for name,field in AppConfig.model_fields.items():
        if field.description and field.description.startswith(STARTUP_ONLY_PREFIX):
            assert name in STARTUP_ONLY_FIELDS, (
                f"Field {name} is marked as startup-only but is not in STARTUP_ONLY_FIELDS"
            )