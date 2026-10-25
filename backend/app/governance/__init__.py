"""Governance features every tenant can configure, rather than code per regulator.

    disclosure.py  the AI disclosure, per channel (HKMA GenAI circular 2024; 人工智能生成合成内容标识办法)
    script.py      simplified or traditional Chinese for what customers read

Tenant settings: ``tenants.locale`` (the script) and ``tenants.governance`` (JSON texts):

    {"disclosure": {"web": "…", "voice": "…"}}
"""
