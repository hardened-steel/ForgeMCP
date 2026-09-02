"""Regression coverage for the one known third-party warning filter."""

from __future__ import annotations

import warnings

from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning


_LIFESPAN_WARNING = "Field 'lifespan' has an incomplete definition"


def test_third_party_lifespan_filter_is_exact_and_does_not_hide_forgemcp_warnings() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.resetwarnings()
        warnings.filterwarnings(
            "ignore",
            message=_LIFESPAN_WARNING,
            category=IncompleteFieldDefinitionWarning,
            module=r"pydantic_settings\.sources\.utils",
        )
        warnings.warn_explicit(
            _LIFESPAN_WARNING,
            IncompleteFieldDefinitionWarning,
            filename="pydantic_settings/sources/utils.py",
            lineno=1,
            module="pydantic_settings.sources.utils",
        )
        warnings.warn("ForgeMCP regression warning must remain visible", UserWarning)

    assert [str(item.message) for item in captured] == ["ForgeMCP regression warning must remain visible"]
