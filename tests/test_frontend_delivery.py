from fastapi.testclient import TestClient

from app.main import app


def test_frontend_html_disables_cache_and_versions_all_local_assets():
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    html = response.text
    for asset in (
        "style.css", "api.js", "chat-state.js", "navigation-state.js",
        "tweaks-panel.jsx", "mascot.jsx", "components.jsx", "edit.jsx",
        "pages.jsx", "main.jsx",
    ):
        assert f'/{asset}?v=20260916-amap-quota-guard' in html


def test_frontend_scripts_are_revalidated():
    response = TestClient(app).get("/pages.jsx?v=20260916-amap-quota-guard")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    assert "function PlanningBriefCard" in response.text
    assert "memory.applied_facts" in response.text


def test_spa_entry_paths_are_served_and_never_cached():
    """/admin 等前端路由必须由后端发壳，否则直接刷新该地址会 404。

    这里只断言"发的是同一份壳"，权限判断属于接口层（见 test_admin_api.py）。
    """
    client = TestClient(app)
    for path in ("/", "/history", "/profile", "/admin"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store, max-age=0", path
        assert '<div id="root"></div>' in response.text, path
