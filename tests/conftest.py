"""Fixtures for National Grid tests."""

from __future__ import annotations

import pytest

MOCK_USERNAME = "testuser@example.com"
MOCK_PASSWORD = "testpassword123"
MOCK_ACCOUNT_ID = "1234567890"
MOCK_ACCOUNT_ID_2 = "0987654321"
MOCK_SERVICE_POINT = "SP001"
MOCK_SERVICE_POINT_2 = "SP002"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: None,
    enable_custom_integrations: None,
) -> None:
    """Enable custom integrations and recorder in Home Assistant."""


def _mock_billing_account(account_id: str = MOCK_ACCOUNT_ID) -> dict:
    """Return a mock billing account."""
    return {
        "billingAccountId": account_id,
        "region": "KEDNY",
        "premiseNumber": "PREM001",
        "customerNumber": 987654321,
        "fuelTypes": [{"type": "Electric"}, {"type": "Gas"}],
        "meter": {
            "nodes": [
                {
                    "servicePointNumber": MOCK_SERVICE_POINT,
                    "meterNumber": "MTR001",
                    "meterPointNumber": "MPT001",
                    "fuelType": "Electric",
                    "hasAmiSmartMeter": True,
                },
                {
                    "servicePointNumber": MOCK_SERVICE_POINT_2,
                    "meterNumber": "MTR002",
                    "meterPointNumber": "MPT002",
                    "fuelType": "Gas",
                    "hasAmiSmartMeter": False,
                },
            ],
        },
    }


def _mock_usages() -> list[dict]:
    """Return mock energy usages."""
    return [
        {
            "usageType": "TOTAL_KWH",
            "usageYearMonth": 202501,
            "usage": 500.0,
        },
        {
            "usageType": "TOTAL_KWH",
            "usageYearMonth": 202412,
            "usage": 450.0,
        },
        {
            "usageType": "THERMS",
            "usageYearMonth": 202501,
            "usage": 30.0,
        },
    ]


def _mock_costs() -> list[dict]:
    """Return mock energy costs.

    month is 1-12 (not year-aware); date is YYYY-MM-01 and is the correct
    field to sort by for most-recent detection.  Includes a December→January
    year boundary so tests can verify date-based ordering is used.
    """
    return [
        {"fuelType": "ELECTRIC", "month": 12, "date": "2024-12-01", "amount": 105.00},
        {"fuelType": "GAS", "month": 12, "date": "2024-12-01", "amount": 38.00},
        {"fuelType": "ELECTRIC", "month": 1, "date": "2025-01-01", "amount": 120.50},
        {"fuelType": "GAS", "month": 1, "date": "2025-01-01", "amount": 45.00},
    ]


def _mock_ami_usages() -> list[dict]:
    """Return mock AMI 15-min usages."""
    return [
        {
            "date": "2025-01-15T12:00:00.000Z",
            "fuelType": "Electric",
            "quantity": 18.5,
        },
    ]


def _mock_interval_reads() -> list[dict]:
    """Return mock interval reads."""
    return [
        {
            "startTime": "2025-01-15T10:00:00.000Z",
            "endTime": "2025-01-15T10:15:00.000Z",
            "quantity": 0.5,
        },
        {
            "startTime": "2025-01-15T10:15:00.000Z",
            "endTime": "2025-01-15T10:30:00.000Z",
            "quantity": 0.4,
        },
    ]


def _mock_bills(account_id: str = MOCK_ACCOUNT_ID) -> list[dict]:
    """Return mock bill history (newest first)."""
    return [
        {
            "accountNumber": account_id,
            "statementDate": "2025-01-01",
            "dueDate": "2025-01-22",
            "status": "UNPAID",
            "currentChargesAmount": 145.50,
            "totalDueAmount": 145.50,
        },
        {
            "accountNumber": account_id,
            "statementDate": "2024-12-01",
            "dueDate": "2024-12-22",
            "status": "PAID",
            "currentChargesAmount": 132.00,
            "totalDueAmount": 132.00,
        },
    ]


def _mock_electric_bill_history() -> list[dict]:
    """Return mock electric bill history records (newest first)."""
    return [
        {
            "readDate": "2025-01-28",
            "readFromDate": "2024-12-28",
            "readDays": 31,
            "readType": "Actual",
            "totalKwh": 520.0,
            "utilityCharges": 98.40,
            "supplierCharges": 47.10,
            "latePayment": 0.0,
            "totalCharges": 145.50,
            "avgDailyUsage": 16.77,
        },
        {
            "readDate": "2024-12-28",
            "readFromDate": "2024-11-28",
            "readDays": 30,
            "readType": "Actual",
            "totalKwh": 490.0,
            "utilityCharges": 92.00,
            "supplierCharges": 44.00,
            "latePayment": 0.0,
            "totalCharges": 136.00,
            "avgDailyUsage": 16.33,
        },
    ]


def _mock_gas_bill_history() -> list[dict]:
    """Return mock gas bill history records (newest first)."""
    return [
        {
            "readDate": "2025-01-28",
            "readFromDate": "2024-12-28",
            "readDays": 31,
            "readType": "Actual",
            "totalTherms": 32.0,
            "utilityCharges": 28.80,
            "supplierCharges": 16.20,
            "latePayment": 0.0,
            "totalCharges": 45.00,
            "avgDailyUsage": 1.03,
        },
    ]


def _mock_account_links(
    account_id: str = MOCK_ACCOUNT_ID,
    next_reading_date: str | None = "2025-02-15",
) -> list[dict]:
    """Return mock linked accounts."""
    return [
        {
            "billingAccountId": account_id,
            "billingAccount": {
                "billingAccountId": account_id,
                "nextSchedReadingDate": next_reading_date,
            },
        }
    ]
