def test_llava_prompt_transcribes_not_infers():
    from carta.vision.router import LLAVA_PROMPT
    p = LLAVA_PROMPT.lower()
    assert "transcribe" in p
    assert "not infer" in p              # forbids inference explicitly
    assert "describe this technical diagram" not in p   # the old interpretive opener is gone
