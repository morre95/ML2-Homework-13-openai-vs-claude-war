"""Create/refresh proxy virtual keys and the .box.env file compose reads.

  uv run scripts/make_keys.py build              # claude-box + codex-box build keys
  uv run scripts/make_keys.py eval NAME [...]    # unlimited eval keys (both vendors)

Existing keys are kept, so usage history stays attached to the same names.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
KEYS = ROOT / "proxy" / "config" / "keys.json"
BOX_ENV = ROOT / ".box.env"


def load() -> dict:
    return json.loads(KEYS.read_text()).get("keys", {}) if KEYS.exists() else {}


def save(keys: dict) -> None:
    KEYS.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEYS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keys": keys}, indent=2))
    tmp.replace(KEYS)


def ensure(keys: dict, name: str, vendors: list[str], max_tokens: int | None) -> str:
    for k, v in keys.items():
        if v["name"] == name:
            v.update(vendors=vendors, max_tokens=max_tokens)
            return k
    k = "vk-" + secrets.token_urlsafe(24)
    keys[k] = {"name": name, "vendors": vendors, "max_tokens": max_tokens}
    return k


def build_keys() -> None:
    budget = int(os.environ.get("BUILD_TOKEN_BUDGET", "2000000"))
    cfg = yaml.safe_load((ROOT / "eval" / "config.yaml").read_text())
    keys = load()
    ck = ensure(keys, "claude-box", ["anthropic"], budget)
    xk = ensure(keys, "codex-box", ["openai"], budget)
    save(keys)
    BOX_ENV.write_text(
        f"CLAUDE_BOX_KEY={ck}\nCODEX_BOX_KEY={xk}\n"
        f"EVAL_CLAUDE_MODEL={cfg['models']['claude']}\nEVAL_GPT_MODEL={cfg['models']['gpt']}\n"
        f"BUILD_TOKEN_BUDGET={budget}\n")
    print(f"build keys ready (budget {budget:,} tokens each) -> {KEYS.relative_to(ROOT)}, .box.env")


def eval_keys(names: list[str]) -> dict[str, str]:
    keys = load()
    out = {n: ensure(keys, n, ["anthropic", "openai"], None) for n in names}
    save(keys)
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["build"]:
        build_keys()
    elif sys.argv[1:2] == ["eval"]:
        print(json.dumps(eval_keys(sys.argv[2:]), indent=2))
    else:
        sys.exit(__doc__)
