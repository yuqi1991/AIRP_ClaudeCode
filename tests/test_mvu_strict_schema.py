from airp.engine.mvu import Command, generate_schema, schema_from_definition, validate_command_strict


def _set(path, value):
    return Command("set", "", [path, value])


def test_strict_generated_schema_rejects_unknown_paths():
    schema = generate_schema({"weather": "clear", "互动对象": {}}, strict_template=True)

    assert validate_command_strict(_set("weather", "rain"), schema)[0]
    assert not validate_command_strict(_set("weather.unknown", "x"), schema)[0]
    assert not validate_command_strict(_set("new_root", "x"), schema)[0]


def test_explicit_card_wildcard_allows_dynamic_object_key_but_not_new_shape():
    base = {"互动对象": {"刻晴": {"好感度": 10}}}
    definition = {
        "fields": {
            "互动对象.*.好感度": {"type": "number", "nullable": False},
        }
    }
    schema = schema_from_definition(
        definition,
        fallback=generate_schema(base, strict_template=True),
    )

    assert validate_command_strict(_set("互动对象.甘雨.好感度", "12"), schema)[0]
    assert not validate_command_strict(_set("互动对象.甘雨.未知字段", "x"), schema)[0]
    assert not validate_command_strict(_set("未声明对象.字段", "x"), schema)[0]
