"""check_config(): always-invalid values in every environment, required values in production."""

from __future__ import annotations

from typing import Any

from app.config.settings import Settings, is_production, secret_values
from app.config.settings import check_config as _check_config
from tests.conftest import build_ai_settings, build_darwin_settings, build_engine_settings, build_settings

GOOD_PROD = {
    "environment": "production",
    "redis_url": "rediss://:placeholder-password@cache.example:6380/0",
    "applicationinsights_connection_string": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    "cors_allowed_origins": "https://app.example.com",
    "storage_backend": "general",
    "general_service_url": "https://general.example",
    "auth_issuer": "https://issuer.example/",
    "auth_audience": "slideforge",
    "auth_jwks_url": "https://issuer.example/.well-known/jwks.json",
}
GENERAL_SERVICE_PLACEHOLDER = "STORAGE_BACKEND=general: the General service adapter is not written yet (D5)."

# What production needs of the engine settings (app/config/engine.py).
GOOD_ENGINE_PROD = {"renderer_url": "https://render.example"}


#: A production AI configuration: the provider key is required there (a placeholder, not a key).
GOOD_AI_PROD = {"anthropic_api_key": "placeholder-anthropic-key"}
#: Darwin's production configuration: the image key is required there (a placeholder).
GOOD_DARWIN_PROD = {"openai_api_key": "placeholder-openai-key"}


def check_config(s: Settings, **engine: Any) -> list[str]:
    """check_config with explicit engine settings, so the process environment cannot leak in."""
    return _check_config(s, build_engine_settings(**engine), build_ai_settings(**GOOD_AI_PROD),
                         build_darwin_settings(**GOOD_DARWIN_PROD))


def test_a_default_local_config_has_no_problems():
    assert check_config(build_settings()) == []


def test_a_complete_production_config_has_only_the_general_service_placeholder():
    """Until the General service exists (D5), production cannot be fully configured:
    the only remaining problem is the stub adapter, and it keeps /readyz at 503."""
    assert check_config(build_settings(**GOOD_PROD), **GOOD_ENGINE_PROD) == [GENERAL_SERVICE_PLACEHOLDER]


def test_production_requires_tls_redis_telemetry_and_a_cors_origin():
    problems = check_config(build_settings(environment="production"))
    text = " ".join(problems)
    assert "rediss://" in text
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in text
    assert "CORS_ALLOWED_ORIGINS" in text


def test_production_rules_do_not_apply_in_development():
    assert check_config(build_settings(environment="development")) == []


def test_always_invalid_values_are_flagged_in_every_environment():
    s = build_settings(redis_protocol=4, redis_url="http://nope", request_id_header="X Bad")
    problems = " ".join(check_config(s))
    assert "REDIS_PROTOCOL" in problems
    assert "REDIS_URL" in problems
    assert "REQUEST_ID_HEADER" in problems


def test_an_unknown_environment_is_a_problem_not_a_guess():
    assert any("ENVIRONMENT" in p for p in check_config(build_settings(environment="prodd")))


def test_cors_origins_must_be_explicit_origins():
    assert any("'*'" in p for p in check_config(build_settings(cors_allowed_origins="*")))
    assert any("origin only" in p for p in check_config(build_settings(cors_allowed_origins="https://a.example/app")))
    assert any("not an http(s) origin" in p for p in check_config(build_settings(cors_allowed_origins="a.example")))


def test_cors_origins_must_be_https_in_production():
    s = build_settings(**{**GOOD_PROD, "cors_allowed_origins": "http://app.example.com"})
    assert any("https" in p for p in check_config(s))


def test_problems_never_quote_a_secret_value():
    s = build_settings(environment="production", redis_url="redis://:super-secret-pass@host:6379/0")
    assert all("super-secret-pass" not in p for p in check_config(s))


def test_is_production_explicit_environment_wins():
    assert is_production(build_settings(environment="staging"))
    assert is_production(build_settings(environment="production"))
    assert not is_production(build_settings(environment="local", website_instance_id="abc"))


def test_is_production_falls_back_to_running_on_azure():
    assert is_production(build_settings(environment="", website_instance_id="abc123"))
    assert not is_production(build_settings(environment=""))


def test_cors_origins_are_split_and_normalised():
    s = build_settings(cors_allowed_origins=" https://a.example/ , https://b.example ,")
    assert s.cors_origins == ["https://a.example", "https://b.example"]


def test_secret_values_include_the_redis_password_and_instrumentation_key():
    s = build_settings(
        redis_url="rediss://:redis-password-123@host:6380/0",
        applicationinsights_connection_string="InstrumentationKey=ikey-value-123;IngestionEndpoint=https://x",
    )
    assert set(secret_values(s)) == {"redis-password-123", "ikey-value-123"}


def test_production_requires_the_token_issuer_audience_and_jwks():
    problems = " ".join(check_config(build_settings(environment="production")))
    for name in ("AUTH_ISSUER", "AUTH_AUDIENCE", "AUTH_JWKS_URL"):
        assert name in problems


def test_auth_algorithms_must_be_asymmetric():
    for bad in ("HS256", "none", "RS256,HS512", ""):
        assert any("AUTH_ALGORITHMS" in p for p in check_config(build_settings(auth_algorithms=bad))), bad
    assert check_config(build_settings(auth_algorithms="RS256, ES256")) == []


def test_auth_jwks_url_must_be_https_in_production():
    s = build_settings(**{**GOOD_PROD, "auth_jwks_url": "http://issuer.example/jwks"})
    assert any("AUTH_JWKS_URL must use https" in p for p in check_config(s, **GOOD_ENGINE_PROD))


def test_production_requires_the_openai_key():
    problems = _check_config(build_settings(**GOOD_PROD), build_engine_settings(**GOOD_ENGINE_PROD),
                             build_ai_settings(**GOOD_AI_PROD), build_darwin_settings())
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_the_openai_key_is_a_secret_for_the_scrubber():
    values = secret_values(build_settings(), build_ai_settings(), build_darwin_settings(openai_api_key="placeholder-openai-key"))
    assert "placeholder-openai-key" in values
