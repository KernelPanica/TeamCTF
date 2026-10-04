"""Portal case catalog; parsing is shared with Arena, without runtime imports."""
from pathlib import Path
from shared.cases import (CaseCatalog, CaseFormatError, CaseLoader, CaseSpec,
                          CaseValidator, CheckSpec, ServiceSpec, RedSpec, BlueSpec)


DEFAULT_CASES = Path(__file__).resolve().parents[1] / 'case_catalog'
if not DEFAULT_CASES.is_dir():
    DEFAULT_CASES = Path(__file__).resolve().parents[2] / 'arena/cases'
