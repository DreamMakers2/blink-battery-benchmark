from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


class _MarkupAudit(HTMLParser):
    void_elements = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.mismatches: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id in self.ids:
            self.duplicate_ids.add(element_id)
        elif element_id:
            self.ids.add(element_id)
        if tag not in self.void_elements:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        expected = self.stack[-1] if self.stack else None
        if expected != tag:
            self.mismatches.append((tag, expected))
            return
        self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id in self.ids:
            self.duplicate_ids.add(element_id)
        elif element_id:
            self.ids.add(element_id)


def test_markup_is_balanced_and_ids_are_unique() -> None:
    audit = _MarkupAudit()
    audit.feed(HTML)
    assert audit.stack == []
    assert audit.mismatches == []
    assert audit.duplicate_ids == set()


def test_webawesome_page_structure_and_local_assets() -> None:
    assert 'class="wa-theme-awesome wa-palette-bright wa-light"' in HTML
    assert HTML.count("<wa-page") == HTML.count("</wa-page>") == 1
    assert '<main id="main-content">' in HTML
    assert 'slot="main"' not in HTML
    assert 'slot="navigation"' not in HTML
    assert "<wa-drawer" not in HTML
    for asset in (
        "/static/vendor/webawesome/styles/webawesome.css",
        "/static/vendor/webawesome/styles/themes/awesome-local.css",
        "/static/vendor/webawesome/webawesome.loader.js",
        "/static/icon-setup.js",
        "/static/vendor/chart.umd.min.js",
        "/static/vendor/hls.min.js",
    ):
        assert asset in HTML


def test_custom_elements_are_explicitly_closed() -> None:
    assert re.search(r"<wa-[^>]+/>", HTML) is None
    for component in (
        "badge",
        "button",
        "callout",
        "card",
        "details",
        "dialog",
        "icon",
        "progress-bar",
    ):
        assert len(re.findall(rf"<wa-{component}(?:\s|>)", HTML)) == HTML.count(
            f"</wa-{component}>"
        )


def test_dashboard_contains_required_views_and_accessible_labels() -> None:
    required_ids = {
        "phase-badge",
        "overall-progress",
        "snapshot-image",
        "snapshot-timeouts",
        "stream-video",
        "stream-receiving",
        "stream-outage",
        "battery-chart",
        "accessible-history",
        "history-summary",
        "measurements-table-body",
        "errors-details",
        "confirmation-dialog",
    }
    audit = _MarkupAudit()
    audit.feed(HTML)
    assert required_ids <= audit.ids
    assert 'alt="Latest successful snapshot from the selected Blink doorbell"' in HTML
    assert 'aria-label="Live video from the selected Blink doorbell"' in HTML
    assert 'label="Overall experiment progress"' in HTML
    assert "Download complete measurement history as JSON" in HTML


def test_icons_and_reachable_theme_assets_are_fully_local() -> None:
    icon_names = set(re.findall(r'<wa-icon[^>]+name="([^"]+)"', HTML))
    icon_root = ROOT / "static" / "vendor" / "fontawesome" / "svgs" / "solid"
    assert icon_names
    assert {path.stem for path in icon_root.glob("*.svg")} == icon_names
    icon_setup = (ROOT / "static" / "icon-setup.js").read_text(encoding="utf-8")
    assert 'setIconPath("/static/vendor/fontawesome/svgs")' in icon_setup

    import_pattern = re.compile(r"@import\s+url\(['\"]?([^)'\"]+)")
    pending = [
        ROOT / "static" / "vendor" / "webawesome" / "styles" / "webawesome.css",
        ROOT / "static" / "vendor" / "webawesome" / "styles" / "themes" / "awesome-local.css",
    ]
    visited: set[Path] = set()
    while pending:
        stylesheet = pending.pop().resolve()
        if stylesheet in visited:
            continue
        visited.add(stylesheet)
        contents = stylesheet.read_text(encoding="utf-8")
        for target in import_pattern.findall(contents):
            assert not target.startswith(("http://", "https://", "//"))
            imported = (stylesheet.parent / target).resolve()
            assert imported.is_file(), f"missing CSS import {imported}"
            pending.append(imported)


def test_frontend_uses_safe_updates_and_mutating_posts() -> None:
    assert "innerHTML" not in JS
    assert 'method: "POST"' in JS
    assert 'body: "{}"' in JS
    assert "status.testIndex + 1" in JS
    for endpoint in (
        "/api/status",
        "/api/measurements",
        "/api/errors",
        "/latest.jpg",
        "/stream/index.m3u8",
    ):
        assert endpoint in JS
    for endpoint in ("start", "stop", "restart", "continue"):
        assert f'"/api/experiment/{endpoint}"' in JS
    assert "stream.outage_seconds" in JS
    assert "stream.fatal_outage_seconds" in JS
    assert 'dom.streamReceiving.textContent = stream.receiving ? "Receiving" : "No data"' in JS
    assert (
        'role="status" aria-live="polite" aria-atomic="true" '
        'aria-label="Stream reception status"' in HTML
    )
    assert "renderStreamHealthUnavailable();" in JS
    assert 'dom.streamReceiving.textContent = "Unknown"' in JS
    assert 'dom.streamOutage.textContent = "Status unavailable"' in JS
    assert (
        'runtime.status.state === "running_snapshot" || runtime.status.state === "recovery"' in JS
    )
    assert 'runtime.status.state.startsWith("running_")' not in JS


def test_style_uses_reference_mesh_and_responsive_layout() -> None:
    for color in ("#3150e8", "#24c8c6", "#ff5935", "#d75ec5", "#7047e5", "#ff823d"):
        assert color in CSS
    assert "radial-gradient" in CSS
    assert "grid-template-columns: minmax(0, 2fr)" in CSS
    assert "@media (max-width:" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
