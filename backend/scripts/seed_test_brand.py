"""Seed a deterministic test brand row so the US1 happy path is observable
without first onboarding documents (which lands in US2 / T049 onwards).

Run from the repo root:

    python -m backend.scripts.seed_test_brand

Idempotent — re-running on an existing DB is a no-op.
"""

from __future__ import annotations

# Load .env before importing the persistence module so AURA_DATA_DIR /
# AURA_DATABASE_URL apply to the engine constructed at module import time.
from dotenv import load_dotenv

load_dotenv()

from backend.persistence import session as session_module  # noqa: E402
from backend.persistence.repository import BrandRepository  # noqa: E402

TEST_BRAND_ID = "01HX0000TESTBRAND0000000001"
TEST_BRAND_DISPLAY_NAME = "Aura Test Brand"


def main() -> str:
    with session_module.SessionLocal() as session:
        existing = BrandRepository.get(session, TEST_BRAND_ID)
        if existing is not None:
            print(f"brand already present: id={existing.id} display_name={existing.display_name!r}")
            return existing.id

        brand = BrandRepository.create(
            session, brand_id=TEST_BRAND_ID, display_name=TEST_BRAND_DISPLAY_NAME
        )
        session.commit()
        print(f"created brand: id={brand.id} display_name={brand.display_name!r}")
        return brand.id


if __name__ == "__main__":
    main()
