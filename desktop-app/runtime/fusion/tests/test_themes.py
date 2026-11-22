"""三套主题必须由同一组变量驱动：组件规则里不许漏掉任何主题的令牌。"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _blocks():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    out = {}
    for name, marker in (("paper", "\n:root {"),
                         ("wujin", '\n:root[data-theme="wujin"] {'),
                         ("qingci", '\n:root[data-theme="qingci"] {')):
        start = css.index(marker) + len(marker)
        end = css.index("\n}", start)
        out[name] = (set(re.findall(r"(--[a-z0-9-]+):", css[start:end])), css[end:])
    return out


def test_three_themes_are_declared():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert css.count(":root") >= 3
    for theme in ("wujin", "qingci"):
        assert f':root[data-theme="{theme}"] {{' in css, f"missing {theme}"


def test_every_variable_used_by_a_component_is_defined_in_root():
    blocks = _blocks()
    declared = blocks["paper"][0]
    rest = blocks["qingci"][1]
    used = set(re.findall(r"var\((--[a-z0-9-]+)", rest))
    missing = used - declared
    assert not missing, f"components use undeclared variables: {sorted(missing)}"


def test_themes_override_the_same_set_of_variables():
    blocks = _blocks()
    wujin, qingci = blocks["wujin"][0], blocks["qingci"][0]
    assert wujin == qingci, (
        f"theme drift: only-wujin={sorted(wujin - qingci)} only-qingci={sorted(qingci - wujin)}")
    # 主题只需覆盖视觉令牌，字体与边框常量留在 :root
    theme_only = {"--sans", "--mono", "--rule"}
    assert not (wujin & theme_only), "themes should not redeclare font/border constants"


def test_no_component_rule_hardcodes_a_hex_colour():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    body = css[css.index("* { box-sizing"):]
    leftovers = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    # 允许：主按钮的墨色高光、纯黑遮罩、印章描边
    allowed = {"#000", "#fff", "#fdf3ec", "#fff8f2", "#fdf2ec"}
    unexpected = [c for c in leftovers if c.lower() not in allowed]
    assert not unexpected, f"hardcoded colours outside tokens: {sorted(set(unexpected))}"


def test_toggle_cycles_all_three_themes_without_storage():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'const THEMES = ["paper", "wujin", "qingci"]' in js
    assert "dataset.theme" in js
    for sink in ("localStorage", "sessionStorage"):
        assert sink not in js, "theme must not be persisted (UI test forbids storage)"
