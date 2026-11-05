"""i18n contract: every key used in the UI must exist, in both languages.

Guards the cleanup done in stage 2D (370 dead keys removed) and the three
literal-key bugs found while doing it: `reason.trail` (rendered as the raw key
in the trades table), `dash.regime`, `bots.top_signals`.

Keys are plain literals (`t('dash.balance')`); the only dynamic call site is
`t(p.labelKey)` in StatsPage, whose keys are literals in the same file.

Run: pytest tests/test_i18n_keys.py -q
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
I18N = SRC / "i18n"

_KEY_RE = re.compile(r"^\s*'([^']+)':", re.M)
_T_CALL_RE = re.compile(r"\bt\(\s*'([^']+)'")


def _keys(path: Path) -> set[str]:
    return set(_KEY_RE.findall(path.read_text(encoding="utf-8")))


def _source_blob() -> str:
    parts = []
    for path in SRC.rglob("*.js*"):
        if I18N in path.parents:
            continue
        parts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


RU = _keys(I18N / "ru.js")
EN = _keys(I18N / "en.js")
BLOB = _source_blob()
USED = set(_T_CALL_RE.findall(BLOB))


def test_dictionaries_are_not_empty():
    assert len(RU) > 100, "ru.js looks empty or unparsable"
    assert len(EN) > 100, "en.js looks empty or unparsable"


def test_ru_and_en_have_the_same_keys():
    assert RU == EN, (
        f"only in ru: {sorted(RU - EN)[:10]} | only in en: {sorted(EN - RU)[:10]}"
    )


@pytest.mark.parametrize("lang,keys", [("ru", RU), ("en", EN)])
def test_every_used_key_is_translated(lang, keys):
    missing = sorted(USED - keys)
    assert not missing, f"{lang}.js is missing keys used by the UI: {missing}"


def test_no_dead_keys():
    """Every key in the dictionaries must be referenced somewhere in src."""
    dead = sorted(k for k in RU if k not in BLOB)
    assert not dead, (
        f"{len(dead)} i18n keys are not referenced anywhere (remove them): {dead[:15]}"
    )
