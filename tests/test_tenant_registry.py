from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from rental_automation.models import Base, Tenant, TenantIdentifier
from rental_automation.tenant_registry import import_tenants


def test_import_creates_and_updates_tenant_by_unit(tmp_path):
    source = tmp_path / "tenants.csv"
    source.write_text(
        "name,unit_number,phone,monthly_rent,maintenance_fee,opening_balance,due_day,upi_vpa,expected_sender_name\n"
        "Asha Rao,A-12,+919876543210,12000,500,250,7,asha@okbank,Asha Rao\n",
        encoding="utf-8",
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert import_tenants(session, source) == 1
        assert import_tenants(session, source) == 1

        tenants = list(session.scalars(select(Tenant)))
        assert len(tenants) == 1
        assert tenants[0].monthly_rent == Decimal("12000")
        assert tenants[0].due_day == 7
        assert tenants[0].upi_vpa == "asha@okbank"
        assert session.query(TenantIdentifier).count() == 1
