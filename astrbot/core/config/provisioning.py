"""Read-only, startup provisioning for dashboard authentication and providers."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    redirect_uri: str
    allowed_organizations: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_callback(self):
        """Require HTTPS except for a loopback callback used during development.

        Returns:
            Validated GitHub configuration.

        Raises:
            ValueError: The callback URI is unsafe or uses the wrong path.
        """
        uri = urlsplit(self.redirect_uri)
        loopback_http = uri.scheme == "http" and uri.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if (
            (uri.scheme != "https" and not loopback_http)
            or not uri.hostname
            or uri.username
            or uri.password
            or uri.query
            or uri.fragment
            or uri.path != "/api/v1/auth/github/callback"
        ):
            raise ValueError(
                "GitHub OAuth requires an HTTPS callback (HTTP only on loopback) at /api/v1/auth/github/callback"
            )
        return self


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    password_login_enabled: bool = True
    github: GitHubConfig | None = None

    @model_validator(mode="after")
    def validate_login_method(self):
        """Require OAuth when local login is disabled.

        Returns:
            Validated authentication configuration.

        Raises:
            ValueError: No usable login method is configured.
        """
        if not self.password_login_enabled and self.github is None:
            raise ValueError("Disabling password login requires GitHub OAuth")
        return self


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    sources: list[dict]
    models: list[dict]

    @model_validator(mode="after")
    def validate_references(self):
        """Validate unique IDs and model references before applying any state.

        Returns:
            Validated provider configuration.

        Raises:
            ValueError: IDs are missing, duplicated or references are invalid.
        """
        for entries in (self.sources, self.models):
            ids = [entry.get("id") for entry in entries]
            if any(not isinstance(id_, str) or not id_.strip() for id_ in ids):
                raise ValueError("Every source and model requires a nonempty string ID")
            if len(set(ids)) != len(ids):
                raise ValueError("Source and model IDs must be unique within each list")
        source_ids = {source["id"] for source in self.sources}
        for source in self.sources:
            if not isinstance(source.get("type"), str) or not source["type"]:
                raise ValueError("Every source requires an AstrBot provider type")
        for model in self.models:
            if model.get("provider_source_id") not in source_ids:
                raise ValueError("Every model must reference a declared source")
            if not isinstance(model.get("model"), str) or not model["model"]:
                raise ValueError("Every model requires a model name")
        return self


class ProvisioningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    version: Literal[1]
    auth: AuthConfig = Field(default_factory=AuthConfig)
    providers: ProvidersConfig | None = None


class ProvisioningLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        """Reject duplicate keys instead of silently replacing configuration.

        Args:
            node: YAML mapping node.
            deep: Whether to recursively construct values.

        Returns:
            Parsed mapping.

        Raises:
            ValueError: A mapping contains duplicate keys.
        """
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate provisioning key")
        return super().construct_mapping(node, deep=deep)

    def construct_env(self, node):
        """Resolve an explicit environment reference without interpolating YAML.

        Args:
            node: Scalar containing an environment variable name.

        Returns:
            The nonempty environment value.

        Raises:
            ValueError: The variable is missing or empty.
        """
        name = self.construct_scalar(node)
        value = os.environ.get(name)
        if not value:
            raise ValueError("Missing provisioning environment variable")
        return value


ProvisioningLoader.add_constructor("!env", ProvisioningLoader.construct_env)


@lru_cache(maxsize=1)
def get_provisioning() -> ProvisioningConfig:
    """Load optional provisioning once per process; restart to apply changes.

    Returns:
        Validated provisioning, or defaults when no path is configured.

    Raises:
        ValueError: Provisioning is unreadable or invalid; values are not logged.
    """
    path = os.environ.get("ASTRBOT_PROVISIONING_FILE")
    if not path:
        return ProvisioningConfig(version=1)
    try:
        document = yaml.load(
            Path(path).read_text(encoding="utf-8"), Loader=ProvisioningLoader
        )
        return ProvisioningConfig.model_validate(document)
    except (OSError, yaml.YAMLError, ValidationError, ValueError, TypeError):
        raise ValueError(
            "Invalid AstrBot provisioning file; check schema and environment references"
        ) from None


def require_unmanaged_providers() -> None:
    """Reject dashboard writes before they mutate provisioned providers.

    Raises:
        ValueError: YAML owns provider configuration.
    """
    if get_provisioning().providers is not None:
        raise ValueError(
            "Providers are managed by YAML; edit the provisioning file and restart AstrBot"
        )
