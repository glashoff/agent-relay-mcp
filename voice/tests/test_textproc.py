import random

from agent_relay_voice.textproc import (
    MAX_SENTENCE, SentenceStreamer, apply_dictionary, clean_for_speech, parse_dictionary,
    split_sentences)


def test_split_basic():
    assert split_sentences("Hallo. Wie geht es? Gut!") == ["Hallo.", "Wie geht es?", "Gut!"]


def test_abbreviations_and_numbers_do_not_split():
    text = "Das ist z. B. ein Test. Es gibt ca. 3. Sorten usw. und mehr. Ende."
    assert split_sentences(text) == ["Das ist z. B. ein Test.", "Es gibt ca. 3. Sorten usw. und mehr.",
                                     "Ende."]


def test_line_breaks_end_sentences():
    assert split_sentences("Erstens\nZweitens\n\nDrittens") == ["Erstens", "Zweitens", "Drittens"]


def test_colon_ends_a_spoken_piece():
    assert split_sentences("Kurz gesagt: es läuft.") == ["Kurz gesagt:", "es läuft."]


def test_streaming_gives_each_sentence_once_it_is_complete():
    s = SentenceStreamer()
    assert s.feed("Hallo") == []
    assert s.feed(". Wie") == ["Hallo."]
    assert s.feed(" geht es?") == []          # no whitespace after "?" yet
    assert s.feed(" Gut") == ["Wie geht es?"]
    assert s.flush() == ["Gut"]
    assert s.flush() == []


def test_streaming_matches_whole_text_for_any_chunking():
    text = ("Alles klar, ich schaue nach. Das Projekt relay läuft im Dev-Container! "
            "Soll ich z. B. den Branch mergen? Ja\noder nein.\n\nEnde.")
    expected = split_sentences(text)
    rnd = random.Random(4)
    for _ in range(50):
        s = SentenceStreamer()
        out, i = [], 0
        while i < len(text):
            n = rnd.randint(1, 9)
            out += s.feed(text[i:i + n])
            i += n
        out += s.flush()
        assert out == expected


def test_markdown_is_not_read_aloud():
    assert clean_for_speech("**Wichtig:** das `foo` ist [hier](https://x.y) _fertig_") == \
        "Wichtig: das foo ist hier fertig"
    assert split_sentences("## Titel\n- erster Punkt\n- zweiter Punkt\n```\ncode\n```\nEnde.") == \
        ["Titel", "erster Punkt", "zweiter Punkt", "Ende."]
    assert split_sentences("Siehe https://example.org/abc für mehr.") == ["Siehe für mehr."]


def test_pieces_without_words_are_dropped():
    assert split_sentences("Hallo.\n---\n***\nEnde.") == ["Hallo.", "Ende."]


def test_long_sentences_are_cut():
    text = "Das ist ein Teil, " * 40 + "Ende."
    parts = split_sentences(text)
    assert len(parts) > 1
    assert all(len(p) <= MAX_SENTENCE for p in parts)
    assert " ".join(parts).replace("  ", " ") == " ".join(text.split())


def test_dictionary_longest_first_whole_words():
    entries = parse_dictionary("# c\nClaude=Klohd\nClaude Code=Klohd Kohd\nbad line\n")
    assert apply_dictionary("Claude Code und claude, aber nicht Claudes", entries) == \
        "Klohd Kohd und Klohd, aber nicht Claudes"
