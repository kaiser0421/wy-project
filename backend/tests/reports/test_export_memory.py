"""CP4 acceptance for an externally deployed API with a dedicated 512 MiB cgroup.

Seed a fresh database with scripts/seed_orders_500k.sql and set CP4_EVENT_ID from its
output. Start a fresh 512 MiB API container with export_dir on a disk-backed volume.
Expose that container's cgroup-v2 memory.peak and memory.max files to the test runner.
Set CP4_API_URL to the API base URL, CP4_EMAIL and CP4_PASSWORD to credentials for a
viewer or admin, and CP4_MEMORY_PEAK_PATH to the mounted memory.peak file. Run
the slow test while the existing k6 booking/SSE workload targets that same instance.
Record the booking p99 and SSE latency from that workload alongside this peak.
"""

import os
from pathlib import Path

import httpx
import pytest
from openpyxl import load_workbook

pytestmark = pytest.mark.slow


def test_export_500k_within_container_budget(tmp_path):
    url = os.environ.get("CP4_API_URL")
    if not url:
        pytest.skip("Set CP4_API_URL and the documented container acceptance variables")
    peak = Path(os.environ["CP4_MEMORY_PEAK_PATH"])
    memory_limit = int(peak.with_name("memory.max").read_text().strip())
    assert memory_limit == 512 * 1024**2
    output = tmp_path / "sales.xlsx"
    with httpx.Client(base_url=url, timeout=300) as client:
        login = client.post(
            "/api/v1/auth/login",
            headers={"Origin": os.environ.get("CP4_ORIGIN", "http://localhost:8080")},
            json={"email": os.environ["CP4_EMAIL"], "password": os.environ["CP4_PASSWORD"]},
        )
        login.raise_for_status()
        with client.stream(
            "GET", f"/api/v1/events/{os.environ['CP4_EVENT_ID']}/reports/sales.xlsx"
        ) as response:
            response.raise_for_status()
            with output.open("wb") as destination:
                for chunk in response.iter_bytes():
                    destination.write(chunk)
    memory_peak = int(peak.read_text().strip())
    print(f"API cgroup peak during 500,000-order export: {memory_peak / 1024**2:.1f} MiB")
    assert memory_peak <= memory_limit
    workbook = load_workbook(output, read_only=True)
    try:
        rows = workbook["Orders"].values
        assert next(rows)[1] == "Created At (Asia/Taipei)"
        count = total = 0
        for row in rows:
            assert isinstance(row[8], int)
            count += 1
            total += row[8]
        assert count == 500_000
        assert total == 500_000 * 1050
    finally:
        workbook.close()
