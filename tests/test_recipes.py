import pytest

from menudb.recipes import Recipe, RecipeError, resolve


def pages(fixture_text):
    site = {
        "https://r.example/": fixture_text("index.html"),
        "https://r.example/food/": fixture_text("menus_page.html"),
    }
    return lambda url: (url, site[url])


def test_follow_then_select_all_pdfs(fixture_text):
    recipe = Recipe.model_validate({
        "start": "https://r.example/",
        "steps": [
            {"follow": {"text_matches": "(?i)menu"}},
            {"select": {"css": "#menus a[href$='.pdf']", "pick": "all"}},
        ],
        "content": "pdf",
    })
    assert resolve(recipe, pages(fixture_text)) == [
        "https://r.example/files/autumn-tasting.pdf",
        "https://r.example/files/autumn-vegetarian.pdf",
    ]


def test_no_steps_is_start_url():
    assert resolve(Recipe(start="https://r.example/menu"), lambda u: (u, "")) == ["https://r.example/menu"]


def test_drift_raises(fixture_text):
    recipe = Recipe.model_validate({
        "start": "https://r.example/",
        "steps": [{"follow": {"text_matches": "(?i)carte du jour"}}],
    })
    with pytest.raises(RecipeError):
        resolve(recipe, pages(fixture_text))


def test_regex_step_reads_urls_from_json():
    body = '{"data":[{"filePath":"aa.pdf","code":"Lunch_en"},{"filePath":"bb.pdf","code":"Dinner_en"}]}'
    recipe = Recipe.model_validate({
        "start": "https://r.example/api/files",
        "steps": [{"regex": {"pattern": r'"filePath":"([0-9a-z]+\.pdf)","code":"Dinner_en"',
                             "template": "/uploads/{1}"}}],
        "content": "pdf",
    })
    assert resolve(recipe, lambda u: (u, body)) == ["https://r.example/uploads/bb.pdf"]
