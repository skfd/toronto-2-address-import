from t2 import tag_diff


def test_compare_same_street_with_different_suffix_spelling():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "123", "addr:street": "Main St"},
        {"addr:housenumber": "123", "addr:street": "Main Street"},
    )
    by_tag = {r["tag"]: r for r in rows}
    assert by_tag["addr:street"]["status"] == "SAME"
    assert by_tag["addr:housenumber"]["status"] == "SAME"


def test_compare_housenumber_case_insensitive():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "12a"},
        {"addr:housenumber": "12A"},
    )
    assert rows[0]["status"] == "SAME"


def test_compare_add_when_osm_missing():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "123", "addr:city": "Toronto"},
        {"addr:housenumber": "123"},
    )
    by_tag = {r["tag"]: r for r in rows}
    assert by_tag["addr:city"]["status"] == "ADD"


def test_compare_change_on_differing_housenumbers():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "123"},
        {"addr:housenumber": "125"},
    )
    assert rows[0]["status"] == "CHANGE"


def test_compare_missing_proposed_when_only_osm_has_it():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "123"},
        {"addr:housenumber": "123", "addr:unit": "4B"},
    )
    by_tag = {r["tag"]: r for r in rows}
    assert by_tag["addr:unit"]["status"] == "MISSING_PROPOSED"


def test_compare_skips_keys_neither_side_has():
    rows = tag_diff.compare_tags({"addr:housenumber": "123"}, {"addr:housenumber": "123"})
    tags = {r["tag"] for r in rows}
    assert "addr:postcode" not in tags
    assert "addr:country" not in tags


def test_compare_handles_none_osm():
    rows = tag_diff.compare_tags(
        {"addr:housenumber": "123", "addr:street": "Main St"},
        None,
    )
    assert all(r["status"] == "ADD" for r in rows)
    assert {r["tag"] for r in rows} == {"addr:housenumber", "addr:street"}


def test_compare_unicode_street():
    rows = tag_diff.compare_tags(
        {"addr:street": "Boulevard René-Lévesque"},
        {"addr:street": "Boulevard René-Lévesque"},
    )
    assert rows[0]["status"] == "SAME"


def test_geom_hint_node():
    assert tag_diff.geom_hint({"type": "node", "tags": {}}) == "node"


def test_geom_hint_way_with_building():
    assert tag_diff.geom_hint({"type": "way", "tags": {"building": "yes"}}) == "way-polygon"


def test_geom_hint_way_with_area_yes():
    assert tag_diff.geom_hint({"type": "way", "tags": {"area": "yes"}}) == "way-polygon"


def test_geom_hint_way_plain_line():
    assert tag_diff.geom_hint({"type": "way", "tags": {"highway": "residential"}}) == "way-line"


def test_geom_hint_relation_multipolygon():
    assert tag_diff.geom_hint({"type": "relation", "tags": {"type": "multipolygon"}}) == "relation-polygon"


def test_geom_hint_relation_building():
    assert tag_diff.geom_hint({"type": "relation", "tags": {"building": "yes"}}) == "relation-polygon"
