from autodl_helper.state import StateStore


def test_state_store_tolerates_empty_json_file(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('', encoding='utf-8')

    store = StateStore(path)

    assert store.data == {'notifications': {}}

