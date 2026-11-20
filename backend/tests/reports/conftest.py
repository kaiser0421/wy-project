import pytest


@pytest.fixture
def settings_overrides(tmp_path, request):
    return {
        "import_dir": str(tmp_path / "imports"),
        "export_dir": str(tmp_path / "exports"),
        **getattr(request, "param", {}),
    }
