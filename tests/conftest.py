import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import load_run_files  # noqa: E402
from pipeline.models import VC, Page, Signal  # noqa: E402

TODAY = date(2026, 10, 11)


@pytest.fixture(scope="session")
def rf():
    return load_run_files()


def ago(n: int) -> date:
    return TODAY - timedelta(days=n)


def make_vc(**kw) -> VC:
    base = dict(id=1, name="", firm="Acme Ventures", thesis="logistics", status="to_contact",
                last_contacted_at=None, last_signal_date=None, geo="US", stage_focus="Seed",
                is_investing=True, flag="")
    base.update(kw)
    return VC(**base)


def make_signal(days_ago=3, type="portfolio_investment", url="https://x.com/a") -> Signal:
    return Signal(type=type, summary="s", quote="q" * 25, url=url, published=ago(days_ago))


def make_page(text, days_ago=3, url="https://x.com/a") -> Page:
    return Page(url=url, title="t", published=ago(days_ago) if days_ago is not None else None, text=text)
