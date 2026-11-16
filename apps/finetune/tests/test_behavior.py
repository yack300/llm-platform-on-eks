from finetune import behavior, data


def test_language():
    assert behavior.detected_language("The cold start takes ~5-6 minutes [1].") == "en"
    assert behavior.detected_language("El arranque en frío tarda ~5-6 minutos con la caché [1].") == "es"
    # Code spans don't count: English settings inside a Spanish answer.
    assert behavior.detected_language("Se usa `the use_lockfile of the backend` para el bloqueo.") == "es"


def test_declines():
    assert behavior.declines("I don't know; the passages don't mention it.")
    assert behavior.declines("Los pasajes no contienen esa información.")
    assert behavior.declines("The context does not provide the answer. However, ...")
    assert not behavior.declines("KEDA scales vLLM from 0 to 1 [2].")


def test_score():
    rows = [
        {"lang": "es", "answerable": True, "answer": "Tarda unos 5 minutos con la caché [1]."},
        {"lang": "es", "answerable": False, "answer": "No lo sé; los pasajes no lo cubren."},
        {"lang": "en", "answerable": True, "answer": "It is the gateway that routes requests."},
    ]
    s = behavior.score(rows)
    assert s["language"] == 1.0 and s["language_es"] == 1.0
    assert s["declines"] == 1.0 and s["false_declines"] == 0.0
    assert s["cites"] == 0.5


def test_dataset_agrees_with_its_labels():
    # The training targets themselves should score (almost) perfectly.
    rows = data.load("data/dataset", "train") + data.load("data/dataset", "validation")
    s = behavior.score(rows)
    assert s["language"] == 1.0
    assert s["declines"] >= 0.95 and s["false_declines"] <= 0.02
