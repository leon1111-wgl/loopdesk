"""Secret references: a tool definition names a secret, the deployment holds its value.

    "auth": {"secret": "HARBOURLINE_API", "scheme": "bearer"}   ->   LOOPDESK_SECRET_HARBOURLINE_API

The value never enters the database, an API response, a trace span or the audit log; an
administrator who can edit tools can choose which secret a tool uses, not read one. A secret's
name must be declared for the tenant (``LOOPDESK_SECRET_<NAME>_TENANT=<slug>``, or listed in the
tenant pack): otherwise one tenant could reference another tenant's credentials by name.
"""

from __future__ import annotations

import os
import re

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SYNTHETIC_PREFIX = "SYNTHETIC_"  # the demo merchants' secrets: fixed, public, accepted only by them


class SecretError(ValueError):
    pass


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise SecretError("密钥引用只能用大写字母、数字和下划线，例如 HARBOURLINE_API")
    return name


def resolve(name: str, tenant_slug: str) -> str:
    """The secret's value for this tenant, or SecretError (missing, or bound to another tenant)."""
    check_name(name)
    if name.startswith(SYNTHETIC_PREFIX):
        return f"synthetic-{tenant_slug}-{name.lower()}"
    value = os.environ.get(f"LOOPDESK_SECRET_{name}")
    if not value:
        raise SecretError(f"密钥 {name} 未配置")
    owner = os.environ.get(f"LOOPDESK_SECRET_{name}_TENANT")
    if owner != tenant_slug:
        raise SecretError(f"密钥 {name} 不属于这个租户")
    return value
