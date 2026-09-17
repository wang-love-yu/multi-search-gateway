from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .providers import ProviderName


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=128)


class PasswordChange(Input):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=16, max_length=128)


class AccountInput(Input):
    provider: ProviderName
    name: str = Field(min_length=1, max_length=80, pattern=r'^\S(?:.*\S)?$')
    priority: int = Field(default=0, ge=0, le=1000)
    attempt_limit: int = Field(default=1000, ge=1, le=10000000)
    rpm: int = Field(default=30, ge=1, le=1000)
    reset_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class AccountUpdate(Input):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    attempt_limit: int | None = Field(default=None, ge=1, le=10000000)
    rpm: int | None = Field(default=None, ge=1, le=1000)
    reset_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class CredentialInput(Input):
    account_id: str = Field(min_length=32, max_length=32)
    name: str = Field(min_length=1, max_length=80)
    key: SecretStr = Field(min_length=8, max_length=512)

    @field_validator('key')
    @classmethod
    def printable_key(cls, key):
        if any(ord(c) < 33 or ord(c) > 126 for c in key.get_secret_value()):
            raise ValueError('Key must be printable ASCII without whitespace')
        return key


class Toggle(Input):
    enabled: bool


class RouteRule(Input):
    name: ProviderName
    enabled: bool = True


class RoutingInput(Input):
    providers: list[RouteRule] = Field(min_length=3, max_length=3)

    @field_validator('providers')
    @classmethod
    def exactly_three(cls, rules):
        if {rule.name for rule in rules} != {'tavily', 'exa', 'brave'}:
            raise ValueError('Include each provider exactly once')
        return rules


class ConfigInput(Input):
    rpm: int = Field(ge=1, le=600)
    daily_limit: int = Field(ge=1, le=100000)
    concurrency: int = Field(ge=1, le=20)
    max_attempts: int = Field(ge=1, le=10)
    timeout_seconds: int = Field(ge=3, le=30)
    api_enabled: bool
