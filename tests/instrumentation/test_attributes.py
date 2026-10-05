import json

from nodeartifact.attributes import MAX_ATTRIBUTE_BYTES, JsonAttribute


def test_a_short_value_is_kept_whole():
    encoded = JsonAttribute.encode({"items": [1, 2, 3]})

    assert json.loads(encoded.text) == {"items": [1, 2, 3]}
    assert encoded.truncated is False


def test_a_long_list_loses_its_first_items():
    items = [f"{number:05d}" + "x" * 995 for number in range(40)]

    encoded = JsonAttribute.encode(items)

    kept = json.loads(encoded.text)
    assert encoded.truncated is True
    assert kept == items[-len(kept) :]
    assert len(kept) < len(items)
    assert len(encoded.text.encode()) <= MAX_ATTRIBUTE_BYTES


def test_the_longest_list_field_of_an_object_is_shortened_first():
    value = {
        "title": "report",
        "tags": ["a", "b"],
        "rows": ["y" * 1000 for _ in range(30)],
    }

    encoded = JsonAttribute.encode(value)

    kept = json.loads(encoded.text)
    assert encoded.truncated is True
    assert kept["title"] == "report"
    assert kept["tags"] == ["a", "b"]
    assert 0 < len(kept["rows"]) < 30


def test_a_last_item_too_long_on_its_own_is_cut():
    encoded = JsonAttribute.encode(["short", "z" * 20_000])

    assert encoded.truncated is True
    assert encoded.text.startswith('["zzz')
    assert len(encoded.text.encode()) <= MAX_ATTRIBUTE_BYTES
