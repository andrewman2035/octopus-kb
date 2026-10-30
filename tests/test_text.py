from octopus.text import is_cjk, join_text, to_simplified


def test_is_cjk():
    assert is_cjk("复") and is_cjk("。") and is_cjk("，")
    assert not is_cjk("a") and not is_cjk("1")


def test_join_text():
    assert join_text("", "b") == "b"
    assert join_text("复利", "需要时间") == "复利需要时间"
    assert join_text("hello", "world") == "hello world"
    assert join_text("Python", "是") == "Python是"


def test_to_simplified():
    assert to_simplified("復利需要時間，這個節目") == "复利需要时间，这个节目"
    assert to_simplified("plain") == "plain"
